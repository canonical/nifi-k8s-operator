# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for nifi_rest_client helpers and NifiRestClient."""

from unittest.mock import MagicMock, patch

import pytest
import requests

from nifi_rest_client import (
    NifiRestClient,
    _build_github_properties,
    _build_gitlab_properties,
    _detect_registry_type,
    _parse_repo_url,
)


class TestParseRepoUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (
                "https://github.com/owner/repo.git",
                ("https://github.com", "owner", "repo"),
            ),
            (
                "https://github.com/owner/repo",
                ("https://github.com", "owner", "repo"),
            ),
            (
                "http://gitea-http:3000/nifi/nifi-flows.git",
                ("http://gitea-http:3000", "nifi", "nifi-flows"),
            ),
            (
                "https://gitlab.com/canonical/nifi-registry.git",
                ("https://gitlab.com", "canonical", "nifi-registry"),
            ),
        ],
    )
    def test_parses_correctly(self, url, expected):
        assert _parse_repo_url(url) == expected

    def test_raises_on_missing_owner_repo(self):
        with pytest.raises(ValueError, match="Cannot extract owner/repo"):
            _parse_repo_url("https://github.com/repo-only")


class TestDetectRegistryType:
    @pytest.mark.parametrize(
        "url, expected",
        [
            ("https://github.com/owner/repo.git", "github"),
            ("http://gitea-http:3000/nifi/flows.git", "github"),
            ("https://gitlab.com/canonical/project.git", "gitlab"),
            ("https://my-gitlab.internal/group/repo.git", "gitlab"),
        ],
    )
    def test_detection(self, url, expected):
        assert _detect_registry_type(url) == expected


class TestBuildProperties:
    def test_github_with_token(self):
        props = _build_github_properties("https://github.com", "owner", "repo", "main", "tok123")
        assert props["GitHub API URL"] == "https://api.github.com/"
        assert props["Repository Owner"] == "owner"
        assert props["Repository Name"] == "repo"
        assert props["Default Branch"] == "main"
        assert props["Authentication Type"] == "PERSONAL_ACCESS_TOKEN"
        assert props["Personal Access Token"] == "tok123"

    def test_github_no_token(self):
        props = _build_github_properties("https://github.com", "owner", "repo", "main", None)
        assert props["GitHub API URL"] == "https://api.github.com/"
        assert props["Authentication Type"] == "NONE"
        assert "Personal Access Token" not in props

    def test_self_hosted_api_url(self):
        """Self-hosted (Gitea, etc.) gets {host}/api/v1/ instead of api.github.com."""
        props = _build_github_properties("http://gitea-http:3000", "nifi", "flows", "main", "tok")
        assert props["GitHub API URL"] == "http://gitea-http:3000/api/v1/"

    def test_gitlab_with_token(self):
        props = _build_gitlab_properties(
            "https://gitlab.com", "canonical", "proj", "develop", "glpat-xyz"
        )
        assert props["GitLab API URL"] == "https://gitlab.com"
        assert props["Repository Namespace"] == "canonical"
        assert props["Repository Name"] == "proj"
        assert props["Default Branch"] == "develop"
        assert props["Authentication Type"] == "ACCESS_TOKEN"
        assert props["Access Token"] == "glpat-xyz"

    def test_gitlab_no_token(self):
        props = _build_gitlab_properties("https://gitlab.com", "canonical", "proj", "main", None)
        assert props["GitLab API URL"] == "https://gitlab.com"
        assert props["Authentication Type"] == "NONE"
        assert "Access Token" not in props


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

    def test_create_raises_on_http_error(self, client, mock_session):
        mock_session.get.return_value.json.return_value = {"registries": []}
        mock_session.post.return_value.raise_for_status.side_effect = requests.HTTPError("500")

        with pytest.raises(requests.HTTPError):
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
