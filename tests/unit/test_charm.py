# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NiFi K8s charm."""

import dataclasses
import pathlib
import tempfile
from unittest.mock import patch

import ops
import ops.testing
import pytest
from conftest import SENSITIVE_KEY_VALUE

import constants


class TestReconcile:
    def test_pebble_ready_reaches_active(self, context, state, container):
        """Charm reaches ActiveStatus when the container is ready and check passes."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_update_status_reaches_active_when_running(self, context, running_state):
        """update_status reports ActiveStatus when the service is up and check passes."""
        state_out = context.run(context.on.update_status(), running_state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_pebble_ready_maintenance_while_starting(
        self, context, booting_state, booting_container
    ):
        """Charm stays in MaintenanceStatus while NiFi is still booting (check DOWN)."""
        state_out = context.run(context.on.pebble_ready(booting_container), booting_state)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)

    @pytest.mark.parametrize(
        "make_event",
        [
            lambda ctx, c: ctx.on.pebble_ready(c),
            lambda ctx, c: ctx.on.config_changed(),
            lambda ctx, c: ctx.on.update_status(),
        ],
        ids=["pebble_ready", "config_changed", "update_status"],
    )
    def test_container_not_ready_goes_maintenance(self, context, disconnected_state, make_event):
        """Charm enters MaintenanceStatus when the container is not reachable."""
        state_in, container = disconnected_state
        state_out = context.run(make_event(context, container), state_in)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_PEBBLE_NOT_READY)


class TestPebbleLayer:
    def test_pebble_layer(self, context, state, container):
        """Pebble plan contains the nifi service and readiness check with expected config."""
        state_out = context.run(context.on.pebble_ready(container), state)
        plan = state_out.get_container(constants.CONTAINER_NAME).plan

        service = plan.services[constants.SERVICE_NAME]
        assert (service.command, service.startup, service.user, service.group) == (
            f"{constants.NIFI_HOME}/bin/nifi.sh run",
            "enabled",
            constants.WORKLOAD_USER,
            constants.WORKLOAD_GROUP,
        )

        check = plan.checks[constants.READY_CHECK_NAME]
        assert (check.level, check.threshold, check.http) == (
            ops.pebble.CheckLevel.READY,
            3,
            {"url": f"http://localhost:{constants.NIFI_PORT}/nifi"},
        )


class TestStorageDirs:
    def test_storage_dirs_created_on_first_boot(self, context, state, container):
        """Expected NiFi storage directories are created on the first reconcile."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        expected_dirs = [
            f"{constants.DATA_DIR}/database_repository",
            f"{constants.DATA_DIR}/flowfile_repository",
            f"{constants.DATA_DIR}/conf",
            constants.CONTENT_REPO_DIR,
            constants.PROVENANCE_REPO_DIR,
        ]
        assert all((root / d.lstrip("/")).is_dir() for d in expected_dirs)


