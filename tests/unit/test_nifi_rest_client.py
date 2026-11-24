# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for NifiRestClient, against a local server that mimics NiFi's registry-client API.

These tests deliberately use a loopback HTTP server instead of mocks: the client's
job is the HTTP and JSON handling, so mocking urllib would test nothing real. The
server only listens on 127.0.0.1 and each test takes milliseconds.
"""

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from nifi_rest_client import NifiClientError, NifiConnectionError, NifiRestClient

API = "/nifi-api/controller/registry-clients"


class NifiTestServer:
    """In-memory registry clients, plus switches to make the next calls fail."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        # method -> (status, body): answer every call with that method this way.
        self.fail: dict[str, tuple[int, str]] = {}
        # Promise more bytes than are sent, like a connection dropped mid-reply.
        self.truncate = False
        self.delay = 0.0
        self._next_id = 0

    def add(self, name: str, client_id: str, version: int) -> None:
        self.clients[client_id] = {
            "id": client_id,
            "revision": {"version": version},
            "component": {"id": client_id, "name": name, "type": "x", "properties": {}},
        }

    def handle(self, method: str, path: str, body: dict | None) -> tuple[int, str]:
        self.requests.append((method, path, body))
        time.sleep(self.delay)
        if method in self.fail:
            return self.fail[method]
        split = urlsplit(path)
        parts = split.path.removeprefix(API).strip("/")
        if method == "GET" and not parts:
            return 200, json.dumps({"registries": list(self.clients.values())})
        if method == "POST" and not parts:
            if body["revision"] != {"version": 0} or "id" in body["component"]:
                return 400, "Flow Registry ID cannot be specified."
            self._next_id += 1
            client_id = f"new-{self._next_id}"
            entity = {
                "id": client_id,
                "revision": {"version": 1},
                "component": {**body["component"], "id": client_id},
            }
            self.clients[client_id] = entity
            return 201, json.dumps(entity)
        if parts not in self.clients:
            return 404, f"Unable to find flow registry with id '{parts}'."
        current = self.clients[parts]
        if method == "PUT":
            if body["revision"] != current["revision"]:
                return 409, "Revision mismatch"
            current["revision"] = {"version": current["revision"]["version"] + 1}
            current["component"] = body["component"]
            return 200, json.dumps(current)
        if method == "DELETE":
            version = int(parse_qs(split.query)["version"][0])
            if version != current["revision"]["version"]:
                return 409, "Revision mismatch"
            del self.clients[parts]
            return 200, json.dumps(current)
        return 405, "Method not allowed"


@pytest.fixture(autouse=True)
def _block_http():
    """Override conftest's guard: these tests call a local test server on purpose."""
    yield


@pytest.fixture()
def nifi():
    api = NifiTestServer()

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = api.handle(self.command, self.path, body)
            encoded = payload.encode()
            self.send_response(status)
            extra = 50 if api.truncate else 0
            self.send_header("Content-Length", str(len(encoded) + extra))
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    api.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield api
    server.shutdown()
    server.server_close()


@pytest.fixture()
def client(nifi):
    return NifiRestClient(nifi.url, timeout=2)


def _calls(nifi, method):
    return [(path, body) for m, path, body in nifi.requests if m == method]


class TestCreateOrUpdate:
    def test_create_github_registry_client(self, client, nifi):
        result = client.create_or_update_registry_client(
            name="test-client",
            repository_url="https://github.com/owner/repo.git",
            branch="main",
            token="tok",
        )

        assert result["id"] == "new-1"
        [(path, body)] = _calls(nifi, "POST")
        assert path == API
        assert body["revision"] == {"version": 0}
        component = body["component"]
        assert "id" not in component
        assert component["name"] == "test-client"
        assert component["type"] == "org.apache.nifi.github.GitHubFlowRegistryClient"
        assert component["properties"] == {
            "GitHub API URL": "https://api.github.com/",
            "Repository Owner": "owner",
            "Repository Name": "repo",
            "Default Branch": "main",
            "Authentication Type": "PERSONAL_ACCESS_TOKEN",
            "Personal Access Token": "tok",
        }

    def test_github_without_token_uses_no_auth(self, client, nifi):
        client.create_or_update_registry_client(
            name="public", repository_url="https://github.com/owner/repo"
        )
        [(_, body)] = _calls(nifi, "POST")
        assert body["component"]["properties"]["Authentication Type"] == "NONE"
        assert "Personal Access Token" not in body["component"]["properties"]

    def test_update_existing_registry_client(self, client, nifi):
        nifi.add("my-client", "abc", 3)

        result = client.create_or_update_registry_client(
            name="my-client",
            repository_url="https://github.com/owner/repo.git",
        )

        assert _calls(nifi, "POST") == []
        [(path, body)] = _calls(nifi, "PUT")
        assert path == f"{API}/abc"
        assert body["revision"] == {"version": 3}
        assert body["component"]["id"] == "abc"
        assert result["revision"] == {"version": 4}

    def test_create_gitlab_registry_client(self, client, nifi):
        client.create_or_update_registry_client(
            name="gitlab-client",
            repository_url="https://gitlab.com/canonical/nifi-registry.git",
            branch="develop",
            token="glpat-tok",
        )

        [(_, body)] = _calls(nifi, "POST")
        assert body["component"]["type"] == "org.apache.nifi.gitlab.GitLabFlowRegistryClient"
        assert body["component"]["properties"] == {
            "GitLab API URL": "https://gitlab.com",
            "Repository Namespace": "canonical",
            "Repository Name": "nifi-registry",
            "Default Branch": "develop",
            "Authentication Type": "ACCESS_TOKEN",
            "Access Token": "glpat-tok",
        }

    def test_gitlab_in_path_is_still_github(self, client, nifi):
        client.create_or_update_registry_client(
            name="c", repository_url="https://github.com/org/gitlab-runner.git"
        )

        [(_, body)] = _calls(nifi, "POST")
        assert body["component"]["type"] == "org.apache.nifi.github.GitHubFlowRegistryClient"
        assert body["component"]["properties"]["Repository Name"] == "gitlab-runner"

    def test_raises_on_invalid_url(self, client, nifi):
        with pytest.raises(ValueError, match="Cannot extract owner/repo"):
            client.create_or_update_registry_client(
                name="bad-url", repository_url="https://github.com/only-one-part"
            )
        assert nifi.requests == []

    def test_gitlab_without_token_raises_value_error(self, client, nifi):
        with pytest.raises(
            ValueError, match="GitLab flow registry clients require a personal access token"
        ):
            client.create_or_update_registry_client(
                name="gitlab-no-token",
                repository_url="https://gitlab.com/canonical/nifi-registry.git",
                token=None,
            )
        assert nifi.requests == []

    def test_http_error_raises_client_error_with_nifi_message(self, client, nifi):
        nifi.fail["POST"] = (400, "Repository Owner is required")
        with pytest.raises(NifiClientError, match="Registry client operation failed") as e:
            client.create_or_update_registry_client(
                name="err-client", repository_url="https://github.com/owner/repo.git"
            )
        assert not isinstance(e.value, NifiConnectionError)
        assert "HTTP 400" in str(e.value)
        assert "Repository Owner is required" in str(e.value)

    def test_stale_revision_raises_client_error(self, client, nifi):
        nifi.add("my-client", "abc", 3)
        nifi.fail["PUT"] = (409, "Revision mismatch")
        with pytest.raises(NifiClientError, match="HTTP 409"):
            client.create_or_update_registry_client(
                name="my-client", repository_url="https://github.com/owner/repo.git"
            )


