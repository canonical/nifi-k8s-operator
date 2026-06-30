# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NiFi K8s charm."""

from unittest.mock import patch

import ops
import ops.testing
import pytest
import requests

import constants


class TestReconcile:
    def test_pebble_ready_reaches_active(self, context, state, container):
        """Charm reaches ActiveStatus when the container is ready and check passes."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_update_status_reaches_active_when_running(self, context, running_container):
        """update_status reports ActiveStatus when the service is up and check passes."""
        state_in = ops.testing.State(containers=[running_container])
        state_out = context.run(context.on.update_status(), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_pebble_ready_maintenance_while_starting(self, context, booting_container):
        """Charm stays in MaintenanceStatus while NiFi is still booting (check DOWN)."""
        state_in = ops.testing.State(containers=[booting_container])
        state_out = context.run(context.on.pebble_ready(booting_container), state_in)
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
            {"url": f"http://localhost:{constants.NIFI_PORT}/nifi/"},
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
            f"nifi.sensitive.props.key={constants.SENSITIVE_PROPS_KEY}",
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
    def test_properties_rewritten_when_drift_detected(self, context, running_container):
        """Second reconcile rewrites nifi.properties when on-disk content has drifted."""
        state_in = ops.testing.State(containers=[running_container])
        state_after_first = context.run(context.on.pebble_ready(running_container), state_in)

        # Simulate external drift on disk between reconciles.
        root = state_after_first.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        props_path = root / constants.NIFI_PROPERTIES_PATH.lstrip("/")
        props_path.write_text("# tampered content\n")

        state_after_second = context.run(context.on.config_changed(), state_after_first)

        root2 = state_after_second.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        content = (root2 / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()
        assert "# tampered content" not in content
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content

    def test_properties_unchanged_on_subsequent_reconcile(self, context, running_container):
        """Second reconcile leaves nifi.properties untouched when content already matches."""
        state_in = ops.testing.State(containers=[running_container])
        state_after_first = context.run(context.on.pebble_ready(running_container), state_in)

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


class TestGitRegistryRelation:
    def test_no_relation_charm_reaches_active(self, context, container):
        """Charm reaches ActiveStatus when no git-registry relation is present (optional)."""
        state_in = ops.testing.State(containers=[container])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_not_ready_goes_waiting(
        self, mock_delete, context, container, git_registry_relation_empty
    ):
        """Charm enters WaitingStatus when relation is joined but provider data is absent."""
        mock_delete.return_value = False
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_empty],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.WaitingStatus(constants.MSG_GIT_REGISTRY_NOT_READY)
        mock_delete.assert_called_once()

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_relation_ready_charm_reaches_active(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm reaches ActiveStatus and configures registry client when relation is ready."""
        mock_create.return_value = {"id": "test-id"}
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()
        mock_create.assert_called_once_with(
            name=constants.FLOW_REGISTRY_CLIENT_NAME,
            repository_url="https://github.com/example/nifi-flows.git",
            branch="main",
            token=None,
        )

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_relation_with_credentials(
        self, mock_create, context, container, git_registry_relation_with_credentials
    ):
        """Registry client is created with auth credentials when provided."""
        mock_create.return_value = {"id": "test-id"}
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_with_credentials],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()
        mock_create.assert_called_once_with(
            name=constants.FLOW_REGISTRY_CLIENT_NAME,
            repository_url="https://github.com/example/nifi-flows.git",
            branch="develop",
            token="ghp_test_token_123",
        )

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_relation_gitlab(self, mock_create, context, container, git_registry_relation_gitlab):
        """Registry client works with GitLab repositories."""
        mock_create.return_value = {"id": "test-id"}
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_gitlab],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()
        mock_create.assert_called_once_with(
            name=constants.FLOW_REGISTRY_CLIENT_NAME,
            repository_url="https://gitlab.com/canonical/nifi-registry.git",
            branch="main",
            token=None,
        )

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_connection_error_goes_maintenance(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm enters MaintenanceStatus when NiFi API is not yet reachable."""
        mock_create.side_effect = requests.ConnectionError("Connection refused")
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_api_error_goes_blocked(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm enters BlockedStatus when REST API call fails."""
        mock_create.side_effect = requests.HTTPError("404 Not Found")
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_GIT_REGISTRY_API_ERROR)

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_broken_deletes_and_stays_active(
        self, mock_delete, context, container, git_registry_relation_ready
    ):
        """Registry client is deleted when relation is broken; charm stays Active."""
        mock_delete.return_value = True
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_out = context.run(context.on.relation_broken(git_registry_relation_ready), state_in)
        assert state_out.unit_status == ops.ActiveStatus()
        mock_delete.assert_called_once_with(constants.FLOW_REGISTRY_CLIENT_NAME)

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_broken_delete_failure_is_harmless(
        self, mock_delete, context, container, git_registry_relation_ready
    ):
        """Delete failure on relation-broken is logged but doesn't crash."""
        mock_delete.side_effect = requests.HTTPError("500 Server Error")
        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_out = context.run(context.on.relation_broken(git_registry_relation_ready), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_registry_client_updated_on_data_change(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Registry client is updated idempotently when relation data changes."""
        mock_create.return_value = {"id": "test-id"}

        state_in = ops.testing.State(
            containers=[container],
            relations=[git_registry_relation_ready],
        )
        state_after_first = context.run(context.on.pebble_ready(container), state_in)
        assert state_after_first.unit_status == ops.ActiveStatus()

        updated_relation = ops.testing.Relation(
            constants.GIT_REGISTRY_RELATION,
            remote_app_data={
                "repository-url": "https://github.com/example/nifi-flows-v2.git",
                "tracking-ref": "production",
            },
        )
        state_with_update = ops.testing.State(
            containers=[container],
            relations=[updated_relation],
        )
        state_after_update = context.run(context.on.update_status(), state_with_update)
        assert state_after_update.unit_status == ops.ActiveStatus()
        assert mock_create.call_count == 2
        assert (
            mock_create.call_args.kwargs["repository_url"]
            == "https://github.com/example/nifi-flows-v2.git"
        )
        assert mock_create.call_args.kwargs["branch"] == "production"