class TestNifiProperties:
    def test_nifi_properties(self, context, state, container):
        """nifi.properties is pushed to the container with the expected content."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        expected = [
            f"nifi.web.http.port={constants.NIFI_PORT}",
            f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}",
            f"nifi.database.directory={constants.DATA_DIR}/database_repository",
            f"nifi.content.repository.directory.default={constants.CONTENT_REPO_DIR}",
            f"nifi.provenance.repository.directory.default={constants.PROVENANCE_REPO_DIR}",
            "nifi.security.allow.anonymous.authentication=true",
        ]
        assert all(line in content for line in expected)


class TestStateManagementXml:
    def test_state_management_xml(self, context, state, container):
        """state-management.xml is pushed with the local state directory on nifi-data storage."""
        state_out = context.run(context.on.pebble_ready(container), state)
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root / constants.STATE_MANAGEMENT_XML_PATH.lstrip("/")).read_text()
        assert f'<property name="Directory">{constants.DATA_DIR}/state/local</property>' in content


class TestReconcileIdempotency:
    def test_properties_rewritten_when_drift_detected(
        self, context, running_state, running_container
    ):
        """Second reconcile rewrites nifi.properties when on-disk content has drifted."""
        state_after_first = context.run(context.on.pebble_ready(running_container), running_state)

        # Simulate external drift on disk between reconciles.
        root = state_after_first.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        props_path = root / constants.NIFI_PROPERTIES_PATH.lstrip("/")
        props_path.write_text("# tampered content\n")

        state_after_second = context.run(context.on.config_changed(), state_after_first)

        root2 = state_after_second.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root2 / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert "# tampered content" not in content
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content

    def test_properties_unchanged_on_subsequent_reconcile(
        self, context, running_state, running_container
    ):
        """Second reconcile leaves nifi.properties untouched when content already matches."""
        state_after_first = context.run(context.on.pebble_ready(running_container), running_state)

        root = state_after_first.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        props_path = root / constants.NIFI_PROPERTIES_PATH.lstrip("/")
        original = props_path.read_text()

        state_after_second = context.run(context.on.config_changed(), state_after_first)

        root2 = state_after_second.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root2 / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert content == original


class TestFailureModes:
    @pytest.mark.parametrize(
        "target, side_effect",
        [
            (
                "properties_generator.NifiPropertiesGenerator.render_nifi_properties",
                RuntimeError("template error"),
            ),
            (
                "properties_generator.NifiPropertiesGenerator.render_state_management_xml",
                RuntimeError("template error"),
            ),
            (
                "ops.Container.push",
                ops.pebble.APIError({}, 500, "Internal Error", "push failed"),
            ),
        ],
        ids=["render_properties", "render_state_xml", "pebble_push"],
    )
    def test_config_write_failure_goes_blocked(
        self, context, state, container, target, side_effect
    ):
        """Charm enters BlockedStatus when config rendering or pushing fails."""
        with patch(target, side_effect=side_effect):
            state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_CONFIG_WRITE_FAILED)


class TestSensitivePropsKey:
    @pytest.mark.parametrize(
        "state_fixture, expected_msg",
        [
            ("state_no_secret", constants.MSG_SENSITIVE_KEY_MISSING),
            ("state_short_key", constants.MSG_SENSITIVE_KEY_TOO_SHORT),
            ("state_missing_field", constants.MSG_SENSITIVE_KEY_INVALID),
        ],
        ids=["no_config", "key_too_short", "field_missing"],
    )
    def test_invalid_secret_goes_blocked(
        self, request, context, container, state_fixture, expected_msg
    ):
        """Charm enters BlockedStatus for any invalid sensitive-props-key configuration."""
        state = request.getfixturevalue(state_fixture)
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.BlockedStatus(expected_msg)

    def test_unreadable_secret_goes_blocked(self, context, state, container):
        """Charm enters BlockedStatus when the configured secret cannot be read."""
        with patch(
            "ops.Model.get_secret",
            side_effect=ops.SecretNotFoundError("secret not found"),
        ):
            state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_SENSITIVE_KEY_INVALID)

    def test_key_unchanged_after_secret_reconfiguration(self, context, container):
        """Reconfiguring the secret does not change the key in nifi.properties.

        Key rotation is a future feature — the charm reads the key from the
        on-disk file (simulated via Mount) and ignores any secret updates,
        preventing NiFi from being unable to decrypt existing flows.
        """
        new_secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: "completely-different-new-key!"},
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            conf_dir = pathlib.Path(tmpdir)
            (conf_dir / "nifi.properties").write_text(
                f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}\n"
            )
            mounted_container = dataclasses.replace(
                container,
                mounts={
                    "conf": ops.testing.Mount(
                        location=f"{constants.NIFI_HOME}/conf",
                        source=conf_dir,
                    )
                },
            )
            state = ops.testing.State(
                containers=[mounted_container],
                secrets={new_secret},
                config={constants.SENSITIVE_PROPS_KEY_CONFIG: new_secret.id},
            )
            context.run(context.on.config_changed(), state)

            content = (conf_dir / "nifi.properties").read_text()
            assert f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}" in content
            assert "completely-different-new-key" not in content
