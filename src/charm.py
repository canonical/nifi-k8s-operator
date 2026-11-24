#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Apache NiFi service."""

import hashlib
import ipaddress
import logging
import os
import socket
from pathlib import Path
from urllib.parse import urlparse

import charms.git_integrator.v0.git as git
import ops
from charmlibs.interfaces.tls_certificates import (
    CertificateRequestAttributes,
    Mode,
    PrivateKey,
    ProviderCertificate,
    TLSCertificatesRequiresV4,
)
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer

import constants
from nifi_rest_client import (
    NifiAuthError,
    NifiClientError,
    NifiConnectionError,
    NifiRestClient,
)
from properties_manager import NifiPropertiesManager

logger = logging.getLogger(__name__)


class ExitWithStatusError(Exception):
    """Exception raised to exit reconcile with a specific unit status."""

    def __init__(self, msg: str, status_type):
        super().__init__(str(msg))
        self.msg = str(msg)
        self.status_type = status_type

    @property
    def status(self):
        """Return the Juju unit status represented by this exception."""
        return self.status_type(self.msg)


class NifiK8SOperatorCharm(ops.CharmBase):
    """Charm the Apache NiFi service."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._container = self.unit.get_container(constants.CONTAINER_NAME)
        self._renderer = NifiPropertiesManager()
        self._nifi_client = NifiRestClient(f"http://localhost:{constants.NIFI_PORT}")
        self.git_registry = git.GitRequires(
            self,
            constants.GIT_REGISTRY_RELATION,
            callback=self._reconcile,
        )

        # Set before the ingress requirer is built:
        self.unit.set_ports(constants.NIFI_PORT)
        self.ingress = IngressPerAppRequirer(
            self,
            relation_name=constants.INGRESS_RELATION,
            port=constants.NIFI_PORT,
            strip_prefix=True,
        )

        # Setup after the ingress requirer: the server certificate includes the
        # ingress host, so ingress changes must refresh the requests.
        self.certificates = TLSCertificatesRequiresV4(
            self,
            constants.CERTIFICATES_RELATION,
            certificate_requests=lambda _capabilities=None: self._certificate_requests(),
            mode=Mode.UNIT,
            refresh_events=[self.on.upgrade_charm, self.ingress.on.ready, self.ingress.on.revoked],
        )

        for event in [
            self.on[constants.CONTAINER_NAME].pebble_ready,
            self.on.start,
            self.on.config_changed,
            self.on.update_status,
            self.on.secret_changed,
            self.on[constants.GIT_REGISTRY_RELATION].relation_broken,
            self.ingress.on.ready,
            self.ingress.on.revoked,
            self.certificates.on.certificate_available,
            self.certificates.on.certificate_denied,
            # Registered after the library's own handlers, so the requests are
            # already sent; reconciling here shows Waiting straight away.
            self.on[constants.CERTIFICATES_RELATION].relation_created,
            self.on[constants.CERTIFICATES_RELATION].relation_changed,
            self.on[constants.CERTIFICATES_RELATION].relation_broken,
        ]:
            self.framework.observe(event, self._reconcile)

    def _configure_git_registry_client(self) -> None:
        """Configure NiFi flow registry client via REST API using git-registry relation data.

        Raises:
            NifiClientError: If REST API call fails.
            ValueError: If repository URL is invalid.
            ExitWithStatusError(BlockedStatus): If git-integrator uses SSH auth
                (NiFi flow registry clients only support token-based auth)
        """
        connection_info_dict = self.git_registry.get_git_connection_information()
        if not connection_info_dict:
            return

        info = next(iter(connection_info_dict.values()))

        auth_method = getattr(info, "authentication_method", None)
        if auth_method and str(auth_method).lower() == "ssh":
            raise ExitWithStatusError(
                constants.MSG_GIT_REGISTRY_SSH_UNSUPPORTED, ops.BlockedStatus
            )

        self._nifi_client.create_or_update_registry_client(
            name=constants.FLOW_REGISTRY_CLIENT_NAME,
            repository_url=info.repository_url,
            branch=info.tracking_ref or "main",
            token=getattr(info, "credentials_personal_access_token", None),
        )

    def _delete_git_registry_client(self) -> None:
        """Delete NiFi flow registry client via REST API.

        Best-effort, except when NiFi rejects the charm's identity: that needs the
        operator, so it raises ExitWithStatusError(BlockedStatus).
        """
        try:
            self._nifi_client.delete_registry_client(constants.FLOW_REGISTRY_CLIENT_NAME)
        except NifiAuthError as e:
            logger.error("NiFi API rejected the charm's identity: %s", e)
            raise ExitWithStatusError(constants.MSG_NIFI_API_AUTH_FAILED, ops.BlockedStatus)
        except NifiClientError as e:
            logger.warning("Failed to delete flow registry client (best-effort): %s", e)

    def _reconcile_git_registry(self) -> None:
        """Reconcile git-registry: configure registry client if relation is ready, else clean up.

        Raises:
            ExitWithStatusError(BlockedStatus): If SSH auth is configured, the API call
                fails, or NiFi doesn't accept the charm's identity.
            ExitWithStatusError(MaintenanceStatus): If NiFi API is not yet reachable.
        """
        if not self.git_registry.relations:
            self._delete_git_registry_client()
            return

        try:
            self._configure_git_registry_client()
        except NifiConnectionError as e:
            logger.warning("NiFi API not reachable yet, Juju will retry on next hook: %s", e)
            raise ExitWithStatusError(constants.MSG_NIFI_STARTING, ops.MaintenanceStatus)
        except NifiAuthError as e:
            logger.error("NiFi API rejected the charm's identity: %s", e)
            raise ExitWithStatusError(constants.MSG_NIFI_API_AUTH_FAILED, ops.BlockedStatus)
        except NifiClientError as e:
            logger.exception("Failed to configure flow registry client: %s", e)
            raise ExitWithStatusError(constants.MSG_GIT_REGISTRY_API_ERROR, ops.BlockedStatus)
        except ValueError as e:
            logger.exception("Failed to configure flow registry client: %s", e)
            raise ExitWithStatusError(str(e), ops.BlockedStatus)

    def _check_git_registry(self) -> None:
        """If a git-registry relation exists but is not yet ready, raise."""
        if self.git_registry.relations and not self.git_registry.is_ready():
            raise ExitWithStatusError(constants.MSG_GIT_REGISTRY_NOT_READY, ops.WaitingStatus)

    def _check_ingress(self) -> None:
        """If an ingress relation exists but no URL has been published yet, raise.

        On revoke the relation is already excluded from the model, so this does
        not stall the reconcile that clears the proxy properties.
        """
        if self.model.get_relation(constants.INGRESS_RELATION) and not self.ingress.url:
            raise ExitWithStatusError(constants.MSG_INGRESS_NOT_READY, ops.WaitingStatus)

    def _ingress_proxy_settings(self) -> tuple[str, str]:
        """Derive (nifi.web.proxy.host, nifi.web.proxy.context.path) from the ingress URL.

        Both are empty when there is no ingress.
        """
        url = self.ingress.url
        if not url:
            return "", ""

        parsed = urlparse(url)
        if not parsed.hostname:
            return "", ""

        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return f"{parsed.hostname}:{port}", parsed.path.rstrip("/")

    @staticmethod
    def _fit_common_name(full: str, short: str) -> str | None:
        """Return the first name that fits a certificate's common name, or None.

        X.509 limits the common name to 64 characters. The short form swaps the
        model name for the start of the model UUID, so it stays unique, stays
        under svc.cluster.local (which Vault allows), and never changes.

        Limitations:
        - The short form isn't a DNS name. That's fine for TLS, since clients
          check the SANs, which always hold the full names.
        - Don't change this rule once released: the charm's identity is seeded
          in NiFi as its first admin, and a different name would lock the charm out.
        - None means even the short form is too long. The charm's identity is
          len(app) + len(model) + 25 characters in full and len(app) + 33 short,
          so it fails only when the app name is over 31 characters and app plus
          model is over 39. The server name (pod FQDN) is 2 * len(app) + len(model)
          + 32 in full and len(app) + 29 short, so it fails only for app names over
          35. Juju can't rename an application, so the only fix is redeploying with
          a shorter name.
        """
        for name in (full, short):
            if len(name) <= constants.TLS_COMMON_NAME_MAX_LENGTH:
                return name
        return None

    def _server_certificate_request(self) -> CertificateRequestAttributes | None:
        """NiFi's server certificate: the pod FQDN as common name, plus its other names.

        All names stay in the SANs, whatever the common name. The library's random
        ID stays in the subject (the default): without it a renewal sends the same
        request again and the provider never re-issues. None if no name fits.

        Limitation: the ingress host is a SAN, so the provider must allow it too.
        A Vault role needs the public parent domain (and pki_allow_ip_sans for an
        IP host); otherwise it denies the request and the unit goes Blocked.
        """
        pod_fqdn = socket.getfqdn()
        unit_host = self.unit.name.replace("/", "-")
        common_name = self._fit_common_name(
            pod_fqdn, f"{unit_host}.{self.model.uuid[:8]}.svc.cluster.local"
        )
        if not common_name:
            return None
        sans_dns = {pod_fqdn, f"{self.app.name}.{self.model.name}.svc.cluster.local"}
        sans_ip: set[str] = set()

        ingress_host = urlparse(self.ingress.url).hostname if self.ingress.url else None
        if ingress_host:
            try:
                ipaddress.ip_address(ingress_host)
                sans_ip.add(ingress_host)
            except ValueError:
                sans_dns.add(ingress_host)

        return CertificateRequestAttributes(
            common_name=common_name,
            sans_dns=sans_dns,
            sans_ip=sans_ip or None,
        )

    def _charm_identity_request(self) -> CertificateRequestAttributes | None:
        """The charm's client certificate, used later to call NiFi's API over HTTPS.

        The name is under svc.cluster.local. Every unit requests the same name.
        It is seeded as NiFi's first admin, so it depends only on the app and
        model, which never change. None if no name fits.
        """
        common_name = self._fit_common_name(
            f"charm.{self.app.name}.{self.model.name}.svc.cluster.local",
            f"charm.{self.app.name}.{self.model.uuid[:8]}.svc.cluster.local",
        )
        return CertificateRequestAttributes(common_name=common_name) if common_name else None

    def _certificate_requests(self) -> list[CertificateRequestAttributes]:
        """Both requests, or none if either name doesn't fit.

        The unit goes Blocked when one doesn't fit, so sending the other would
        only get a certificate issued for a unit that can't use it.
        """
        server = self._server_certificate_request()
        identity = self._charm_identity_request()
        if server is None or identity is None:
            return []
        return [server, identity]

    def _certificate_request_error(self, request: CertificateRequestAttributes) -> str | None:
        """Return the provider's error for one of this unit's requests, if it was denied.

        The library never re-sends a denied request with the same names. Recovery
        relies on the provider retrying it. A provider that doesn't retry
        needs the relation removed and added again.
        """
        for requirer_csr in self.certificates.get_csrs_from_requirer_relation_data():
            csr = requirer_csr.certificate_signing_request
            if CertificateRequestAttributes.from_csr(csr, requirer_csr.is_ca) != request:
                continue
            error = self.certificates.get_request_error(csr)
            if error:
                return error.error.message
        return None

    def _client_tls_dir(self) -> Path:
        # In the charm container, under the unit's agent directory. It isn't on a
        # volume, so a new pod starts without it; reconcile writes it again from
        # the library's secrets.
        return Path(self.charm_dir).parent / constants.CLIENT_TLS_DIR_NAME

    def _push_if_changed(self, path: str, content: str, permissions: int) -> None:
        """Push a file to the workload only when its content differs."""
        if self._container.exists(path) and self._container.pull(path).read() == content:
            return
        self._container.push(
            path,
            content,
            user=constants.WORKLOAD_USER,
            group=constants.WORKLOAD_GROUP,
            permissions=permissions,
            make_dirs=True,
        )

    @staticmethod
    def _cert_with_intermediates(cert: ProviderCertificate) -> str:
        """The leaf followed by any intermediates, without the CA.

        ca.pem may hold only the root, so a peer can only build the chain
        if the intermediates are sent with the leaf.
        """
        leaf, ca = str(cert.certificate), str(cert.ca)
        intermediates = [str(c) for c in cert.chain if str(c) not in (leaf, ca)]
        return "\n".join([leaf, *intermediates]) + "\n"

    def _write_server_tls_files(self, cert: ProviderCertificate, key: PrivateKey) -> None:
        """Write NiFi's certificate (with intermediates), key and CA to the workload.

        Renewals keep the same file names, so NiFi can reload them in place.
        """
        # TODO: the three files are pushed one by one, so a new key can
        # briefly sit next to the old certificate. Harmless while NiFi serves HTTP;
        # once NiFi reloads these files, write the certificate and key so it never
        # reads a mismatched pair.
        try:
            self._push_if_changed(constants.TLS_KEY_PATH, f"{key}\n", 0o600)
            self._push_if_changed(
                constants.TLS_CERT_PATH, self._cert_with_intermediates(cert), 0o644
            )
            self._push_if_changed(constants.TLS_CA_PATH, f"{cert.ca}\n", 0o644)
        except ops.pebble.Error as e:
            logger.exception("Failed to write TLS files: %s", e)
            raise ExitWithStatusError(constants.MSG_TLS_WRITE_FAILED, ops.BlockedStatus)

    def _write_client_files(self, files: list[tuple[str, str, int]]) -> None:
        """Write (name, content, mode) files to the client TLS dir in the charm container."""
        directory = self._client_tls_dir()
        try:
            directory.mkdir(mode=0o700, exist_ok=True)
            for name, content, mode in files:
                path = directory / name
                if path.exists() and path.read_text() == content:
                    continue
                # Create the file with its final mode, so the key is never readable by others.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                path.chmod(mode)
        except OSError as e:
            logger.exception("Failed to write the charm's client TLS files: %s", e)
            raise ExitWithStatusError(constants.MSG_TLS_WRITE_FAILED, ops.BlockedStatus)

    def _write_client_tls_files(self, cert: ProviderCertificate, key: PrivateKey) -> None:
        """Write the charm's client certificate (with intermediates), key and CA.

        They stay in the charm container only.
        """
        self._write_client_files(
            [
                ("cert.pem", self._cert_with_intermediates(cert), 0o644),
                ("key.pem", f"{key}\n", 0o600),
                ("ca.pem", f"{cert.ca}\n", 0o644),
            ]
        )

    def _tls_files_written(self, relation: ops.Relation) -> bool:
        """Whether both sets of files were written from this relation's certificates.

        Files from an earlier relation may still be on disk, but they don't count:
        after the relation is added again, possibly with another provider, the
        unit waits until the new certificates are written.
        """
        marker = self._client_tls_dir() / constants.TLS_RELATION_ID_FILE
        return (
            self._container.exists(constants.TLS_CERT_PATH)
            and marker.exists()
            and marker.read_text() == str(relation.id)
        )

    def _reconcile_tls(self) -> None:
        """Write the certificates from the `certificates` relation, once issued.

        NiFi doesn't use these files yet; it still serves HTTP. Without the
        relation, existing files are left in place, but they don't outlive the
        pod: the library deletes its secrets when the relation is removed, so
        nothing can write them again after a restart.

        Raises:
            ExitWithStatusError(BlockedStatus): If the names can't fit a certificate,
                or the provider denied a request.
            ExitWithStatusError(WaitingStatus): Until this relation's certificates are
                first written.
        """
        relation = self.model.get_relation(constants.CERTIFICATES_RELATION)
        if not relation:
            return

        server_request = self._server_certificate_request()
        identity_request = self._charm_identity_request()
        if server_request is None or identity_request is None:
            raise ExitWithStatusError(constants.MSG_TLS_NAME_TOO_LONG, ops.BlockedStatus)
        for request in (server_request, identity_request):
            reason = self._certificate_request_error(request)
            if reason:
                raise ExitWithStatusError(
                    constants.MSG_TLS_DENIED.format(reason=reason), ops.BlockedStatus
                )

        server_cert, server_key = self.certificates.get_assigned_certificate(server_request)
        if server_cert and server_key:
            self._write_server_tls_files(server_cert, server_key)
        identity_cert, identity_key = self.certificates.get_assigned_certificate(identity_request)
        if identity_cert and identity_key:
            self._write_client_tls_files(identity_cert, identity_key)
        if server_cert and identity_cert:
            self._write_client_files([(constants.TLS_RELATION_ID_FILE, str(relation.id), 0o644)])

        # A renewal also leaves a request pending for a while; the current files
        # stay valid then, so only wait until this relation's first certificates
        # are written.
        if not self._tls_files_written(relation):
            raise ExitWithStatusError(constants.MSG_TLS_WAITING, ops.WaitingStatus)

    def _rotate_sensitive_key(self, event) -> str | None:
        """Run nifi.sh key rotation if *event* is a secret-changed for our key.

        Returns the new key if rotation was performed, None if the event is
        unrelated.

        Raises:
            ExitWithStatusError: on validation failures or pebble errors.
        """
        if not isinstance(event, ops.SecretChangedEvent):
            return None
        if event.secret.id != self.config.get(constants.SENSITIVE_PROPS_KEY_CONFIG):
            return None

        try:
            content = event.secret.get_content(refresh=True)
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            logger.exception("Failed to refresh sensitive-props-key secret: %s", e)
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_INVALID, ops.BlockedStatus)

        new_key = content.get(constants.SENSITIVE_PROPS_KEY_FIELD, "")
        if len(new_key) < constants.SENSITIVE_PROPS_KEY_MIN_LENGTH:
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_TOO_SHORT, ops.BlockedStatus)

        try:
            self._container.stop(constants.SERVICE_NAME)
            self._container.exec(
                [f"{constants.NIFI_HOME}/bin/nifi.sh", "set-sensitive-properties-key", new_key],
                user=constants.WORKLOAD_USER,
                group=constants.WORKLOAD_GROUP,
            ).wait_output()
        except ops.pebble.Error as e:
            logger.exception("Failed to rotate sensitive props key: %s", e)
            raise ExitWithStatusError(constants.MSG_KEY_ROTATION_FAILED, ops.BlockedStatus)
        return new_key

    def _check_pebble_connection(self) -> None:
        """Verify connection to the container; otherwise raise."""
        if not self._container.can_connect():
            raise ExitWithStatusError(constants.MSG_PEBBLE_NOT_READY, ops.MaintenanceStatus)

    def _resolve_secret_field(self, config_key: str, field_name: str) -> str:
        """Read a field from the Juju user secret referenced by a config option.

        Args:
            config_key: The charm config option holding the secret URI.
            field_name: The expected field inside the secret content.

        Returns:
            The secret field value.

        Raises:
            ExitWithStatusError(BlockedStatus): If the config is unset, the
                secret is unreadable, or the field is missing/empty.
        """
        secret_id = self.config.get(config_key)
        if not secret_id:
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_MISSING, ops.BlockedStatus)
        try:
            content = self.model.get_secret(id=secret_id).get_content()
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            logger.exception("Failed to read '%s' secret: %s", config_key, e)
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_INVALID, ops.BlockedStatus)

        value = content.get(field_name, "")
        if not value:
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_INVALID, ops.BlockedStatus)
        return value

    def _get_sensitive_props_key(self) -> str:
        """Resolve nifi.sensitive.props.key from the configured Juju user secret.

        Once nifi.properties has been written to disk, the key is read from the
        existing file rather than from the secret. This ensures reconcile ignores
        any secret updates that were not routed through the deliberate rotation
        flow (see _rotate_sensitive_key), which is the only path allowed to
        change the key in place — an unmanaged change would leave NiFi unable
        to decrypt existing flows.

        Raises:
            ExitWithStatusError(BlockedStatus): if the secret is unset, unreadable,
                missing the expected field, or shorter than the minimum length
                (applies only on first boot before the file exists on disk).
            ExitWithStatusError(MaintenanceStatus): if nifi.properties exists but
                cannot be read due to I/O or permission errors.
        """
        try:
            existing = NifiPropertiesManager.get_nifi_property(
                self._container, "nifi.sensitive.props.key"
            )
        except ops.pebble.Error as e:
            logger.exception("Cannot read nifi.properties from workload: %s", e)
            raise ExitWithStatusError(constants.MSG_PROPERTY_READ_ERROR, ops.MaintenanceStatus)

        if existing:
            return existing

        key = self._resolve_secret_field(
            constants.SENSITIVE_PROPS_KEY_CONFIG,
            constants.SENSITIVE_PROPS_KEY_FIELD,
        )
        if len(key) < constants.SENSITIVE_PROPS_KEY_MIN_LENGTH:
            raise ExitWithStatusError(constants.MSG_SENSITIVE_KEY_TOO_SHORT, ops.BlockedStatus)
        return key

    # TODO: Refactor to potentially remove this method once nifi rock is available.
    def _ensure_storage_dirs(self) -> None:
        """Create NiFi storage subdirectories if they don't already exist.

        Juju mounts the storage volumes group-owned by the rootless fs group
        (gid 170) and group-writable, and the workload runs as _daemon_ with
        that supplemental group, so the repositories are writable without any
        privileged chown (which a non-root Pebble could not perform anyway).
        """
        dirs = [
            f"{constants.DATA_DIR}/database_repository",
            f"{constants.DATA_DIR}/flowfile_repository",
            f"{constants.DATA_DIR}/conf",
            constants.CONTENT_REPO_DIR,
            constants.PROVENANCE_REPO_DIR,
        ]
        try:
            for d in dirs:
                if not self._container.exists(d):
                    self._container.make_dir(
                        d,
                        user=constants.WORKLOAD_USER,
                        group=constants.WORKLOAD_GROUP,
                        make_parents=True,
                    )
        except ops.pebble.Error as e:
            logger.exception("Failed to create storage directories: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)

    def _write_nifi_properties(self, sensitive_props_key: str | None = None) -> bool:
        """Write nifi.properties to the workload container.

        Args:
            sensitive_props_key: Key to render, supplied by the rotation flow.
                Resolved from disk or the configured secret when not given.

        Returns True if the file was created or updated, False if unchanged.
        """
        if sensitive_props_key is None:
            sensitive_props_key = self._get_sensitive_props_key()
        proxy_host, proxy_context_path = self._ingress_proxy_settings()
        try:
            rendered = self._renderer.render_nifi_properties(
                sensitive_props_key=sensitive_props_key,
                proxy_host=proxy_host,
                proxy_context_path=proxy_context_path,
            )
        except RuntimeError as e:
            logger.exception("Failed to render nifi.properties: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)
        rendered_hash = hashlib.sha256(rendered.encode()).hexdigest()

        try:
            if self._container.exists(constants.NIFI_PROPERTIES_PATH):
                on_disk = self._container.pull(constants.NIFI_PROPERTIES_PATH).read()
                on_disk_hash = hashlib.sha256(on_disk.encode()).hexdigest()
                if rendered_hash == on_disk_hash:
                    return False

            self._container.push(
                constants.NIFI_PROPERTIES_PATH,
                rendered,
                user=constants.WORKLOAD_USER,
                group=constants.WORKLOAD_GROUP,
                make_dirs=True,
            )
        except ops.pebble.Error as e:
            logger.exception("Failed to write nifi.properties: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)
        return True

    def _write_state_management_xml(self) -> None:
        """Write state-management.xml to the workload container if not already present.

        The contents are static; no diffing is needed.
        """
        try:
            rendered_xml = self._renderer.render_state_management_xml()
        except RuntimeError as e:
            logger.exception("Failed to render state-management.xml: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)

        try:
            if self._container.exists(constants.STATE_MANAGEMENT_XML_PATH):
                return

            self._container.push(
                constants.STATE_MANAGEMENT_XML_PATH,
                rendered_xml,
                user=constants.WORKLOAD_USER,
                group=constants.WORKLOAD_GROUP,
                make_dirs=True,
            )
        except ops.pebble.Error as e:
            logger.exception("Failed to write state-management.xml: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)

    def _service_is_running(self) -> bool:
        """Check if the NiFi pebble service is currently running.

        Returns False on first boot before the layer has been applied,
        when get_service() raises ModelError because the service is not yet
        registered in the pebble plan.
        """
        try:
            return self._container.get_service(constants.SERVICE_NAME).is_running()
        except ops.ModelError:
            return False

    @property
    def _pebble_layer(self) -> ops.pebble.LayerDict:
        """Define the Pebble layer for the NiFi workload."""
        return {
            "services": {
                constants.SERVICE_NAME: {
                    "override": "replace",
                    "summary": "Apache NiFi",
                    "command": f"{constants.NIFI_HOME}/bin/nifi.sh run",
                    "startup": "enabled",
                    "user": constants.WORKLOAD_USER,
                    "group": constants.WORKLOAD_GROUP,
                    "kill-delay": constants.NIFI_KILL_DELAY,
                    # override: replace wipes the rock's baked service environment,
                    # so it is redeclared here. JAVA_HOME is required or nifi.sh
                    # cannot find Java (the rock does not register the java
                    # alternative on PATH). NIFI_LOG_DIR + NIFI_OVERRIDE_NIFIENV
                    # keep logs on /var/log/nifi; without the override
                    # flag nifi-env.sh ignores NIFI_LOG_DIR and logs to
                    # $NIFI_HOME/logs instead.
                    "environment": {
                        "JAVA_HOME": constants.JAVA_HOME,
                        "NIFI_HOME": constants.NIFI_HOME,
                        "NIFI_LOG_DIR": constants.NIFI_LOG_DIR,
                        "NIFI_OVERRIDE_NIFIENV": "true",
                    },
                }
            },
            "checks": {
                constants.READY_CHECK_NAME: {
                    "override": "replace",
                    "level": "ready",
                    "startup": "enabled",
                    "threshold": 3,
                    "http": {"url": f"http://localhost:{constants.NIFI_PORT}/nifi/"},
                }
            },
        }

    def _add_layer_and_replan(self, restart: bool = False) -> None:
        """Add the Pebble layer and replan services.

        The service starts automatically after replanning as startup is enabled.

        Args:
            restart: If True, restart the service instead of replanning to avoid
                a double-restart (replan restarts a running service when the plan
                changes; an explicit restart on top would start it a second time).

        Raises:
            ExitWithStatusError: If replanning or restarting fails.
        """
        self._container.add_layer(constants.SERVICE_NAME, self._pebble_layer, combine=True)
        try:
            if restart:
                self._container.restart(constants.SERVICE_NAME)
            else:
                self._container.replan()
        except ops.pebble.Error as e:
            logger.exception("Pebble (re)start failed: %s", e)
            raise ExitWithStatusError(constants.MSG_SERVICE_START_FAILED, ops.BlockedStatus)

    def _check_nifi_ready(self) -> None:
        """Verify NiFi HTTP endpoint is UP; raise if still starting."""
        check = self._container.get_checks(constants.READY_CHECK_NAME).get(
            constants.READY_CHECK_NAME
        )
        if not (check and check.status == ops.pebble.CheckStatus.UP):
            raise ExitWithStatusError(constants.MSG_NIFI_STARTING, ops.MaintenanceStatus)

    def _reconcile(self, event) -> None:
        """Idempotent reconcile handler for all charm events.

        All events funnel through here. The reconcile inspects the current
        world state and converges to the correct unit status:

        1. Verify container connectivity.
        2. If secret-changed for our key, rotate the key via nifi.sh.
        3. Check git-registry and ingress relation readiness.
        4. Ensure storage directories exist.
        5. Render and push nifi.properties and state-management.xml.
        6. Apply pebble layer and (re)start the service as needed.
        7. Wait for NiFi to become ready.
        8. Configure or clean up the flow registry client via REST API.
        9. Write TLS certificates, once issued. This runs last because NiFi
           doesn't use them yet, so a pending certificate must not hold back
           the HTTP setup above. As a result, the files are written only once
           NiFi is ready.
        """
        try:
            self._check_pebble_connection()
            new_key = self._rotate_sensitive_key(event)
            self._check_git_registry()
            self._check_ingress()
            self._ensure_storage_dirs()
            was_running = self._service_is_running()
            config_changed = self._write_nifi_properties(new_key)
            self._write_state_management_xml()

            # Restart when config drifted on a running service, or after rotation
            # (NiFi was stopped by the toolkit command and must be brought back).
            self._add_layer_and_replan(
                restart=(config_changed and was_running) or new_key is not None
            )
            self._check_nifi_ready()
            self._reconcile_git_registry()
            self._reconcile_tls()
        except ExitWithStatusError as e:
            self.unit.status = e.status
            return

        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(NifiK8SOperatorCharm)
