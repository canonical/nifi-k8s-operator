#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Apache NiFi service."""

import hashlib
import logging

import ops

import constants
from properties_generator import NifiPropertiesGenerator

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
        self._renderer = NifiPropertiesGenerator()

        for event in [
            self.on[constants.CONTAINER_NAME].pebble_ready,
            self.on.start,
            self.on.config_changed,
            self.on.update_status,
        ]:
            self.framework.observe(event, self._reconcile)

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
        existing file rather than from the secret. This ensures the charm ignores
        any subsequent secret updates — key rotation is a future feature and
        changing the key while NiFi has existing flows would corrupt them.

        Raises:
            ExitWithStatusError(BlockedStatus): if the secret is unset, unreadable,
                missing the expected field, or shorter than the minimum length
                (applies only on first boot before the file exists on disk).
        """
        existing = NifiPropertiesGenerator.get_workload_property(
            self._container, "nifi.sensitive.props.key"
        )
        if existing:
            return existing

        # First boot: read from the configured Juju secret.
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

            # Juju storage mount points are owned by root; chown only when necessary.
            # Check the mount roots non-recursively first to avoid an expensive
            # recursive chown on every reconcile.
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
                }
            },
            "checks": {
                constants.READY_CHECK_NAME: {
                    "override": "replace",
                    "level": "ready",
                    "startup": "enabled",
                    "threshold": 3,
                    "http": {"url": f"http://localhost:{constants.NIFI_PORT}/nifi"},
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

    def _reconcile(self, _) -> None:
        """Idempotent reconcile handler for all charm events.

        1. Verify container connectivity.
        2. Render and push nifi.properties and state-management.xml (if changed).
        3. Apply pebble layer and (re)start the service as needed.
        """
        try:
            self._check_pebble_connection()
            self._ensure_storage_dirs()
            was_running = self._service_is_running()
            config_changed = self._write_nifi_properties()
            self._write_state_management_xml()

            # On first boot, replan starts the service (startup: enabled).
            # On subsequent reconciles with config changes, restart is required
            # because NiFi reads nifi.properties only at startup.
            self._add_layer_and_replan(restart=config_changed and was_running)

        except ExitWithStatusError as e:
            self.unit.status = e.status
            return

        # Gate active on the Pebble HTTP check so we don't claim active while
        # NiFi is still booting (replan() returns as soon as the process launches).
        check = self._container.get_checks(constants.READY_CHECK_NAME).get(
            constants.READY_CHECK_NAME
        )
        if check and check.status == ops.pebble.CheckStatus.UP:
            self.unit.status = ops.ActiveStatus()
        else:
            self.unit.status = ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)


if __name__ == "__main__":
    ops.main(NifiK8SOperatorCharm)
