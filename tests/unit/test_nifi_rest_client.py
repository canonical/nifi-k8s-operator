# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for NifiRestClient, against a local server that mimics NiFi's registry-client API.

These tests deliberately use a loopback HTTP and HTTPS server instead of mocks: the
client's job is the HTTP, TLS and JSON handling, so mocking urllib would test
nothing real. The server only listens on 127.0.0.1 and each test takes milliseconds.
One test patches the opener, for TLS errors that a real socket only produces now and then.
"""

import contextlib
import datetime
import json
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from nifi_rest_client import (
    ClientTls,
    NifiAuthError,
    NifiClientError,
    NifiConnectionError,
    NifiRestClient,
    _is_tls_failure,
)

API = "/nifi-api/controller/registry-clients"


class NifiTestServer:
    """In-memory registry clients, plus switches to make the next calls fail."""

    def __init__(self):
        self.clients: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict | None]] = []
        # Common name of the client certificate on each HTTPS request.
        self.peers: list[str] = []
        # method -> (status, body): answer every call with that method this way.
        self.fail: dict[str, tuple[int, str]] = {}
        # Promise more bytes than are sent, like a connection dropped mid-reply.
        self.truncate = False
        # Location header to send, to answer with a redirect.
        self.location: str | None = None
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


@contextlib.contextmanager
def _serve(api: NifiTestServer, ssl_context: ssl.SSLContext | None = None):
    """Run `api` on a loopback HTTP server, or HTTPS with `ssl_context`."""

    class Handler(BaseHTTPRequestHandler):
        def _serve(self):
            if ssl_context:
                subject = dict(x[0] for x in self.connection.getpeercert()["subject"])
                api.peers.append(subject["commonName"])
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length)) if length else None
            status, payload = api.handle(self.command, self.path, body)
            encoded = payload.encode()
            self.send_response(status)
            extra = 50 if api.truncate else 0
            self.send_header("Content-Length", str(len(encoded) + extra))
            if api.location:
                self.send_header("Location", api.location)
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = do_POST = do_PUT = do_DELETE = _serve  # noqa: N815

        def log_message(self, *args):
            pass

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            # Expected in the timeout and TLS tests: the client hangs up first.
            pass

    server = Server(("127.0.0.1", 0), Handler)
    if ssl_context:
        server.socket = ssl_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    port = server.server_address[1]
    # HTTPS uses "localhost", the name in the test server's certificate.
    api.url = f"https://localhost:{port}" if ssl_context else f"http://127.0.0.1:{port}"
    try:
        yield api
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def nifi():
    with _serve(NifiTestServer()) as api:
        yield api


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

    @pytest.mark.parametrize(
        ("repository_url", "api_url"),
        [
            ("https://github.com/owner/repo.git", "https://api.github.com/"),
            ("https://notgithub.com/owner/repo.git", "https://notgithub.com/api/v1/"),
            (
                "https://github.com.example.org/owner/repo",
                "https://github.com.example.org/api/v1/",
            ),
        ],
        ids=["github", "similar-name", "github-in-host"],
    )
    def test_github_api_url_only_for_github_com(self, client, nifi, repository_url, api_url):
        client.create_or_update_registry_client(name="c", repository_url=repository_url)

        [(_, body)] = _calls(nifi, "POST")
        assert body["component"]["properties"]["GitHub API URL"] == api_url

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

    @pytest.mark.parametrize(
        ("status", "expected_exception"),
        [(503, NifiConnectionError), (403, NifiAuthError)],
        ids=["503", "403"],
    )
    def test_error_body_cut_short_keeps_status(self, client, nifi, status, expected_exception):
        nifi.fail["GET"] = (status, "boom")
        nifi.truncate = True
        with pytest.raises(expected_exception, match="<body unreadable>"):
            client.delete_registry_client("any")

    def test_redirect_is_not_followed(self, client, nifi):
        nifi.fail["GET"] = (302, "moved")
        nifi.location = f"{nifi.url}/elsewhere"
        with pytest.raises(NifiClientError, match="HTTP 302"):
            client.delete_registry_client("any")
        assert len(nifi.requests) == 1

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

    @pytest.mark.parametrize("status", [401, 403])
    def test_unauthorized_raises_auth_error(self, client, nifi, status):
        nifi.fail["GET"] = (status, "Unable to view the controller")
        with pytest.raises(NifiAuthError, match=f"HTTP {status}"):
            client.delete_registry_client("any")


# --- HTTPS -------------------------------------------------------------------

_VALIDITY = datetime.timedelta(days=1)


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _cert(cn: str, key, issuer=None, *, ca=False, dns=None, client=False):
    """Issue a certificate for `key`; self-signed when `issuer` is None."""
    issuer_cert, issuer_key = issuer if issuer else (None, key)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(issuer_cert.subject if issuer_cert else name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + _VALIDITY)
        .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        # Python 3.13+ verifies strictly (VERIFY_X509_STRICT), which needs these,
        # as real CAs (self-signed-certificates, Vault) always set them.
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()), False
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=not ca,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=not ca,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if dns:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(d) for d in dns]), critical=False
        )
    if not ca:
        usage = ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH
        builder = builder.add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


def _write(path: Path, *objects) -> Path:
    pems = []
    for obj in objects:
        if isinstance(obj, x509.Certificate):
            pems.append(obj.public_bytes(serialization.Encoding.PEM))
        else:
            pems.append(
                obj.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
    path.write_bytes(b"".join(pems))
    return path


class Pki:
    """A CA that signs NiFi's server certificate and the charm's client certificate."""

    def __init__(self, tmp_path: Path):
        self.dir = tmp_path
        self.ca_key = _key()
        self.ca = _cert("test-ca", self.ca_key, ca=True)
        self.server_key = _key()
        self.server = _cert(
            "localhost", self.server_key, (self.ca, self.ca_key), dns=["localhost"]
        )

    def client_tls(
        self, cn: str = "charm.nifi.test", issuer=None, name: str = "charm"
    ) -> ClientTls:
        """Write a client certificate (signed by this CA unless `issuer`) and return its files."""
        key = _key()
        cert = _cert(cn, key, issuer or (self.ca, self.ca_key), client=True)
        return ClientTls(
            ca=_write(self.dir / f"{name}-ca.pem", self.ca),
            cert=_write(self.dir / f"{name}-cert.pem", cert),
            key=_write(self.dir / f"{name}-key.pem", key),
        )

    def server_context(self, server_cert=None, max_version=None) -> ssl.SSLContext:
        """NiFi's side: present the server certificate, require a client certificate."""
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        if max_version:
            context.maximum_version = max_version
        context.load_cert_chain(
            _write(self.dir / "server-cert.pem", server_cert or self.server),
            _write(self.dir / "server-key.pem", self.server_key),
        )
        context.load_verify_locations(_write(self.dir / "server-ca.pem", self.ca))
        context.verify_mode = ssl.CERT_REQUIRED
        return context


