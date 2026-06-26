# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients."""

import json
import logging
from enum import Enum
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class RegistryType(str, Enum):
    """Supported git provider types for NiFi flow registry."""

    GITHUB = "github"
    GITLAB = "gitlab"


class NifiRestClient:
    """Client for interacting with NiFi REST API."""

    # NiFi component types for flow registry clients
    GITHUB_REGISTRY_TYPE = "org.apache.nifi.github.GitHubFlowRegistryClient"
    GITLAB_REGISTRY_TYPE = "org.apache.nifi.gitlab.GitLabFlowRegistryClient"

    def __init__(self, base_url: str, timeout: int = 30):
        """Initialize the NiFi REST client.

        Args:
            base_url: Base URL for NiFi API (e.g., http://localhost:8080)
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()

    def _detect_registry_type(self, repository_url: str) -> RegistryType:
        """Detect git provider type from repository URL.

        Args:
            repository_url: Git repository URL

        Returns:
            RegistryType enum value

        Raises:
            ValueError: If provider cannot be determined
        """
        url_lower = repository_url.lower()
        if "github.com" in url_lower:
            return RegistryType.GITHUB
        elif "gitlab.com" in url_lower or "gitlab" in url_lower:
            return RegistryType.GITLAB
        else:
            raise ValueError(
                f"Cannot determine registry type from URL: {repository_url}. "
                "Supported providers: GitHub, GitLab"
            )

    def _build_registry_client_payload(
        self,
        name: str,
        registry_type: RegistryType,
        repository_url: str,
        branch: str,
        username: Optional[str] = None,
        token: Optional[str] = None,
    ) -> dict:
        """Build the payload for creating/updating a flow registry client.

        Args:
            name: Display name for the registry client
            registry_type: Type of registry (GitHub or GitLab)
            repository_url: Git repository URL
            branch: Git branch to track
            username: Authentication username (optional)
            token: Authentication token (optional)

        Returns:
            JSON payload dict for POST/PUT request
        """
        if registry_type == RegistryType.GITHUB:
            component_type = self.GITHUB_REGISTRY_TYPE
            properties = {
                "github-repository-url": repository_url,
                "github-branch": branch,
            }
            if username and token:
                properties["github-authentication-username"] = username
                properties["github-authentication-token"] = token
        elif registry_type == RegistryType.GITLAB:
            component_type = self.GITLAB_REGISTRY_TYPE
            properties = {
                "gitlab-project-url": repository_url,
                "gitlab-branch": branch,
            }
            if username and token:
                properties["gitlab-username"] = username
                properties["gitlab-personal-access-token"] = token
        else:
            raise ValueError(f"Unsupported registry type: {registry_type}")

        return {
            "revision": {"version": 0},
            "component": {
                "name": name,
                "type": component_type,
                "properties": properties,
            },
        }

    def list_registry_clients(self) -> list[dict]:
        """List all flow registry clients.

        Returns:
            List of registry client dicts

        Raises:
            requests.HTTPError: If the request fails
        """
        url = f"{self.base_url}/nifi-api/controller/registry-clients"
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        data = response.json()
        return data.get("registries", [])

    def get_registry_client_by_name(self, name: str) -> Optional[dict]:
        """Find a registry client by name.

        Args:
            name: Display name of the registry client

        Returns:
            Registry client dict if found, None otherwise

        Raises:
            requests.HTTPError: If the list request fails
        """
        clients = self.list_registry_clients()
        for client in clients:
            component = client.get("component", {})
            if component.get("name") == name:
                return client
        return None

    def create_or_update_registry_client(
        self,
        name: str,
        repository_url: str,
        branch: str = "main",
        username: Optional[str] = None,
        token: Optional[str] = None,
    ) -> dict:
        """Create or update a flow registry client.

        If a client with the same name exists, it will be updated.
        Otherwise, a new client will be created.

        Args:
            name: Display name for the registry client
            repository_url: Git repository URL
            branch: Git branch to track
            username: Authentication username (optional)
            token: Authentication token (optional)

        Returns:
            Created/updated registry client dict

        Raises:
            requests.HTTPError: If the request fails
            ValueError: If repository URL is invalid
        """
        registry_type = self._detect_registry_type(repository_url)
        payload = self._build_registry_client_payload(
            name, registry_type, repository_url, branch, username, token
        )

        existing = self.get_registry_client_by_name(name)
        if existing:
            # Update existing client
            client_id = existing.get("id")
            current_revision = existing.get("revision", {}).get("version", 0)
            payload["revision"]["version"] = current_revision

            url = f"{self.base_url}/nifi-api/controller/registry-clients/{client_id}"
            logger.info(
                "Updating existing registry client: %s (id=%s, revision=%s)",
                name,
                client_id,
                current_revision,
            )
            response = self.session.put(url, json=payload, timeout=self.timeout)
        else:
            # Create new client
            url = f"{self.base_url}/nifi-api/controller/registry-clients"
            logger.info("Creating new registry client: %s", name)
            response = self.session.post(url, json=payload, timeout=self.timeout)

        response.raise_for_status()
        return response.json()

    def delete_registry_client(self, name: str) -> bool:
        """Delete a flow registry client by name.

        Args:
            name: Display name of the registry client to delete

        Returns:
            True if deleted, False if not found

        Raises:
            requests.HTTPError: If the delete request fails
        """
        existing = self.get_registry_client_by_name(name)
        if not existing:
            logger.info("Registry client not found: %s (already deleted)", name)
            return False

        client_id = existing.get("id")
        revision_version = existing.get("revision", {}).get("version", 0)
        client_token = existing.get("revision", {}).get("clientId", "")

        url = f"{self.base_url}/nifi-api/controller/registry-clients/{client_id}"
        params = {"version": revision_version, "clientId": client_token}

        logger.info("Deleting registry client: %s (id=%s)", name, client_id)
        response = self.session.delete(url, params=params, timeout=self.timeout)
        response.raise_for_status()
        return True
