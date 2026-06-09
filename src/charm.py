#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the Apache NiFi service."""

import hashlib
import logging
import pathlib

import ops
from jinja2 import Environment, FileSystemLoader

import constants

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

        self.framework.observe(
            self.on[constants.CONTAINER_NAME].pebble_ready, self._reconcile
        )
        self.framework.observe(self.on.start, self._reconcile)
        self.framework.observe(self.on.config_changed, self._reconcile)
        self.framework.observe(self.on.update_status, self._reconcile)

    def _render_nifi_properties(self) -> str:
        """Render nifi.properties from the Jinja2 template."""
        templates_dir = pathlib.Path(__file__).parent / "templates"
        # autoescape disabled — generating a properties file, not HTML
        env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)
        template = env.get_template("nifi.properties.j2")
        return template.render(
            data_dir=constants.DATA_DIR,
            content_repo_dir=constants.CONTENT_REPO_DIR,
            provenance_repo_dir=constants.PROVENANCE_REPO_DIR,
            http_host=constants.NIFI_HTTP_HOST,
            http_port=constants.NIFI_PORT,
            sensitive_props_key=constants.SENSITIVE_PROPS_KEY,
        )

    def _ensure_storage_dirs(self) -> None:
        """Create NiFi storage directories if they don't already exist."""
        dirs = [
            f"{constants.DATA_DIR}/database_repository",
            f"{constants.DATA_DIR}/flowfile_repository",
            f"{constants.DATA_DIR}/conf",
            constants.CONTENT_REPO_DIR,
            constants.PROVENANCE_REPO_DIR,
        ]
        for d in dirs:
            if not self._container.exists(d):
                self._container.make_dir(
                    d,
                    user=constants.WORKLOAD_USER,
                    group=constants.WORKLOAD_GROUP,
                    make_parents=True,
                )

        # Juju storage mount points are owned by root; NiFi needs write access.
        self._container.exec(
            [
                "chown",
                "-R",
                f"{constants.WORKLOAD_USER}:{constants.WORKLOAD_GROUP}",
                constants.DATA_DIR,
                constants.CONTENT_REPO_DIR,
                constants.PROVENANCE_REPO_DIR,
            ]
        ).wait()

    def _write_nifi_properties(self) -> bool:
        """Write nifi.properties to the workload container.

        Returns True if the file was created or updated, False if unchanged.
        """
        rendered = self._render_nifi_properties()
        rendered_hash = hashlib.sha256(rendered.encode()).hexdigest()

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
        return True

    def _service_is_running(self) -> bool:
        """Check if the NiFi pebble service is currently running."""
        try:
            return self._container.get_service(constants.SERVICE_NAME).is_running()
        except (ops.pebble.APIError, ops.ModelError):
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
                    "working-dir": constants.NIFI_HOME,
                }
            }
        }

    def _add_layer_and_replan(self, restart: bool = False) -> None:
        """Add the Pebble layer and replan services.

        The service starts automatically after replanning as startup is enabled.

        Args:
            restart: If True, restart the service after replanning.

        Raises:
            ExitWithStatusError: If replanning or restarting fails.
        """
        self._container.add_layer(constants.SERVICE_NAME, self._pebble_layer, combine=True)
        try:
            self._container.replan()
            if restart:
                self._container.restart(constants.SERVICE_NAME)
        except ops.pebble.ChangeError as e:
            logger.exception("Pebble replan failed: %s", e)
            raise ExitWithStatusError(
                "Failed to (re)start NiFi service", ops.BlockedStatus
            )
        except ops.pebble.APIError as e:
            logger.exception("Pebble API error during restart: %s", e)
            raise ExitWithStatusError(
                "Failed to (re)start NiFi service", ops.BlockedStatus
            )

    def _reconcile(self, _) -> None:
        """Idempotent reconcile handler for all charm events.

        1. Verify container connectivity.
        2. Render and push nifi.properties (if changed).
        3. Apply pebble layer and (re)start the service as needed.
        """
        try:
            if not self._container.can_connect():
                raise ExitWithStatusError(
                    "Waiting for Pebble to be ready", ops.WaitingStatus
                )

            self._ensure_storage_dirs()
            was_running = self._service_is_running()
            config_changed = self._write_nifi_properties()

            # On first boot, replan starts the service (startup: enabled).
            # On subsequent reconciles with config changes, restart is required
            # because NiFi reads nifi.properties only at startup.
            self._add_layer_and_replan(restart=config_changed and was_running)

        except ExitWithStatusError as e:
            self.unit.status = e.status
            return

        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":
    ops.main(NifiK8SOperatorCharm)
