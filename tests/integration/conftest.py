# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for NiFi K8s integration tests."""

import pathlib
import secrets

import jubilant
import pytest

import constants

REPO_ROOT = pathlib.Path(__file__).parent.parent.parent

APP_NAME = "nifi-k8s"
CONTAINER_NAME = "nifi"
NIFI_IMAGE = "docker.io/apache/nifi:2.10.0"
UNIT = f"{APP_NAME}/0"


@pytest.fixture(scope="module")
def juju():
    """Provide a jubilant.Juju instance scoped to the test module."""
    with jubilant.temp_model() as juju:
        yield juju


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
    return juju.ssh(UNIT, cmd, container=CONTAINER_NAME)
