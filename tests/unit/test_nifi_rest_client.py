# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for NifiRestClient."""

from unittest.mock import MagicMock, patch

import pytest
from nipyapi.nifi.models import FlowRegistryClientDTO, FlowRegistryClientEntity, RevisionDTO
from nipyapi.nifi.rest import ApiException

from nifi_rest_client import NifiClientError, NifiConnectionError, NifiRestClient


def _make_entity(name, client_id, revision_version):
    """Build a mock FlowRegistryClientEntity as _find_by_name returns."""
    entity = MagicMock(spec=FlowRegistryClientEntity)
    entity.id = client_id
    entity.component = MagicMock(spec=FlowRegistryClientDTO)
    entity.component.name = name
    entity.revision = MagicMock(spec=RevisionDTO)
    entity.revision.version = revision_version
    return entity


class TestNifiRestClient:
    @pytest.fixture()
    def client(self):
        return NifiRestClient("http://localhost:8080")

    @pytest.fixture()
    def mock_controller(self, client):
        with patch.object(client, "_controller") as mock:
            mock.get_flow_registry_clients.return_value.registries = []
            yield mock

    def test_create_github_registry_client(self, client, mock_controller):
        created = MagicMock()
        created.to_dict.return_value = {"id": "new-id"}
        mock_controller.create_flow_registry_client.return_value = created

        result = client.create_or_update_registry_client(
            name="test-client",
            repository_url="https://github.com/owner/repo.git",
            branch="main",
            token="tok",
        )

        assert result == {"id": "new-id"}
        body = mock_controller.create_flow_registry_client.call_args[1]["body"]
        assert body.component.type == "org.apache.nifi.github.GitHubFlowRegistryClient"
        assert body.component.properties["Repository Owner"] == "owner"
        assert body.component.properties["Repository Name"] == "repo"

    def test_update_existing_registry_client(self, client, mock_controller):
        mock_controller.get_flow_registry_clients.return_value.registries = [
            _make_entity("my-client", "abc", 3)
        ]
        updated = MagicMock()
        updated.to_dict.return_value = {"id": "abc"}
        mock_controller.update_flow_registry_client.return_value = updated

        client.create_or_update_registry_client(
            name="my-client",
            repository_url="https://github.com/owner/repo.git",
        )

        body = mock_controller.update_flow_registry_client.call_args[1]["body"]
        assert body.revision.version == 3
        assert body.component.id == "abc"

    def test_create_gitlab_registry_client(self, client, mock_controller):
        created = MagicMock()
        created.to_dict.return_value = {"id": "gl-id"}
        mock_controller.create_flow_registry_client.return_value = created

        client.create_or_update_registry_client(
            name="gitlab-client",
            repository_url="https://gitlab.com/canonical/nifi-registry.git",
            branch="develop",
            token="glpat-tok",
        )

        body = mock_controller.create_flow_registry_client.call_args[1]["body"]
        assert body.component.type == "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
        assert body.component.properties["Repository Namespace"] == "canonical"
        assert body.component.properties["Repository Name"] == "nifi-registry"
        assert body.component.properties["GitLab API URL"] == "https://gitlab.com"
        assert body.component.properties["Authentication Type"] == "ACCESS_TOKEN"
        assert body.component.properties["Access Token"] == "glpat-tok"

    def test_delete_existing_client(self, client, mock_controller):
        mock_controller.get_flow_registry_clients.return_value.registries = [
            _make_entity("to-delete", "del-id", 1)
        ]

        assert client.delete_registry_client("to-delete") is True
        mock_controller.delete_flow_registry_client.assert_called_once_with(id="del-id", version=1)

    def test_delete_nonexistent_client_returns_false(self, client, mock_controller):
        assert client.delete_registry_client("missing") is False
        mock_controller.delete_flow_registry_client.assert_not_called()

    def test_create_raises_nifi_client_error_on_http_error(self, client, mock_controller):
        mock_controller.create_flow_registry_client.side_effect = ApiException(status=500)

        with pytest.raises(NifiClientError, match="Registry client operation failed"):
            client.create_or_update_registry_client(
                name="err-client",
                repository_url="https://github.com/owner/repo.git",
            )

    def test_create_raises_nifi_connection_error_on_connection_error(
        self, client, mock_controller
    ):
        mock_controller.get_flow_registry_clients.side_effect = ApiException(status=0)

        with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
            client.create_or_update_registry_client(
                name="err-client",
                repository_url="https://github.com/owner/repo.git",
            )

    def test_create_raises_on_invalid_url(self, client, mock_controller):
        with pytest.raises(ValueError, match="Cannot extract owner/repo"):
            client.create_or_update_registry_client(
                name="bad-url",
                repository_url="https://github.com/only-one-part",
            )
