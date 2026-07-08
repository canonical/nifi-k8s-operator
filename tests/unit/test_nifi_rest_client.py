# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for NifiRestClient."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from nifi_rest_client import NifiClientError, NifiRestClient


class TestNifiRestClient:
    @pytest.fixture()
    def client(self):
        return NifiRestClient("http://localhost:8080")

    @pytest.fixture()
    def mock_session(self, client):
        with patch.object(client, "_session") as mock:
            yield mock

    def test_create_github_registry_client(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        mock_session.post.return_value.json.return_value = {"id": "new-id"}
        mock_session.post.return_value.raise_for_status = MagicMock()

        result = client.create_or_update_registry_client(
            name="test-client",
            repository_url="https://github.com/owner/repo.git",
            branch="main",
            token="tok",
        )

        assert result == {"id": "new-id"}
        payload = mock_session.post.call_args[1]["json"]
        assert payload["component"]["type"] == "org.apache.nifi.github.GitHubFlowRegistryClient"
        assert payload["component"]["properties"]["Repository Owner"] == "owner"
        assert payload["component"]["properties"]["Repository Name"] == "repo"

    def test_update_existing_registry_client(self, client, mock_session):
        existing = {"id": "abc", "revision": {"version": 3}, "component": {"name": "my-client"}}
        mock_session.get.return_value.json.return_value = {"registries": [existing]}
        mock_session.put.return_value.json.return_value = {"id": "abc"}
        mock_session.put.return_value.raise_for_status = MagicMock()

        client.create_or_update_registry_client(
            name="my-client",
            repository_url="https://github.com/owner/repo.git",
        )

        payload = mock_session.put.call_args[1]["json"]
        assert payload["revision"]["version"] == 3
        assert payload["component"]["id"] == "abc"

    def test_create_gitlab_registry_client(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        mock_session.post.return_value.json.return_value = {"id": "gl-id"}
        mock_session.post.return_value.raise_for_status = MagicMock()

        client.create_or_update_registry_client(
            name="gitlab-client",
            repository_url="https://gitlab.com/canonical/nifi-registry.git",
            branch="develop",
            token="glpat-tok",
        )

        payload = mock_session.post.call_args[1]["json"]
        assert payload["component"]["type"] == "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
        assert payload["component"]["properties"]["Repository Namespace"] == "canonical"
        assert payload["component"]["properties"]["Repository Name"] == "nifi-registry"
        assert payload["component"]["properties"]["GitLab API URL"] == "https://gitlab.com"
        assert payload["component"]["properties"]["Authentication Type"] == "ACCESS_TOKEN"
        assert payload["component"]["properties"]["Access Token"] == "glpat-tok"

    def test_delete_existing_client(self, client, mock_session):
        existing = {
            "id": "del-id",
            "revision": {"version": 1, "clientId": "c1"},
            "component": {"name": "to-delete"},
        }
        mock_session.get.return_value.json.return_value = {"registries": [existing]}
        mock_session.delete.return_value.raise_for_status = MagicMock()

        assert client.delete_registry_client("to-delete") is True
        mock_session.delete.assert_called_once()

    def test_delete_nonexistent_client_returns_false(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        assert client.delete_registry_client("missing") is False
        mock_session.delete.assert_not_called()

    def test_create_raises_nifi_client_error_on_http_error(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        mock_session.post.return_value.raise_for_status.side_effect = (
            requests.HTTPError("500")
        )

        with pytest.raises(NifiClientError, match="Registry client operation failed"):
            client.create_or_update_registry_client(
                name="err-client",
                repository_url="https://github.com/owner/repo.git",
            )

    def test_create_raises_on_invalid_url(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        with pytest.raises(ValueError, match="Cannot extract owner/repo"):
            client.create_or_update_registry_client(
                name="bad-url",
                repository_url="https://github.com/only-one-part",
            )
