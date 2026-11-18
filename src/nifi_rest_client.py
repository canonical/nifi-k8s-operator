# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients.

Built on the standard library (urllib.request and json) rather than an
external client: the charm only needs four calls on one resource.
"""

import http.client
import json
import logging
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger(__name__)

REGISTRY_CLIENTS_PATH = "/controller/registry-clients"
# HTTP statuses that mean NiFi is still starting or briefly unavailable.
RETRYABLE_STATUSES = frozenset({503})


class NifiClientError(Exception):
    """Raised when NiFi REST API operations fail."""


class NifiConnectionError(NifiClientError):
    """Raised when the NiFi API is not reachable (transient — safe to retry)."""


class NifiRestClient:
    """Thin client for NiFi's /nifi-api/controller/registry-clients endpoints."""

    def __init__(self, base_url: str, timeout: float = 30):
        self._api_url = f"{base_url.rstrip('/')}/nifi-api"
        self._timeout = timeout

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

        # TODO: GitLab detection limitation
        # Currently detecting GitLab by checking if "gitlab" appears in the URL.
        # This fails for self-hosted GitLab instances with custom domains (e.g., git.company.com).
        # Tracked in: https://github.com/canonical/nifi-k8s-operator/issues/XX
        # Enhancement proposal for git-integrator: https://github.com/canonical/git-integrator/issues/XX
        # Once git-integrator provides explicit provider type information, update this logic.
        is_gitlab = "gitlab" in repository_url.lower()

        if is_gitlab:
            if not token:
                raise ValueError(
                    "GitLab flow registry clients require a personal access token; "
                    "reconfigure git-integrator with credentials (personal access token)"
                )
            component_type = "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
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

        existing = self._find_by_name(name)
        component: dict[str, Any] = {
            "name": name,
            "type": component_type,
            "properties": properties,
        }
        error = "Registry client operation failed"
        if existing:
            client_id = existing["id"]
            component["id"] = client_id
            logger.info("Updating registry client %s (id=%s)", name, client_id)
            return self._request(
                "PUT",
                f"{REGISTRY_CLIENTS_PATH}/{client_id}",
                error=error,
                body={"revision": existing["revision"], "component": component},
            )
        logger.info("Creating registry client %s", name)
        # NiFi rejects a create unless the revision is 0 and no ID is set.
        return self._request(
            "POST",
            REGISTRY_CLIENTS_PATH,
            error=error,
            body={"revision": {"version": 0}, "component": component},
        )

    def delete_registry_client(self, name: str) -> bool:
        """Delete a flow registry client by name. Returns True if deleted."""
        existing = self._find_by_name(name)
        if not existing:
            return False

        client_id = existing["id"]
        logger.info("Deleting registry client %s (id=%s)", name, client_id)
        self._request(
            "DELETE",
            f"{REGISTRY_CLIENTS_PATH}/{client_id}",
            error="Registry client delete failed",
            query={"version": existing["revision"]["version"]},
        )
        return True

    def _find_by_name(self, name: str) -> dict | None:
        """Find a flow registry client entity by name.

        Raises:
            NifiConnectionError: If the NiFi API is not reachable.
            NifiClientError: If the API call fails with other HTTP errors.
        """
        result = self._request(
            "GET", REGISTRY_CLIENTS_PATH, error="Failed to list registry clients"
        )
        return next(
            (
                c
                for c in result.get("registries") or []
                if (c.get("component") or {}).get("name") == name
            ),
            None,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        error: str,
        body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        """Call the NiFi API and return the decoded JSON response ({} if empty).

        Args:
            method: HTTP method.
            path: Path under /nifi-api.
            error: Message prefix for a failed call.
            body: JSON request body.
            query: Query parameters.

        Raises:
            NifiConnectionError: If NiFi can't be reached, times out, answers 503,
                or drops the connection mid-reply.
            NifiClientError: If NiFi answers with any other error, or invalid JSON.
        """
        url = f"{self._api_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Accept", "application/json")
        if data is not None:
            request.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            # NiFi explains most errors in a plain-text body.
            detail = e.read().decode(errors="replace").strip()[:500]
            message = f"HTTP {e.code} from {method} {path}: {detail}"
            if e.code in RETRYABLE_STATUSES:
                raise NifiConnectionError(f"NiFi API not reachable: {message}") from e
            raise NifiClientError(f"{error}: {message}") from e
        # HTTPException covers a reply cut off mid-body, e.g. NiFi restarting.
        except (
            urllib.error.URLError,
            http.client.HTTPException,
            TimeoutError,
            ConnectionError,
        ) as e:
            reason = getattr(e, "reason", e)
            raise NifiConnectionError(f"NiFi API not reachable: {reason}") from e

        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise NifiClientError(f"{error}: invalid JSON from {method} {path}") from e
