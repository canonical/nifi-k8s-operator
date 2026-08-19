# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for exposing NiFi through the `ingress` relation."""

import json
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import jubilant
import pytest
from conftest import APP_NAME, NIFI_IMAGE, UNIT, make_sensitive_key

import constants

MODEL_CONFIG = {"update-status-hook-interval": "10s"}

TRAEFIK = "traefik-k8s"
TRAEFIK_CHANNEL = "latest/stable"

# Traefik publishes its URL as soon as it has a gateway address, but the route
# itself takes a moment longer to become live.
ROUTE_READY_TIMEOUT = 300


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Own model for this module, overriding the shared conftest fixture.

    Always a temporary model: the conftest fixture reuses a single named model
    when JUJU_MODEL is set, and this module deploys the same application as
    tests/integration/test_charm.py, so the two cannot share one.
    """
    with jubilant.temp_model(config=MODEL_CONFIG) as j:
        j.wait_timeout = 20 * 60
        yield j

        if request.session.testsfailed:
            time.sleep(0.5)
            print(j.debug_log(limit=1000), end="", file=sys.stderr)


@pytest.fixture(scope="module")
def ingress(juju: jubilant.Juju, nifi_charm: pathlib.Path) -> str:
    """Deploy NiFi behind traefik and return the proxied base URL."""
    juju.deploy(nifi_charm, app=APP_NAME, resources={"nifi-image": NIFI_IMAGE})
    juju.deploy(TRAEFIK, channel=TRAEFIK_CHANNEL, trust=True)

    secret_uri = juju.add_secret(
        "nifi-sensitive-key",
        {constants.SENSITIVE_PROPS_KEY_FIELD: make_sensitive_key()},
    )
    juju.grant_secret("nifi-sensitive-key", APP_NAME)
    juju.config(APP_NAME, {constants.SENSITIVE_PROPS_KEY_CONFIG: secret_uri})

    juju.integrate(f"{APP_NAME}:{constants.INGRESS_RELATION}", f"{TRAEFIK}:ingress")
    juju.wait(jubilant.all_active, delay=15, timeout=900)

    task = juju.run(f"{TRAEFIK}/0", "show-proxied-endpoints")
    endpoints = json.loads(task.results["proxied-endpoints"])
    return endpoints[APP_NAME]["url"].rstrip("/")


def _get(url: str) -> tuple[int, str]:
    """GET a URL through the ingress, retrying until the route is live."""
    deadline = time.monotonic() + ROUTE_READY_TIMEOUT
    while True:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 (fixed http scheme)
                return resp.status, resp.read().decode()
        except (urllib.error.URLError, OSError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(5)


def test_proxy_properties_match_ingress_url(juju: jubilant.Juju, ingress: str):
    """The ingress URL is reflected in nifi.properties."""
    parsed = urllib.parse.urlparse(ingress)
    props = juju.ssh(
        UNIT,
        f"cat {constants.NIFI_PROPERTIES_PATH}",
        container=constants.CONTAINER_NAME,
    )

    assert f"nifi.web.proxy.host={parsed.hostname}:{parsed.port or 80}" in props
    assert f"nifi.web.proxy.context.path={parsed.path}" in props


def test_ui_reachable_through_ingress(ingress: str):
    """The NiFi UI is served through the ingress."""
    status, _ = _get(f"{ingress}/nifi/")
    assert status == 200


def test_api_reachable_through_ingress(ingress: str):
    """A NiFi REST call succeeds through the ingress.

    The UI shell loads even when the proxy properties are wrong, so this is the
    assertion that actually exercises X-Forwarded-Prefix validation.
    """
    status, body = _get(f"{ingress}/nifi-api/flow/status")

    assert status == 200
    assert "controllerStatus" in json.loads(body)
