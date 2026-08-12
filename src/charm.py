#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Apache NiFi service."""

import hashlib
import logging

import charms.git_integrator.v0.git as git
import ops

import constants
from nifi_rest_client import NifiClientError, NifiConnectionError, NifiRestClient
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

        for event in [
            self.on[constants.CONTAINER_NAME].pebble_ready,
            self.on.start,
            self.on.config_changed,
            self.on.update_status,
            self.on.secret_changed,
            self.on[constants.GIT_REGISTRY_RELATION].relation_broken,
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
        """Delete NiFi flow registry client via REST API. Best-effort."""
        try:
            self._nifi_client.delete_registry_client(constants.FLOW_REGISTRY_CLIENT_NAME)
        except (NifiClientError, NifiConnectionError) as e:
            logger.warning("Failed to delete flow registry client (best-effort): %s", e)

    def _reconcile_git_registry(self) -> None:
        """Reconcile git-registry: configure registry client if relation is ready, else clean up.

        Raises:
            ExitWithStatusError(BlockedStatus): If SSH auth is configured or API call fails.
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

    def _rotate_sensitive_key(self, event) -> bool:
        """Run nifi.sh key rotation if *event* is a secret-changed for our key.

        Returns True if rotation was performed (caller must restart NiFi).
        Returns False if the event is unrelated and no action was taken.

        Raises:
            ExitWithStatusError: on validation failures or pebble errors.
        """
        if not isinstance(event, ops.SecretChangedEvent):
            return False
        if event.secret.id != self.config.get(constants.SENSITIVE_PROPS_KEY_CONFIG):
            return False

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
            self._container.push(
                constants.NIFI_PROPERTIES_PATH,
                self._renderer.render_nifi_properties(sensitive_props_key=new_key),
                user=constants.WORKLOAD_USER,
                group=constants.WORKLOAD_GROUP,
                make_dirs=True,
            )
        except ops.pebble.Error as e:
            logger.exception("Failed to rotate sensitive props key: %s", e)
            raise ExitWithStatusError(constants.MSG_KEY_ROTATION_FAILED, ops.BlockedStatus)
        return True

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
        """Create NiFi storage directories if they don't already exist."""
        dirs = [
            f"{constants.DATA_DIR}/database_repository",
            f"{constants.DATA_DIR}/flowfile_repository",
            f"{constants.DATA_DIR}/conf",
            constants.CONTENT_REPO_DIR,
            constants.PROVENANCE_REPO_DIR,
        ]
        mount_roots = [
            constants.DATA_DIR,
            constants.CONTENT_REPO_DIR,
            constants.PROVENANCE_REPO_DIR,
        ]
        expected_owner = f"{constants.WORKLOAD_USER}:{constants.WORKLOAD_GROUP}"
        try:
            for d in dirs:
                if not self._container.exists(d):
                    self._container.make_dir(
                        d,
                        user=constants.WORKLOAD_USER,
                        group=constants.WORKLOAD_GROUP,
                        make_parents=True,
                    )

            needs_chown = any(
                self._container.exec(["stat", "-c", "%U:%G", root]).wait_output()[0].strip()
                != expected_owner
                for root in mount_roots
                if self._container.exists(root)
            )
            if needs_chown:
                self._container.exec(
                    [
                        "chown",
                        "-R",
                        expected_owner,
                        *mount_roots,
                    ]
                ).wait()
        except ops.pebble.Error as e:
            logger.exception("Failed to create storage directories: %s", e)
            raise ExitWithStatusError(constants.MSG_CONFIG_WRITE_FAILED, ops.BlockedStatus)

    def _write_nifi_properties(self) -> bool:
        """Write nifi.properties to the workload container.

        Returns True if the file was created or updated, False if unchanged.
        """
        sensitive_props_key = self._get_sensitive_props_key()
        try:
            rendered = self._renderer.render_nifi_properties(
                sensitive_props_key=sensitive_props_key,
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
        2. If secret-changed for our key, rotate (nifi.sh + push new properties).
        3. Check git-registry relation readiness.
        4. Ensure storage directories exist.
        5. Render and push nifi.properties and state-management.xml.
        6. Apply pebble layer and (re)start the service as needed.
        7. Wait for NiFi to become ready.
        8. Configure or clean up the flow registry client via REST API.
        """
        try:
            self._check_pebble_connection()
            rotated = self._rotate_sensitive_key(event)
            self._check_git_registry()
            self._ensure_storage_dirs()
            was_running = self._service_is_running()
            config_changed = self._write_nifi_properties()
            self._write_state_management_xml()

            # Restart when config drifted on a running service, or after rotation
            # (NiFi was stopped by the toolkit command and must be brought back).
            self._add_layer_and_replan(restart=config_changed and was_running or rotated)
            self._check_nifi_ready()
            self._reconcile_git_registry()
        except ExitWithStatusError as e:
            self.unit.status = e.status
            return

        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(NifiK8SOperatorCharm)
