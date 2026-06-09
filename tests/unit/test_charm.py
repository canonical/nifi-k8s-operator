# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NiFi K8s charm."""

import ops
import ops.testing

import constants


class TestReconcile:
    def test_pebble_ready_reaches_active(self, context, state, container):
        """Charm reaches ActiveStatus when the container is ready."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_container_not_ready_goes_waiting(self, context):
        """Charm enters WaitingStatus when the container is not reachable."""
        container = ops.testing.Container(
            name=constants.CONTAINER_NAME,
            can_connect=False,
        )
        state_in = ops.testing.State(containers=[container])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.WaitingStatus("Waiting for Pebble to be ready")

    def test_start_container_not_ready(self, context):
        """Start event with unavailable container goes to WaitingStatus."""
        container = ops.testing.Container(
            name=constants.CONTAINER_NAME,
            can_connect=False,
        )
        state_in = ops.testing.State(containers=[container])
        state_out = context.run(context.on.start(), state_in)
        assert isinstance(state_out.unit_status, ops.WaitingStatus)


class TestPebbleLayer:
    def test_pebble_plan_has_nifi_service(self, context, state, container):
        """Pebble plan contains the nifi service after reconcile."""
        state_out = context.run(context.on.pebble_ready(container), state)
        plan = state_out.get_container(constants.CONTAINER_NAME).plan
        assert constants.SERVICE_NAME in plan.services

    def test_nifi_service_command(self, context, state, container):
        """NiFi service command runs nifi.sh in foreground mode."""
        state_out = context.run(context.on.pebble_ready(container), state)
        plan = state_out.get_container(constants.CONTAINER_NAME).plan
        service = plan.services[constants.SERVICE_NAME]
        assert service.command == f"{constants.NIFI_HOME}/bin/nifi.sh run"

    def test_nifi_service_startup_enabled(self, context, state, container):
        """NiFi service has startup enabled so it starts on replan."""
        state_out = context.run(context.on.pebble_ready(container), state)
        plan = state_out.get_container(constants.CONTAINER_NAME).plan
        service = plan.services[constants.SERVICE_NAME]
        assert service.startup == "enabled"


class TestNifiProperties:
    def test_nifi_properties_pushed_on_pebble_ready(self, context, state, container):
        """nifi.properties is pushed to the container on pebble_ready."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        properties_file = root / constants.NIFI_PROPERTIES_PATH.lstrip("/")
        assert properties_file.exists()

    def test_nifi_properties_contains_http_port(self, context, state, container):
        """Rendered nifi.properties has the expected HTTP port."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content

    def test_nifi_properties_contains_sensitive_props_key(self, context, state, container):
        """Rendered nifi.properties has the placeholder sensitive props key."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert f"nifi.sensitive.props.key={constants.SENSITIVE_PROPS_KEY}" in content

    def test_nifi_properties_uses_juju_storage_paths(self, context, state, container):
        """Repository directories point to the Juju storage mount paths."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert f"nifi.database.directory={constants.DATA_DIR}/database_repository" in content
        assert f"nifi.content.repository.directory.default={constants.CONTENT_REPO_DIR}" in content
        assert (
            f"nifi.provenance.repository.directory.default={constants.PROVENANCE_REPO_DIR}"
            in content
        )

    def test_anonymous_authentication_enabled(self, context, state, container):
        """Anonymous authentication is enabled for HTTP mode."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert "nifi.security.allow.anonymous.authentication=true" in content
