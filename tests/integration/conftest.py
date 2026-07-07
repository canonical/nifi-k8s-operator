# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for NiFi K8s integration tests."""

import pathlib
import secrets

import jubilant
import pytest

APP_NAME = "nifi-k8s"
CONTAINER_NAME = "nifi"
NIFI_IMAGE = "docker.io/apache/nifi:2.10.0"
NIFI_PORT = 8080
SENSITIVE_PROPS_KEY_CONFIG = "sensitive-props-key"
SENSITIVE_PROPS_KEY_FIELD = "sensitive-props-key"
UNIT = f"{APP_NAME}/0"


def pytest_addoption(parser):
    """Register --charm-path CLI option."""
    parser.addoption(
        "--charm-path",
        action="store",
        default=None,
        help="Path to the packed .charm file to deploy.",
    )


@pytest.fixture(scope="module")
def juju():
    """Provide a jubilant.Juju instance scoped to the test module."""
    with jubilant.temp_model() as juju:
        yield juju


@pytest.fixture(scope="module")
def nifi_charm(pytestconfig) -> pathlib.Path:
    """Return the path to the packed nifi-k8s charm."""
    charm = pytestconfig.getoption("--charm-path")
    if not charm:
        pytest.fail(
            "Charm path not provided. "
            "Pass --charm-path=<path-to-.charm> or run `just integration`."
        )
    return pathlib.Path(charm)


def make_sensitive_key() -> str:
    """Generate a random sensitive props key of sufficient length."""
    return secrets.token_urlsafe(32)


def nifi_curl(juju: jubilant.Juju, path: str) -> str:
    """Run a curl GET against the NiFi REST API inside the workload container."""
    cmd = f"curl -fsS --max-time 10 http://localhost:{NIFI_PORT}{path}"
    return juju.ssh(UNIT, cmd, container=CONTAINER_NAME)