@pytest.fixture()
def pki(tmp_path):
    return Pki(tmp_path)


@pytest.fixture()
def nifi_https(pki):
    with _serve(NifiTestServer(), pki.server_context()) as api:
        yield api


class TestHttps:
    def test_tls_files_need_an_https_url(self, pki):
        """Over http urllib would ignore the TLS context and call in plain text."""
        with pytest.raises(ValueError, match="needs an https:// URL"):
            NifiRestClient("http://localhost:8080", tls=pki.client_tls())

    def test_certificates_signed_by_an_intermediate(self, pki):
        # Like Vault or a corporate CA: both certificates come from an intermediate,
        # each cert.pem carries it, and both sides trust only the root.
        int_key = _key()
        intermediate = _cert("test-intermediate", int_key, (pki.ca, pki.ca_key), ca=True)
        server_key = _key()
        server = _cert("localhost", server_key, (intermediate, int_key), dns=["localhost"])
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            _write(pki.dir / "int-server.pem", server, intermediate),
            _write(pki.dir / "int-server-key.pem", server_key),
        )
        context.load_verify_locations(_write(pki.dir / "int-root.pem", pki.ca))
        context.verify_mode = ssl.CERT_REQUIRED

        client_key = _key()
        client_cert = _cert("charm.nifi.test", client_key, (intermediate, int_key), client=True)
        tls = ClientTls(
            ca=_write(pki.dir / "int-client-ca.pem", pki.ca),
            cert=_write(pki.dir / "int-client-cert.pem", client_cert, intermediate),
            key=_write(pki.dir / "int-client-key.pem", client_key),
        )
        with _serve(NifiTestServer(), context) as api:
            NifiRestClient(api.url, timeout=2, tls=tls).delete_registry_client("any")
            assert api.peers == ["charm.nifi.test"]

            # Without the intermediate in cert.pem, NiFi can't verify the charm.
            _write(tls.cert, client_cert)
            with pytest.raises(NifiAuthError, match="TLS check failed"):
                NifiRestClient(api.url, timeout=2, tls=tls).delete_registry_client("any")

    def test_calls_nifi_with_the_charms_certificate(self, pki, nifi_https):
        client = NifiRestClient(nifi_https.url, timeout=2, tls=pki.client_tls())

        client.create_or_update_registry_client(
            name="c", repository_url="https://github.com/owner/repo"
        )

        assert [m for m, _, _ in nifi_https.requests] == ["GET", "POST"]
        assert nifi_https.peers == ["charm.nifi.test", "charm.nifi.test"]

    def test_renewed_certificate_is_used_on_the_next_call(self, pki, nifi_https):
        tls = pki.client_tls(cn="charm.before")
        client = NifiRestClient(nifi_https.url, timeout=2, tls=tls)
        client.delete_registry_client("any")

        renewed = pki.client_tls(cn="charm.after")  # same file names, new content
        assert renewed == tls
        client.delete_registry_client("any")

        assert nifi_https.peers == ["charm.before", "charm.after"]

    def test_untrusted_server_certificate_raises_auth_error(self, pki, nifi_https):
        tls = pki.client_tls()
        other_ca_key = _key()
        _write(tls.ca, _cert("other-ca", other_ca_key, ca=True))
        client = NifiRestClient(nifi_https.url, timeout=2, tls=tls)

        with pytest.raises(NifiAuthError, match="TLS check failed"):
            client.delete_registry_client("any")
        assert nifi_https.requests == []

    def test_server_name_mismatch_raises_auth_error(self, pki):
        wrong_name = _cert(
            "elsewhere", pki.server_key, (pki.ca, pki.ca_key), dns=["nifi.elsewhere"]
        )
        with _serve(NifiTestServer(), pki.server_context(wrong_name)) as api:
            client = NifiRestClient(api.url, timeout=2, tls=pki.client_tls())
            with pytest.raises(NifiAuthError, match="TLS check failed"):
                client.delete_registry_client("any")

    def test_client_certificate_rejected_raises_auth_error(self, pki):
        # Signed by a CA NiFi doesn't trust. Pinned to TLS 1.2, where the rejection
        # always comes during the handshake. With TLS 1.3 it comes after it, and
        # now and then as a closed connection, which is retried (see _is_tls_failure).
        other_ca_key = _key()
        other_ca = _cert("other-ca", other_ca_key, ca=True)
        tls = pki.client_tls(issuer=(other_ca, other_ca_key))
        context = pki.server_context(max_version=ssl.TLSVersion.TLSv1_2)
        with _serve(NifiTestServer(), context) as api:
            client = NifiRestClient(api.url, timeout=2, tls=tls)
            with pytest.raises(NifiAuthError, match="TLS check failed"):
                client.delete_registry_client("any")
            assert api.requests == []

    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (ssl.SSLError(1, "[SSL: TLSV13_ALERT_CERTIFICATE_REQUIRED]"), NifiAuthError),
            (ssl.SSLEOFError(8, "EOF occurred in violation of protocol"), NifiConnectionError),
        ],
        ids=["alert", "eof"],
    )
    def test_tls_error_while_reading_the_reply(self, pki, error, expected):
        # With TLS 1.3 these arrive unwrapped, from reading the reply.
        client = NifiRestClient("https://localhost:8443", timeout=2, tls=pki.client_tls())
        with patch("nifi_rest_client.urllib.request.OpenerDirector.open", side_effect=error):
            with pytest.raises(expected):
                client.delete_registry_client("any")

    def test_connection_closed_during_handshake_is_retried(self, pki):
        # Like NiFi restarting: the port accepts, then closes before any TLS.
        listener = socket.create_server(("127.0.0.1", 0))
        port = listener.getsockname()[1]

        def _accept_and_close():
            conn, _ = listener.accept()
            conn.close()

        thread = threading.Thread(target=_accept_and_close, daemon=True)
        thread.start()
        try:
            client = NifiRestClient(f"https://localhost:{port}", timeout=2, tls=pki.client_tls())
            with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
                client.delete_registry_client("any")
        finally:
            thread.join(timeout=2)
            listener.close()

    def test_missing_files_raise_auth_error(self, pki, nifi_https):
        tls = pki.client_tls()
        tls.cert.unlink()
        client = NifiRestClient(nifi_https.url, timeout=2, tls=tls)

        with pytest.raises(NifiAuthError, match="Cannot load the charm's TLS files"):
            client.delete_registry_client("any")

    def test_key_not_matching_certificate_raises_auth_error(self, pki, nifi_https):
        tls = pki.client_tls()
        _write(tls.key, _key())
        client = NifiRestClient(nifi_https.url, timeout=2, tls=tls)

        with pytest.raises(NifiAuthError, match="Cannot load the charm's TLS files"):
            client.delete_registry_client("any")

    def test_https_unreachable_raises_connection_error(self, pki):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        client = NifiRestClient(f"https://localhost:{port}", timeout=2, tls=pki.client_tls())
        with pytest.raises(NifiConnectionError, match="NiFi API not reachable"):
            client.delete_registry_client("any")

    @pytest.mark.parametrize(
        ("error", "is_tls_failure"),
        [
            (ssl.SSLCertVerificationError("certificate verify failed"), True),
            (ssl.SSLError("tlsv13 alert certificate required"), True),
            (ssl.SSLEOFError("EOF occurred in violation of protocol"), False),
            (ssl.SSLZeroReturnError("TLS/SSL connection has been closed"), False),
            (ConnectionRefusedError(), False),
        ],
        ids=["verify-failed", "alert", "eof", "closed", "refused"],
    )
    def test_only_tls_check_failures_need_the_operator(self, error, is_tls_failure):
        # A connection dropped mid-handshake (NiFi restarting) is retried instead.
        assert _is_tls_failure(error) is is_tls_failure
