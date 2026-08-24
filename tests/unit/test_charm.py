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
from conftest import _SENSITIVE_KEY_SECRET, SENSITIVE_KEY_VALUE, make_ingress_relation

import constants
from nifi_rest_client import NifiClientError, NifiConnectionError


def _state_with_secret_and_relations(container, relations):
    """Build a State with the sensitive-props-key secret and the given relations."""
    return ops.testing.State(
        containers=[container],
        relations=relations,
        secrets={_SENSITIVE_KEY_SECRET},
        config={constants.SENSITIVE_PROPS_KEY_CONFIG: _SENSITIVE_KEY_SECRET.id},
    )


class TestReconcile:
    def test_pebble_ready_reaches_active(self, context, state, container):
        """Charm reaches ActiveStatus when the container is ready and check passes."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_update_status_reaches_active_when_running(self, context, running_state):
        """update_status reports ActiveStatus when the service is up and check passes."""
        state_out = context.run(context.on.update_status(), running_state)
        assert state_out.unit_status == ops.ActiveStatus()

    @pytest.mark.parametrize(
        "make_event",
        [
            lambda ctx, c: ctx.on.pebble_ready(c),
            lambda ctx, c: ctx.on.config_changed(),
            lambda ctx, c: ctx.on.update_status(),
        ],
        ids=["pebble_ready", "config_changed", "update_status"],
    )
    def test_reconcile_events_reach_active(self, context, container, make_event):
        """Events that trigger _reconcile reach ActiveStatus when charm is healthy."""
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )
        state_out = context.run(make_event(context, container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    def test_secret_changed_reaches_active_with_running_service(
        self, context, rotation_container_factory
    ):
        """secret_changed triggers _reconcile and reaches Active with running service."""
        container = rotation_container_factory(SENSITIVE_KEY_VALUE)
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )
        state_out = context.run(context.on.secret_changed(secret), state)
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
        # override: replace drops the rock's baked service environment, so the
        # layer must redeclare it: JAVA_HOME (the rock does not register the
        # java alternative) and NIFI_LOG_DIR + NIFI_OVERRIDE_NIFIENV (logs to
        # /var/log/nifi).
        assert service.environment == {
            "JAVA_HOME": constants.JAVA_HOME,
            "NIFI_HOME": constants.NIFI_HOME,
            "NIFI_LOG_DIR": constants.NIFI_LOG_DIR,
            "NIFI_OVERRIDE_NIFIENV": "true",
        }

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
                "properties_manager.NifiPropertiesManager.render_nifi_properties",
                RuntimeError("template error"),
            ),
            (
                "properties_manager.NifiPropertiesManager.render_state_management_xml",
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
    def test_no_relation_active_with_secret(self, context, state, container):
        """Charm reaches ActiveStatus when no git-registry relation is present."""
        state_out = context.run(context.on.pebble_ready(container), state)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_relation_ready_active_with_secret(
        self, mock_create, context, state, container, git_registry_relation_ready
    ):
        """Charm reaches ActiveStatus when relation is ready and secret is set."""
        mock_create.return_value = {"id": "test-id"}
        state_in = dataclasses.replace(state, relations=frozenset([git_registry_relation_ready]))
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    @pytest.mark.parametrize(
        "relation_fixture",
        [None, "git_registry_relation_ready"],
        ids=["no_relation", "relation_ready"],
    )
    def test_active_with_secret_regardless_of_git_relation(
        self, mock_create, request, context, state, container, relation_fixture
    ):
        """Charm reaches ActiveStatus when sensitive-props-key is set, with or without git-registry."""  # noqa: E501
        mock_create.return_value = {"id": "test-id"}
        if relation_fixture:
            relation = request.getfixturevalue(relation_fixture)
            state_in = dataclasses.replace(state, relations=frozenset([relation]))
        else:
            state_in = state
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @pytest.mark.parametrize(
        "relation_fixture",
        [None, "git_registry_relation_ready"],
        ids=["no_relation", "relation_ready"],
    )
    def test_blocked_without_secret_regardless_of_git_relation(
        self, request, context, container, relation_fixture
    ):
        """Charm stays BlockedStatus without sensitive-props-key, with or without git-registry."""
        relations = []
        if relation_fixture:
            relations = [request.getfixturevalue(relation_fixture)]
        state_in = ops.testing.State(containers=[container], relations=relations)
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_SENSITIVE_KEY_MISSING)

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_not_ready_goes_waiting(
        self, mock_delete, context, state, container, git_registry_relation_empty
    ):
        """Charm enters WaitingStatus when relation is joined but provider data is absent."""
        mock_delete.return_value = False
        state_in = dataclasses.replace(state, relations=frozenset([git_registry_relation_empty]))
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.WaitingStatus(constants.MSG_GIT_REGISTRY_NOT_READY)

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_relation_ready_charm_reaches_active(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm reaches ActiveStatus and configures registry client when relation is ready."""
        mock_create.return_value = {"id": "test-id"}
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
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
        state_in = _state_with_secret_and_relations(
            container, [git_registry_relation_with_credentials]
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
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_gitlab])
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
        mock_create.side_effect = NifiConnectionError("NiFi API not reachable: Connection refused")
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_api_error_goes_blocked(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm enters BlockedStatus when REST API call fails."""
        mock_create.side_effect = NifiClientError(
            "Registry client operation failed: 404 Not Found"
        )
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_GIT_REGISTRY_API_ERROR)

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_value_error_goes_blocked_with_specific_message(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Charm enters BlockedStatus with specific ValueError message surfaced."""
        mock_create.side_effect = ValueError(
            "GitLab flow registry clients require a personal access token"
        )
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.BlockedStatus(
            "GitLab flow registry clients require a personal access token"
        )

    def test_ssh_auth_goes_blocked(self, context, container, git_registry_relation_ssh):
        """Charm enters BlockedStatus when git-integrator uses SSH authentication."""
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ssh])
        state_out = context.run(context.on.pebble_ready(container), state_in)
        assert state_out.unit_status == ops.BlockedStatus(
            constants.MSG_GIT_REGISTRY_SSH_UNSUPPORTED
        )

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_broken_deletes_and_stays_active(
        self, mock_delete, context, container, git_registry_relation_ready
    ):
        """Registry client is deleted when relation is broken; charm stays Active."""
        mock_delete.return_value = True
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_out = context.run(context.on.relation_broken(git_registry_relation_ready), state_in)
        assert state_out.unit_status == ops.ActiveStatus()
        mock_delete.assert_called_once_with(constants.FLOW_REGISTRY_CLIENT_NAME)

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_relation_broken_delete_failure_is_harmless(
        self, mock_delete, context, container, git_registry_relation_ready
    ):
        """Delete failure on relation-broken is logged but doesn't crash."""
        mock_delete.side_effect = NifiClientError(
            "Registry client delete failed: 500 Server Error"
        )
        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_out = context.run(context.on.relation_broken(git_registry_relation_ready), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.delete_registry_client")
    def test_no_relation_delete_connection_error_is_harmless(
        self, mock_delete, context, container
    ):
        """Delete failure due to NiFi being unreachable doesn't crash when no relation exists."""
        mock_delete.side_effect = NifiConnectionError("NiFi API not reachable: Connection refused")
        state_in = _state_with_secret_and_relations(container, [])
        state_out = context.run(context.on.config_changed(), state_in)
        assert state_out.unit_status == ops.ActiveStatus()

    @patch("nifi_rest_client.NifiRestClient.create_or_update_registry_client")
    def test_registry_client_updated_on_data_change(
        self, mock_create, context, container, git_registry_relation_ready
    ):
        """Registry client is updated idempotently when relation data changes."""
        mock_create.return_value = {"id": "test-id"}

        state_in = _state_with_secret_and_relations(container, [git_registry_relation_ready])
        state_after_first = context.run(context.on.pebble_ready(container), state_in)
        assert state_after_first.unit_status == ops.ActiveStatus()

        updated_relation = ops.testing.Relation(
            constants.GIT_REGISTRY_RELATION,
            remote_app_data={
                "repository-url": "https://github.com/example/nifi-flows-v2.git",
                "tracking-ref": "production",
            },
        )
        state_with_update = _state_with_secret_and_relations(container, [updated_relation])
        state_after_update = context.run(context.on.update_status(), state_with_update)
        assert state_after_update.unit_status == ops.ActiveStatus()
        assert mock_create.call_count == 2
        assert (
            mock_create.call_args.kwargs["repository_url"]
            == "https://github.com/example/nifi-flows-v2.git"
        )
        assert mock_create.call_args.kwargs["branch"] == "production"

    def test_relation_ready_connection_info_accessible(
        self, context, state, container, git_registry_relation_ready
    ):
        """Git connection info is accessible from the charm when the relation is ready."""
        state_in = dataclasses.replace(state, relations=frozenset([git_registry_relation_ready]))
        with context(context.on.pebble_ready(container), state_in) as mgr:
            charm = mgr.charm
            info = charm.git_registry.get_git_connection_information()
            assert info  # at least one relation has data
            model = next(iter(info.values()))
            assert model.repository_url == "https://github.com/example/nifi-flows.git"
            assert model.tracking_ref == "main"


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
        """Reconfiguring to a different secret via config-changed does not rotate the key.

        Rotation only happens via secret-changed on the tracked secret (see
        TestSensitivePropsKeyRotation). The charm reads the key from the on-disk
        file (simulated via Mount) and leaves it untouched.
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


class TestSensitivePropsKeyRotation:
    def test_rotation_updates_properties_and_goes_active(
        self, context, rotation_container_factory
    ):
        """A secret-changed event rotates the key on disk and ends ActiveStatus."""
        new_key = "rotated-key-1234567890"
        container = rotation_container_factory(SENSITIVE_KEY_VALUE)
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
            latest_content={constants.SENSITIVE_PROPS_KEY_FIELD: new_key},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )

        # Verify new key is NOT in properties before rotation
        content_before = container.mounts["conf"].source.joinpath("nifi.properties").read_text()
        assert f"nifi.sensitive.props.key={new_key}" not in content_before
        assert f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}" in content_before

        state_out = context.run(context.on.secret_changed(secret), state)

        assert state_out.unit_status == ops.ActiveStatus()
        content = container.mounts["conf"].source.joinpath("nifi.properties").read_text()
        assert f"nifi.sensitive.props.key={new_key}" in content

    def test_too_short_new_key_goes_blocked_without_writing(
        self, context, rotation_container_factory
    ):
        """A too-short replacement key blocks the unit and leaves the file untouched."""
        container = rotation_container_factory(SENSITIVE_KEY_VALUE)
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
            latest_content={constants.SENSITIVE_PROPS_KEY_FIELD: "short"},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )

        state_out = context.run(context.on.secret_changed(secret), state)

        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_SENSITIVE_KEY_TOO_SHORT)
        content = container.mounts["conf"].source.joinpath("nifi.properties").read_text()
        assert f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}" in content

    def test_rotation_stays_maintenance_until_ready_check_passes(
        self, context, rotation_container_factory
    ):
        """If NiFi is still booting after restart, the unit waits before going Active."""
        new_key = "rotated-key-1234567890"
        container = rotation_container_factory(
            SENSITIVE_KEY_VALUE, check_status=ops.pebble.CheckStatus.DOWN
        )
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
            latest_content={constants.SENSITIVE_PROPS_KEY_FIELD: new_key},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )

        state = context.run(context.on.secret_changed(secret), state)
        assert state.unit_status == ops.MaintenanceStatus(constants.MSG_NIFI_STARTING)

        booted_container = dataclasses.replace(
            next(iter(state.containers)),
            check_infos={
                ops.testing.CheckInfo(
                    constants.READY_CHECK_NAME,
                    level=ops.pebble.CheckLevel.READY,
                    status=ops.pebble.CheckStatus.UP,
                ),
            },
        )
        state = dataclasses.replace(state, containers=[booted_container])
        state_out = context.run(context.on.update_status(), state)

        assert state_out.unit_status == ops.ActiveStatus()

    def test_unreadable_secret_on_refresh_goes_blocked(self, context, rotation_container_factory):
        """A secret that fails to refresh blocks the unit without touching the file."""
        container = rotation_container_factory(SENSITIVE_KEY_VALUE)
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
            latest_content={constants.SENSITIVE_PROPS_KEY_FIELD: "rotated-key-1234567890"},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )

        with patch(
            "ops.Secret.get_content",
            side_effect=ops.SecretNotFoundError("secret not found"),
        ):
            state_out = context.run(context.on.secret_changed(secret), state)

        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_SENSITIVE_KEY_INVALID)
        content = container.mounts["conf"].source.joinpath("nifi.properties").read_text()
        assert f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}" in content

    def test_exec_failure_during_rotation_goes_blocked(self, context, rotation_container_factory):
        """A failing nifi.sh invocation blocks the unit without updating the file."""
        container = rotation_container_factory(SENSITIVE_KEY_VALUE, rotate_fails=True)
        secret = ops.testing.Secret(
            tracked_content={constants.SENSITIVE_PROPS_KEY_FIELD: SENSITIVE_KEY_VALUE},
            latest_content={constants.SENSITIVE_PROPS_KEY_FIELD: "rotated-key-1234567890"},
        )
        state = ops.testing.State(
            containers=[container],
            secrets={secret},
            config={constants.SENSITIVE_PROPS_KEY_CONFIG: secret.id},
        )

        state_out = context.run(context.on.secret_changed(secret), state)

        assert state_out.unit_status == ops.BlockedStatus(constants.MSG_KEY_ROTATION_FAILED)
        content = container.mounts["conf"].source.joinpath("nifi.properties").read_text()
        assert f"nifi.sensitive.props.key={SENSITIVE_KEY_VALUE}" in content


class TestIngressRelation:
    @staticmethod
    def _properties(context, state_out):
        root = state_out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
        return (root / constants.NIFI_PROPERTIES_PATH.lstrip("/")).read_text()

    def test_no_relation_leaves_proxy_properties_empty(self, context, state, container):
        """Without ingress, both proxy properties render empty."""
        state_out = context.run(context.on.pebble_ready(container), state)
        content = self._properties(context, state_out)
        assert "nifi.web.proxy.host=\n" in content
        assert "nifi.web.proxy.context.path=\n" in content

    def test_path_routing_sets_host_and_context_path(self, context, container):
        """A path-routing provider (traefik) populates both proxy properties."""
        relation = make_ingress_relation("http://10.0.0.1/mymodel-nifi-k8s")
        state = _state_with_secret_and_relations(container, [relation])

        state_out = context.run(context.on.pebble_ready(container), state)

        content = self._properties(context, state_out)
        assert "nifi.web.proxy.host=10.0.0.1:80\n" in content
        assert "nifi.web.proxy.context.path=/mymodel-nifi-k8s\n" in content

    def test_host_routing_leaves_context_path_empty(self, context, container):
        """A host-routing provider (nginx) sets only the proxy host."""
        relation = make_ingress_relation("http://nifi.example.com/")
        state = _state_with_secret_and_relations(container, [relation])

        state_out = context.run(context.on.pebble_ready(container), state)

        content = self._properties(context, state_out)
        assert "nifi.web.proxy.host=nifi.example.com:80\n" in content
        assert "nifi.web.proxy.context.path=\n" in content

    def test_https_url_without_port_defaults_to_443(self, context, container):
        """An https URL with no explicit port is advertised on 443."""
        relation = make_ingress_relation("https://nifi.example.com/")
        state = _state_with_secret_and_relations(container, [relation])

        state_out = context.run(context.on.pebble_ready(container), state)

        assert "nifi.web.proxy.host=nifi.example.com:443\n" in self._properties(context, state_out)

    def test_explicit_port_is_preserved(self, context, container):
        """A URL with an explicit port uses that port, not the scheme default."""
        relation = make_ingress_relation("http://nifi.example.com:8443/")
        state = _state_with_secret_and_relations(container, [relation])

        state_out = context.run(context.on.pebble_ready(container), state)

        assert "nifi.web.proxy.host=nifi.example.com:8443\n" in self._properties(
            context, state_out
        )

    def test_relation_without_url_goes_waiting(self, context, container):
        """Relation created but no URL published yet leaves the unit waiting."""
        relation = make_ingress_relation()
        state = _state_with_secret_and_relations(container, [relation])

        state_out = context.run(context.on.pebble_ready(container), state)

        assert state_out.unit_status == ops.WaitingStatus(constants.MSG_INGRESS_NOT_READY)

    def test_revoked_clears_proxy_properties(self, context, container):
        """Removing the ingress relation clears both proxy properties."""
        relation = make_ingress_relation("http://10.0.0.1/mymodel-nifi-k8s")
        state = _state_with_secret_and_relations(container, [relation])
        state_after = context.run(context.on.pebble_ready(container), state)
        assert "nifi.web.proxy.host=10.0.0.1:80\n" in self._properties(context, state_after)

        state_out = context.run(context.on.relation_broken(relation), state_after)

        content = self._properties(context, state_out)
        assert "nifi.web.proxy.host=\n" in content
        assert "nifi.web.proxy.context.path=\n" in content
        assert state_out.unit_status == ops.ActiveStatus()
