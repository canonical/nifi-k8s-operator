# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients."""

import logging

import requests

logger = logging.getLogger(__name__)

# Registry type → (NiFi component class, property key mapping)
_REGISTRY_CONFIGS: dict[str, tuple[str, dict[str, str]]] = {
    "github": (
        "org.apache.nifi.github.GitHubFlowRegistryClient",
        {
            "url": "github-repository-url",
            "branch": "github-branch",
            "username": "github-authentication-username",
            "token": "github-authentication-token",
        },
    ),
    "gitlab": (
        "org.apache.nifi.gitlab.GitLabFlowRegistryClient",
        {
            "url": "gitlab-project-url",
            "branch": "gitlab-branch",
            "username": "gitlab-username",
            "token": "gitlab-personal-access-token",
        },
    ),
}


def _detect_registry_type(repository_url: str) -> str:
    """Return 'github' or 'gitlab' based on the repository URL."""
    url_lower = repository_url.lower()
    if "github.com" in url_lower:
        return "github"
    if "gitlab" in url_lower:
        return "gitlab"
    raise ValueError(
        f"Cannot determine registry type from URL: {repository_url}. "
        "Supported providers: GitHub, GitLab"
    )


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
        username: str | None = None,
        token: str | None = None,
    ) -> dict:
        """Create or update a flow registry client by name.

        Raises requests.HTTPError on failure, ValueError for unsupported URLs.
        """
        registry_type = _detect_registry_type(repository_url)
        component_type, keys = _REGISTRY_CONFIGS[registry_type]

        properties: dict[str, str] = {keys["url"]: repository_url, keys["branch"]: branch}
        if username and token:
            properties[keys["username"]] = username
            properties[keys["token"]] = token

        existing = self._find_by_name(name)
        if existing:
            version = existing.get("revision", {}).get("version", 0)
            payload = self._payload(name, component_type, properties, version)
            logger.info("Updating registry client %s (id=%s)", name, existing["id"])
            resp = self._session.put(
                f"{self._api}/{existing['id']}", json=payload, timeout=self._timeout
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
    def _payload(name: str, component_type: str, properties: dict, version: int = 0) -> dict:
        return {
            "revision": {"version": version},
            "component": {"name": name, "type": component_type, "properties": properties},
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
