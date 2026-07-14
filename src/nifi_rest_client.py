# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients."""

import logging
from urllib.parse import urlsplit

import nipyapi
from nipyapi.nifi.apis import ControllerApi
from nipyapi.nifi.models import FlowRegistryClientDTO, FlowRegistryClientEntity, RevisionDTO
from nipyapi.nifi.rest import ApiException

logger = logging.getLogger(__name__)


class NifiClientError(Exception):
    """Raised when NiFi REST API operations fail."""


class NifiConnectionError(NifiClientError):
    """Raised when the NiFi API is not reachable (transient — safe to retry)."""


class NifiRestClient:
    """Thin client for NiFi's /nifi-api/controller/registry-clients endpoints."""

    def __init__(self, base_url: str, timeout: int = 30):
        nipyapi.config.nifi_config.host = f"{base_url.rstrip('/')}/nifi-api"
        nipyapi.config.nifi_config.connection_timeout = timeout
        self._controller = ControllerApi()

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

        try:
            existing = self._find_by_name(name)
            component = FlowRegistryClientDTO(
                name=name, type=component_type, properties=properties
            )
            if existing:
                component.id = existing.id
                logger.info("Updating registry client %s (id=%s)", name, existing.id)
                result = self._controller.update_flow_registry_client(
                    body=FlowRegistryClientEntity(revision=existing.revision, component=component),
                    id=existing.id,
                )
            else:
                logger.info("Creating registry client %s", name)
                result = self._controller.create_flow_registry_client(
                    body=FlowRegistryClientEntity(
                        revision=RevisionDTO(version=0), component=component
                    ),
                )
            return result.to_dict()
        except ApiException as e:
            if e.status in (0, 503):
                raise NifiConnectionError(f"NiFi API not reachable: {e}") from e
            raise NifiClientError(f"Registry client operation failed: {e}") from e

    def delete_registry_client(self, name: str) -> bool:
        """Delete a flow registry client by name. Returns True if deleted."""
        try:
            existing = self._find_by_name(name)
            if not existing:
                return False

            logger.info("Deleting registry client %s (id=%s)", name, existing.id)
            self._controller.delete_flow_registry_client(
                id=existing.id,
                version=existing.revision.version if existing.revision else 0,
            )
            return True
        except ApiException as e:
            if e.status in (0, 503):
                raise NifiConnectionError(f"NiFi API not reachable: {e}") from e
            raise NifiClientError(f"Registry client delete failed: {e}") from e

    def _find_by_name(self, name: str):
        result = self._controller.get_flow_registry_clients()
        registries = (result.registries or []) if result else []
        return next(
            (c for c in registries if c.component and c.component.name == name),
            None,
        )