class TestDelete:
    def test_delete_existing_client(self, client, nifi):
        nifi.add("to-delete", "del-id", 1)

        assert client.delete_registry_client("to-delete") is True
        [(path, body)] = _calls(nifi, "DELETE")
        assert path == f"{API}/del-id?version=1"
        assert body is None
        assert nifi.clients == {}

    def test_delete_nonexistent_client_returns_false(self, client, nifi):
        nifi.add("other", "other-id", 1)
        assert client.delete_registry_client("missing") is False
        assert _calls(nifi, "DELETE") == []

    @pytest.mark.parametrize(
        ("status", "expected_exception", "expected_message"),
        [
            (502, NifiConnectionError, "NiFi API not reachable"),
            (503, NifiConnectionError, "NiFi API not reachable"),
            (504, NifiConnectionError, "NiFi API not reachable"),
            (404, NifiClientError, "Registry client delete failed"),
            (500, NifiClientError, "Registry client delete failed"),
        ],
        ids=["502", "503", "504", "404", "500"],
    )
    def test_delete_http_errors(self, client, nifi, status, expected_exception, expected_message):
        nifi.add("to-delete", "del-id", 1)
        nifi.fail["DELETE"] = (status, "boom")
        with pytest.raises(expected_exception, match=expected_message):
            client.delete_registry_client("to-delete")


class TestErrors:
    @pytest.mark.parametrize(
        ("status", "expected_exception", "expected_message"),
        [
            (502, NifiConnectionError, "NiFi API not reachable"),
            (503, NifiConnectionError, "NiFi API not reachable"),
            (504, NifiConnectionError, "NiFi API not reachable"),
            (404, NifiClientError, "Failed to list registry clients"),
            (500, NifiClientError, "Failed to list registry clients"),
        ],
        ids=["502", "503", "504", "404", "500"],
    )
    def test_list_http_errors(self, client, nifi, status, expected_exception, expected_message):
        nifi.fail["GET"] = (status, "boom")
        with pytest.raises(expected_exception, match=expected_message):
            client.create_or_update_registry_client(
                name="c", repository_url="https://github.com/owner/repo"
            )
        assert _calls(nifi, "POST") == []

    def test_connection_refused_raises_connection_error(self):
        # Bind then close a socket to get a port nothing listens on.
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        client = NifiRestClient(f"http://127.0.0.1:{port}", timeout=2)
        with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
            client.delete_registry_client("any")

    def test_timeout_raises_connection_error(self, nifi):
        nifi.delay = 0.5
        client = NifiRestClient(nifi.url, timeout=0.1)
        with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
            client.delete_registry_client("any")

    def test_reply_cut_off_raises_connection_error(self, client, nifi):
        nifi.truncate = True
        with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
            client.delete_registry_client("any")

    def test_invalid_json_raises_client_error(self, client, nifi):
        nifi.fail["GET"] = (200, "<html>not json</html>")
        with pytest.raises(NifiClientError, match="invalid JSON"):
            client.delete_registry_client("any")

    def test_empty_response_means_no_clients(self, client, nifi):
        nifi.fail["GET"] = (200, "")
        assert client.delete_registry_client("any") is False

    def test_base_url_trailing_slash(self, nifi):
        client = NifiRestClient(f"{nifi.url}/", timeout=2)
        client.delete_registry_client("any")
        assert nifi.requests[0][1] == API
