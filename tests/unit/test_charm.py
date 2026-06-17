# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NiFi K8s charm."""

import unittest.mock

import ops
import ops.testing
import pytest

import constants
from charm import NifiK8SOperatorCharm


class TestReconcile:
    def test_pebble_ready_reaches_active(self, context, state, container):
        """Charm reaches ActiveStatus when the container is ready and check passes."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_pebble_ready_maintenance_while_starting(self, context, monkeypatch):
        """Charm stays in MaintenanceStatus while NiFi is still booting (check DOWN)."""
        booting_container = ops.testing.Container(
            name=constants.CONTAINER_NAME,
            can_connect=True,
            execs={
                ops.testing.Exec(
                    [
                        "chown",
                        "-R",
                        f"{constants.WORKLOAD_USER}:{constants.WORKLOAD_GROUP}",
                        constants.DATA_DIR,
                        constants.CONTENT_REPO_DIR,
                        constants.PROVENANCE_REPO_DIR,
                    ],
                ),
            },
        )
        state_in = ops.testing.State(containers=[booting_container])

        # Override pebble check response to simulate NiFi not yet ready.
        monkeypatch.setattr(
            ops.Container,
            "get_checks",
            lambda self, *names, **kwargs: {
                "nifi-ready": ops.pebble.CheckInfo(
                    "nifi-ready",
                    level=ops.pebble.CheckLevel.READY,
                    status=ops.pebble.CheckStatus.DOWN,
                    failures=1,
                )
            },
        )

        state_out = context.run(context.on.pebble_ready(booting_container), state_in)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)

    @pytest.mark.parametrize(
        "make_event",
        [
            lambda ctx, c: ctx.on.pebble_ready(c),
            lambda ctx, c: ctx.on.start(),
        ],
        ids=["pebble_ready", "start"],
    )
    def test_container_not_ready_goes_maintenance(self, context, disconnected_state, make_event):
        """Charm enters MaintenanceStatus when the container is not reachable."""
        state_in, container = disconnected_state
        state_out = context.run(make_event(context, container), state_in)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_PEBBLE_NOT_READY)


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


class TestStateManagementXml:
    def test_state_management_xml(self, context, state, container):
        """state-management.xml is pushed with the local state directory on nifi-data storage."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.STATE_MANAGEMENT_XML_PATH.lstrip("/")).read_text()
        assert f"<property name=\"Directory\">{constants.DATA_DIR}/state/local</property>" in content


class TestServiceRestart:
    def test_service_restarts_when_properties_change(
        self, context, running_container, monkeypatch
    ):
        """Second reconcile with changed properties restarts the service."""
        state_in = ops.testing.State(containers=[running_container])

        mock_replan = unittest.mock.Mock()
        # Simulate config change: nifi.properties differs from what's on disk.
        monkeypatch.setattr(NifiK8SOperatorCharm, "_write_nifi_properties", lambda self: True)
        monkeypatch.setattr(
            NifiK8SOperatorCharm, "_write_state_management_xml", lambda self: False
        )
        monkeypatch.setattr(NifiK8SOperatorCharm, "_add_layer_and_replan", mock_replan)

        context.run(context.on.config_changed(), state_in)

        mock_replan.assert_called_once_with(restart=True)

    def test_service_not_restarted_when_properties_unchanged(
        self, context, running_container, monkeypatch
    ):
        """Second reconcile with unchanged properties does not restart the service."""
        state_in = ops.testing.State(containers=[running_container])

        mock_replan = unittest.mock.Mock()
        # Simulate no config change: rendered output matches what's already on disk.
        monkeypatch.setattr(NifiK8SOperatorCharm, "_write_nifi_properties", lambda self: False)
        monkeypatch.setattr(
            NifiK8SOperatorCharm, "_write_state_management_xml", lambda self: False
        )
        monkeypatch.setattr(NifiK8SOperatorCharm, "_add_layer_and_replan", mock_replan)

        context.run(context.on.config_changed(), state_in)

        mock_replan.assert_called_once_with(restart=False)
