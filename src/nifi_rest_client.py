# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients."""

import logging
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)


def _parse_repo_url(repository_url: str) -> tuple[str, str, str]:
    """Parse a git repository URL into (api_base_url, owner, repo_name).

    Supports URLs like:
      http://gitea-http:3000/nifi/nifi-flows.git
      https://github.com/owner/repo.git
      https://gitlab.com/group/project.git
    """
    parsed = urlparse(repository_url)
    path = parsed.path.rstrip("/")
    if path.endswith(".git"):
        path = path[:-4]

    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError(
            f"Cannot extract owner/repo from URL: {repository_url}. "
            "Expected format: https://host/owner/repo[.git]"
        )

    owner = parts[-2]
    repo_name = parts[-1]
    api_base_url = f"{parsed.scheme}://{parsed.netloc}"
    return api_base_url, owner, repo_name


def _detect_registry_type(repository_url: str) -> str:
    """Return 'github' or 'gitlab' based on the repository URL.

    Gitea-hosted repos default to github-compatible since Gitea implements
    the same Git HTTP protocol used by NiFi's GitHubFlowRegistryClient.
    """
    url_lower = repository_url.lower()
    if "gitlab" in url_lower:
        return "gitlab"
    # GitHub, Gitea, and other Git-compatible hosts use the GitHub registry client
    return "github"


def _build_github_properties(
    api_base_url: str, owner: str, repo_name: str, branch: str, token: str | None
) -> dict[str, str]:
    """Build NiFi GitHubFlowRegistryClient properties."""
    props: dict[str, str] = {
        "GitHub API URL": f"{api_base_url}/api/v1/",
        "Repository Owner": owner,
        "Repository Name": repo_name,
        "Default Branch": branch,
    }
    if token:
        props["Authentication Type"] = "PERSONAL_ACCESS_TOKEN"
        props["Personal Access Token"] = token
    else:
        props["Authentication Type"] = "NONE"
    return props


def _build_gitlab_properties(
    api_base_url: str, owner: str, repo_name: str, branch: str, token: str | None
) -> dict[str, str]:
    """Build NiFi GitLabFlowRegistryClient properties."""
    props: dict[str, str] = {
        "GitLab API URL": f"{api_base_url}/api/v4/",
        "Project Path": f"{owner}/{repo_name}",
        "Default Branch": branch,
    }
    if token:
        props["Personal Access Token"] = token
    return props


class NifiRestClient:
    """Thin client for NiFi's /nifi-api/controller/registry-clients endpoints."""

    def __init__(self, base_url: str, timeout: int = 30):
        self._api = f"{base_url.rstrip('/')}/nifi-api/controller/registry-clients"
        self._timeout = timeout
        self._session = requests.Session()

    def create_or_update_registry_client(
        self,
        name: str,
        repository_url: str,
        branch: str = "main",
        token: str | None = None,
    ) -> dict:
        """Create or update a flow registry client by name.

        Raises requests.HTTPError on failure, ValueError for unsupported URLs.
        """
        registry_type = _detect_registry_type(repository_url)
        api_base_url, owner, repo_name = _parse_repo_url(repository_url)

        if registry_type == "gitlab":
            component_type = "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
            properties = _build_gitlab_properties(
                api_base_url, owner, repo_name, branch, token
            )
        else:
            component_type = "org.apache.nifi.github.GitHubFlowRegistryClient"
            properties = _build_github_properties(
                api_base_url, owner, repo_name, branch, token
            )

        existing = self._find_by_name(name)
        if existing:
            version = existing.get("revision", {}).get("version", 0)
            client_id = existing["id"]
            payload = self._payload(name, component_type, properties, version, client_id)
            logger.info("Updating registry client %s (id=%s)", name, client_id)
            resp = self._session.put(
                f"{self._api}/{client_id}", json=payload, timeout=self._timeout
            )
        else:
            payload = self._payload(name, component_type, properties)
            logger.info("Creating registry client %s", name)
            resp = self._session.post(self._api, json=payload, timeout=self._timeout)

        resp.raise_for_status()
        return resp.json()

    def delete_registry_client(self, name: str) -> bool:
        """Delete a flow registry client by name. Returns True if deleted."""
        existing = self._find_by_name(name)
        if not existing:
            return False

        rev = existing.get("revision", {})
        params = {"version": rev.get("version", 0), "clientId": rev.get("clientId", "")}
        logger.info("Deleting registry client %s (id=%s)", name, existing["id"])
        self._session.delete(
            f"{self._api}/{existing['id']}", params=params, timeout=self._timeout
        ).raise_for_status()
        return True

    @staticmethod
    def _payload(
        name: str,
        component_type: str,
        properties: dict,
        version: int = 0,
        client_id: str | None = None,
    ) -> dict:
        component: dict = {"name": name, "type": component_type, "properties": properties}
        if client_id:
            component["id"] = client_id
        return {
            "revision": {"version": version},
            "component": component,
        }

    def _find_by_name(self, name: str) -> dict | None:
        resp = self._session.get(self._api, timeout=self._timeout)
        resp.raise_for_status()
        return next(
            (
                c
                for c in resp.json().get("registries", [])
                if c.get("component", {}).get("name") == name
            ),
            None,
        )
