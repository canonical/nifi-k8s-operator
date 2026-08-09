# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for NiFi K8s integration tests."""

import json
import os
import pathlib
import secrets
import sys
import time
import urllib.request

import jubilant
import pytest

import constants

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent

APP_NAME = "nifi-k8s"
NIFI_IMAGE = "ghcr.io/canonical/nifi-rocks/nifi:2.10"
UNIT = f"{APP_NAME}/0"


@pytest.fixture(scope="module")
def juju(request: pytest.FixtureRequest):
    """Create a temporary Juju model for running tests."""
    if "JUJU_MODEL" in os.environ:
        j = jubilant.Juju(wait_timeout=20 * 60)
        j.add_model(
            os.environ["JUJU_MODEL"],
            config={"update-status-hook-interval": "10s"},
        )
        yield j

        if request.session.testsfailed:
            time.sleep(0.5)
            log = j.debug_log(limit=1000)
            print(log, end="", file=sys.stderr)
        return

    with jubilant.temp_model(config={"update-status-hook-interval": "10s"}) as j:
        j.wait_timeout = 20 * 60
        yield j

        if request.session.testsfailed:
            time.sleep(0.5)
            log = j.debug_log(limit=1000)
            print(log, end="", file=sys.stderr)


@pytest.fixture(scope="module")
def nifi_charm() -> pathlib.Path:
    """Return the path to the most recently packed nifi-k8s charm."""
    charms = sorted(REPO_ROOT.glob("nifi-k8s_*.charm"), key=lambda p: p.stat().st_mtime)
    if not charms:
        pytest.fail("No packed charm found in repo root. Run `just pack-charm` first.")
    return charms[-1]


def make_sensitive_key() -> str:
    """Generate a random sensitive props key of sufficient length."""
    return secrets.token_urlsafe(32)


def _nifi_base_url(juju: jubilant.Juju) -> str:
    """Return the NiFi REST API base URL, reachable from the test runner.

    NiFi binds 0.0.0.0:8080 (nifi.web.http.host), so it is reachable on the
    unit's pod address. The requests are made from the runner rather than by
    shelling into the container, because the Canonical rock ships no HTTP
    client (curl/wget) -- unlike the upstream apache/nifi image the tests
    originally targeted.
    """
    address = juju.status().apps[APP_NAME].units[UNIT].address
    return f"http://{address}:{constants.NIFI_PORT}"


def nifi_get(juju: jubilant.Juju, path: str) -> str:
    """GET the NiFi REST API from the test runner and return the response body."""
    url = f"{_nifi_base_url(juju)}{path}"
    with urllib.request.urlopen(url, timeout=10) as resp:  # noqa: S310 (fixed http scheme)
        return resp.read().decode()


def nifi_post(juju: jubilant.Juju, path: str, payload: dict) -> str:
    """POST JSON to the NiFi REST API from the test runner and return the body."""
    url = f"{_nifi_base_url(juju)}{path}"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 (fixed http scheme)
        return resp.read().decode()


def get_nifi_sensitive_key(juju: jubilant.Juju) -> str:
    """Retrieve the current nifi.sensitive.props.key value from nifi.properties."""
    cmd = f"cat {constants.NIFI_PROPERTIES_PATH} | grep nifi.sensitive.props.key"
    output = juju.ssh(UNIT, cmd, container=constants.CONTAINER_NAME).strip()
    return output.split("=", 1)[1] if "=" in output else ""
