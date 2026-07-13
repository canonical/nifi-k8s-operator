# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients."""

import logging
from urllib.parse import urlsplit

import requests

logger = logging.getLogger(__name__)


class NifiClientError(Exception):
    """Raised when NiFi REST API operations fail."""


class NifiConnectionError(NifiClientError):
    """Raised when the NiFi API is not reachable (transient — safe to retry)."""


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

        Raises:
            NifiClientError: if the NiFi API call fails.
            ValueError: if the repository URL cannot be parsed or GitLab is
                configured without a token.
        """
        # Parse URL using urlsplit to determine registry type, api_base_url,
        # owner, and repo_name.
        parsed = urlsplit(repository_url)
        path = parsed.path.rstrip("/").removesuffix(".git")
        parts = [p for p in path.split("/") if p]
        if len(parts) < 2:
            raise ValueError(
                f"Cannot extract owner/repo from URL: {repository_url}. "
                "Expected format: https://host/owner/repo[.git]"
            )
        owner, repo_name = parts[-2], parts[-1]
        api_base_url = f"{parsed.scheme}://{parsed.netloc}"

        is_gitlab = "gitlab" in repository_url.lower()

        if is_gitlab:
            if not token:
                raise ValueError(
                    "GitLab flow registry clients require a personal access token; "
                    "reconfigure git-integrator with credentials (personal access token)"
                )
            component_type = "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
            # NiFi appends /api/v4/ internally — provide the base URL only.
            properties: dict[str, str] = {
                "GitLab API URL": api_base_url,
                "Repository Namespace": owner,
                "Repository Name": repo_name,
                "Default Branch": branch,
                "Authentication Type": "ACCESS_TOKEN",
                "Access Token": token,
            }
        else:
            component_type = "org.apache.nifi.github.GitHubFlowRegistryClient"
            # github.com uses https://api.github.com/; self-hosted (Gitea, etc.)
            # uses {host}/api/v1/.
            api_url = (
                "https://api.github.com/"
                if "github.com" in api_base_url.lower()
                else f"{api_base_url}/api/v1/"
            )
            properties = {
                "GitHub API URL": api_url,
                "Repository Owner": owner,
                "Repository Name": repo_name,
                "Default Branch": branch,
            }
            if token:
                properties["Authentication Type"] = "PERSONAL_ACCESS_TOKEN"
                properties["Personal Access Token"] = token
            else:
                properties["Authentication Type"] = "NONE"

        try:
            existing = self._find_by_name(name)
            if existing:
                version = existing.get("revision", {}).get("version", 0)
                client_id = existing["id"]
                component: dict = {
                    "id": client_id,
                    "name": name,
                    "type": component_type,
                    "properties": properties,
                }
                logger.info("Updating registry client %s (id=%s)", name, client_id)
                resp = self._session.put(
                    f"{self._api}/{client_id}",
                    json={"revision": {"version": version}, "component": component},
                    timeout=self._timeout,
                )
            else:
                logger.info("Creating registry client %s", name)
                resp = self._session.post(
                    self._api,
                    json={
                        "revision": {"version": 0},
                        "component": {
                            "name": name,
                            "type": component_type,
                            "properties": properties,
                        },
                    },
                    timeout=self._timeout,
                )
            resp.raise_for_status()
            return resp.json()
        except requests.ConnectionError as e:
            raise NifiConnectionError(f"NiFi API not reachable: {e}") from e
        except requests.RequestException as e:
            raise NifiClientError(f"Registry client operation failed: {e}") from e

    def delete_registry_client(self, name: str) -> bool:
        """Delete a flow registry client by name. Returns True if deleted."""
        try:
            existing = self._find_by_name(name)
            if not existing:
                return False

            rev = existing.get("revision", {})
            params = {
                "version": rev.get("version", 0),
                "clientId": rev.get("clientId", ""),
            }
            logger.info("Deleting registry client %s (id=%s)", name, existing["id"])
            self._session.delete(
                f"{self._api}/{existing['id']}", params=params, timeout=self._timeout
            ).raise_for_status()
            return True
        except requests.ConnectionError as e:
            raise NifiConnectionError(f"NiFi API not reachable: {e}") from e
        except requests.RequestException as e:
            raise NifiClientError(f"Registry client delete failed: {e}") from e

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
