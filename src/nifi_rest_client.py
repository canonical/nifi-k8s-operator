# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi REST API client for managing flow registry clients.

Built on the standard library (urllib.request and json)
"""

import http.client
import json
import logging
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

logger = logging.getLogger(__name__)

REGISTRY_CLIENTS_PATH = "/controller/registry-clients"
# HTTP statuses that mean NiFi is still starting or briefly unavailable
# (502/504 if a proxy sits in front of it).
RETRYABLE_STATUSES = frozenset({502, 503, 504})
# HTTP statuses that mean NiFi doesn't accept the charm's identity.
AUTH_STATUSES = frozenset({401, 403})


class NifiClientError(Exception):
    """Raised when NiFi REST API operations fail."""


class NifiConnectionError(NifiClientError):
    """Raised when the NiFi API is not reachable (transient — safe to retry)."""


class NifiAuthError(NifiClientError):
    """Raised when the TLS check fails or NiFi rejects the charm's identity.

    Retrying doesn't help: the certificates or NiFi's policies need fixing.
    """


@dataclass(frozen=True)
class ClientTls:
    """Files the charm uses to call NiFi over HTTPS.

    Attributes:
        ca: CA bundle that verifies NiFi's server certificate.
        cert: The charm's client certificate (with intermediates).
        key: The private key for `cert`.
    """

    ca: Path
    cert: Path
    key: Path


class NifiRestClient:
    """Thin client for NiFi's /nifi-api/controller/registry-clients endpoints.

    Without `tls`, it calls NiFi over plain HTTP. With `tls`, it verifies NiFi's
    certificate and presents the charm's client certificate. The files are read
    on every call, so renewed certificates are used without recreating the client.
    """

    def __init__(self, base_url: str, timeout: float = 30, tls: ClientTls | None = None):
        self._api_url = f"{base_url.rstrip('/')}/nifi-api"
        self._timeout = timeout
        self._tls = tls

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

        # TODO: GitLab is detected from the host name only, so self-hosted GitLab
        # on a custom domain (e.g. git.company.com) is treated as GitHub. Fixing it
        # needs git-integrator to send the provider type.
        # Tracked in: https://github.com/canonical/nifi-k8s-operator/issues/35
        is_gitlab = "gitlab" in (parsed.hostname or "")

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
            NifiConnectionError: If NiFi can't be reached, times out, answers 502, 503 or 504,
                or drops the connection mid-reply.
            NifiAuthError: If the TLS files can't be loaded, the TLS check fails,
                or NiFi answers 401/403.
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

        context = self._ssl_context()
        try:
            with urllib.request.urlopen(
                request, timeout=self._timeout, context=context
            ) as response:
                payload = response.read()
        except urllib.error.HTTPError as e:
            raise _http_error(e, f"{method} {path}", error) from e
        # A TLS error while connecting arrives wrapped in URLError; one while
        # reading the reply (TLS 1.3 rejects a client certificate after the
        # handshake) arrives as is.
        except (urllib.error.URLError, ssl.SSLError) as e:
            reason = e.reason if isinstance(e, urllib.error.URLError) else e
            if _is_tls_failure(reason):
                raise NifiAuthError(f"{error}: TLS check failed: {reason}") from e
            raise NifiConnectionError(f"NiFi API not reachable: {reason}") from e
        # HTTPException covers a reply cut off mid-body, e.g. NiFi restarting.
        except (http.client.HTTPException, TimeoutError, ConnectionError) as e:
            raise NifiConnectionError(f"NiFi API not reachable: {e}") from e

        if not payload:
            return {}
        try:
            return json.loads(payload)
        except json.JSONDecodeError as e:
            raise NifiClientError(f"{error}: invalid JSON from {method} {path}") from e

    def _ssl_context(self) -> ssl.SSLContext | None:
        """Build a TLS context from the current files, or None for plain HTTP.

        Raises:
            NifiAuthError: If the files are missing, unreadable or don't match.
        """
        if self._tls is None:
            return None
        try:
            context = ssl.create_default_context(cafile=str(self._tls.ca))
            context.load_cert_chain(certfile=str(self._tls.cert), keyfile=str(self._tls.key))
        except (OSError, ssl.SSLError) as e:
            raise NifiAuthError(f"Cannot load the charm's TLS files: {e}") from e
        return context


def _http_error(e: urllib.error.HTTPError, call: str, error: str) -> NifiClientError:
    """Turn an HTTP error status from NiFi into the matching client error."""
    # NiFi explains most errors in a plain-text body. It can be cut short if
    # NiFi drops the connection, which mustn't hide the status code.
    try:
        detail = e.read().decode(errors="replace").strip()[:500]
    except (OSError, http.client.HTTPException):
        detail = "<body unreadable>"
    message = f"HTTP {e.code} from {call}: {detail}"
    if e.code in RETRYABLE_STATUSES:
        return NifiConnectionError(f"NiFi API not reachable: {message}")
    if e.code in AUTH_STATUSES:
        return NifiAuthError(f"{error}: {message}")
    return NifiClientError(f"{error}: {message}")


def _is_tls_failure(reason: object) -> bool:
    """Whether an error means the TLS check failed, rather than a dropped connection.

    A connection closed mid-handshake (e.g. NiFi restarting) shows up as an EOF
    and is worth retrying. Anything else from TLS, such as an unknown CA, a name
    mismatch or a rejected client certificate, needs the operator.

    Trade-off: with TLS 1.3, a rejected client certificate occasionally arrives
    as a closed connection instead (an EOF here, or a reset handled as a
    ConnectionError), so that call is retried (Maintenance) and a later one
    reports it (Blocked). Treating every EOF as an auth failure would instead
    block the charm whenever NiFi restarts.
    """
    if isinstance(reason, (ssl.SSLEOFError, ssl.SSLZeroReturnError)):
        return False
    return isinstance(reason, ssl.SSLError)
