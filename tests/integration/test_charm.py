# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the NiFi K8s charm core functionality."""

import json
import pathlib

import jubilant
import pytest
from conftest import (
    APP_NAME,
    NIFI_IMAGE,
    UNIT,
    make_sensitive_key,
    nifi_curl,
)

import constants


def test_charm_blocked_without_sensitive_props_key(juju: jubilant.Juju, nifi_charm: pathlib.Path):
    """Charm enters BlockedStatus when deployed without sensitive-props-key."""
    juju.deploy(nifi_charm, app=APP_NAME, resources={"nifi-image": NIFI_IMAGE})
    juju.wait(jubilant.all_blocked, delay=15, timeout=300)

    status = juju.status()
    app_status = status.apps[APP_NAME].app_status
    assert app_status.current == "blocked"
    assert constants.SENSITIVE_PROPS_KEY_CONFIG in app_status.message


def test_charm_active_with_sensitive_props_key(juju: jubilant.Juju):
    """Charm reaches ActiveStatus after sensitive-props-key Juju secret is configured."""
    secret_uri = juju.add_secret(
        "nifi-sensitive-key",
        {constants.SENSITIVE_PROPS_KEY_FIELD: make_sensitive_key()},
    )
    juju.grant_secret("nifi-sensitive-key", APP_NAME)
    juju.config(APP_NAME, {constants.SENSITIVE_PROPS_KEY_CONFIG: secret_uri})

    juju.wait(jubilant.all_active, delay=15, timeout=600)

    status = juju.status()
    assert status.apps[APP_NAME].app_status.current == "active"


def test_pebble_health_check_up(juju: jubilant.Juju):
    """The NiFi Pebble readiness check reports UP after the charm is active."""
    output = juju.ssh(UNIT, "/charm/bin/pebble checks", container=constants.CONTAINER_NAME)
    lines = [line.split() for line in output.strip().splitlines()[1:] if line.strip()]
    assert lines, f"No Pebble checks found in output: {output}"
    for cols in lines:
        name, status = cols[0], cols[3]
        assert status == "up", f"Pebble check {name} not up: {cols}"


@pytest.mark.parametrize(
    "api_path, expected_key",
    [
        ("/nifi-api/controller", "controller"),
        ("/nifi-api/system-diagnostics", "systemDiagnostics"),
        ("/nifi-api/flow/status", "controllerStatus"),
    ],
    ids=["controller", "system-diagnostics", "flow-status"],
)
def test_nifi_api_endpoint(juju: jubilant.Juju, api_path: str, expected_key: str):
    """NiFi REST API endpoints return expected JSON keys."""
    output = nifi_curl(juju, api_path)
    data = json.loads(output)
    assert expected_key in data, (
        f"Expected '{expected_key}' in {api_path} response: {output[:500]}"
    )


def test_nifi_create_and_list_process_group(juju: jubilant.Juju):
    """NiFi can create a process group and list it back via the REST API."""
    pg_name = "integration-test-group"
    payload = json.dumps(
        {
            "revision": {"version": 0},
            "component": {"name": pg_name, "position": {"x": 0, "y": 0}},
        }
    )

    cmd = (
        f"curl -fsS --max-time 10 -X POST"
        f" -H 'Content-Type: application/json'"
        f" -d '{payload}'"
        f" http://localhost:{constants.NIFI_PORT}/nifi-api/process-groups/root/process-groups"
    )
    create_output = juju.ssh(UNIT, cmd, container=constants.CONTAINER_NAME)
    assert pg_name in create_output, f"Failed to create process group: {create_output}"

    list_output = nifi_curl(juju, "/nifi-api/process-groups/root/process-groups")
    group_names = [
        pg["component"]["name"] for pg in json.loads(list_output).get("processGroups", [])
    ]
    assert pg_name in group_names, f"Process group not found: {group_names}"


def test_nifi_accessible_after_rotation(juju: jubilant.Juju):
    """NiFi REST API remains accessible after sensitive key rotation."""
    # Rotate to a new key
    new_key = make_sensitive_key()
    juju.update_secret("nifi-sensitive-key", {constants.SENSITIVE_PROPS_KEY_FIELD: new_key})
    juju.wait(jubilant.all_active, delay=10, timeout=300)

    # Verify NiFi API is accessible
    output = nifi_curl(juju, "/nifi-api/flow/status")
    data = json.loads(output)
    assert "controllerStatus" in data, f"NiFi API not accessible after rotation: {output[:500]}"


def test_data_persists_across_rotation(juju: jubilant.Juju):
    """Process groups created before rotation remain accessible after rotation."""
    # Create a process group before rotation
    pg_name = "rotation-persistence-test"
    payload = json.dumps(
        {
            "revision": {"version": 0},
            "component": {"name": pg_name, "position": {"x": 100, "y": 100}},
        }
    )
    cmd = (
        f"curl -fsS --max-time 10 -X POST"
        f" -H 'Content-Type: application/json'"
        f" -d '{payload}'"
        f" http://localhost:{constants.NIFI_PORT}/nifi-api/process-groups/root/process-groups"
    )
    create_output = juju.ssh(UNIT, cmd, container=constants.CONTAINER_NAME)
    assert pg_name in create_output, f"Failed to create process group: {create_output}"

    # Rotate the key
    new_key = make_sensitive_key()
    juju.update_secret("nifi-sensitive-key", {constants.SENSITIVE_PROPS_KEY_FIELD: new_key})
    juju.wait(jubilant.all_active, delay=10, timeout=300)

    # Verify the process group still exists after rotation
    list_output = nifi_curl(juju, "/nifi-api/process-groups/root/process-groups")
    group_names = [
        pg["component"]["name"] for pg in json.loads(list_output).get("processGroups", [])
    ]
    assert pg_name in group_names, f"Process group lost after rotation: {group_names}"
