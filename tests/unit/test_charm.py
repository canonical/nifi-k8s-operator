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
    def test_pebble_layer(self, context, state, container):
        """Pebble plan contains the nifi service with the expected configuration."""
        state_out = context.run(context.on.pebble_ready(container), state)
        service = state_out.get_container(constants.CONTAINER_NAME).plan.services[
            constants.SERVICE_NAME
        ]
        assert service.command == f"{constants.NIFI_HOME}/bin/nifi.sh run"
        assert service.startup == "enabled"


class TestNifiProperties:
    def test_nifi_properties(self, context, state, container):
        """nifi.properties is pushed to the container with the expected content."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content
        assert f"nifi.sensitive.props.key={constants.SENSITIVE_PROPS_KEY}" in content
        assert f"nifi.database.directory={constants.DATA_DIR}/database_repository" in content
        assert f"nifi.content.repository.directory.default={constants.CONTENT_REPO_DIR}" in content
        assert (
            f"nifi.provenance.repository.directory.default={constants.PROVENANCE_REPO_DIR}"
            in content
        )
        assert "nifi.security.allow.anonymous.authentication=true" in content
