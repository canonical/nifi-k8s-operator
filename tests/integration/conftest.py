# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for NiFi K8s integration tests."""

import os
import pathlib
import secrets
import sys
import time

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


def nifi_curl(juju: jubilant.Juju, path: str) -> str:
    """Run a curl GET against the NiFi REST API inside the workload container."""
    cmd = f"curl -fsS --max-time 10 http://localhost:{constants.NIFI_PORT}{path}"
    return juju.ssh(UNIT, cmd, container=constants.CONTAINER_NAME)


def get_nifi_sensitive_key(juju: jubilant.Juju) -> str:
    """Retrieve the current nifi.sensitive.props.key value from nifi.properties."""
    cmd = f"cat {constants.NIFI_PROPERTIES_PATH} | grep nifi.sensitive.props.key"
    output = juju.ssh(UNIT, cmd, container=constants.CONTAINER_NAME).strip()
    return output.split("=", 1)[1] if "=" in output else ""
