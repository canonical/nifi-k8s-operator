# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the NiFi K8s charm core functionality."""

import json
import pathlib
import time

import jubilant
import lightkube
import pytest
import yaml
from conftest import (
    APP_NAME,
    NIFI_IMAGE,
    UNIT,
    make_sensitive_key,
    nifi_get,
    nifi_post,
    nifi_request,
)
from helpers import (
    assert_security_context,
    generate_container_securitycontext_map,
    get_pod_names,
    replace_pod,
)
from lightkube.resources.core_v1 import Pod

import constants

CHARMCRAFT = yaml.safe_load(pathlib.Path("./charmcraft.yaml").read_text())
CONTAINERS_SECURITY_CONTEXT_MAP = generate_container_securitycontext_map(CHARMCRAFT)


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


@pytest.mark.parametrize("container_name", list(CONTAINERS_SECURITY_CONTEXT_MAP.keys()))
def test_container_security_context(
    juju: jubilant.Juju,
    container_name: str,
) -> None:
    """Container spec defines the security context with the expected non-root UID/GID."""
    lightkube_client = lightkube.Client()
    pod_name = get_pod_names(lightkube_client, juju.model, APP_NAME)[0]
    assert_security_context(
        lightkube_client,
        pod_name,
        container_name,
        CONTAINERS_SECURITY_CONTEXT_MAP,
        juju.model,
    )


def test_pod_uses_rootless_fsgroup(juju: jubilant.Juju) -> None:
    """The pod security context uses Juju's rootless fs group (gid 170).

    Juju group-owns the storage volumes by this gid and runs the workload with
    it as a supplemental group, which is what makes the mounts writable by the
    non-root user without any chown.
    """
    lightkube_client = lightkube.Client()
    pod_name = get_pod_names(lightkube_client, juju.model, APP_NAME)[0]
    pod = lightkube_client.get(Pod, pod_name, namespace=juju.model)
    assert pod.spec.securityContext.fsGroup == 170


def test_workload_writable_as_non_root(juju: jubilant.Juju) -> None:
    """The workload runs as _daemon_ (584792) and can write to every repository mount."""
    uid = juju.ssh(UNIT, "id -u", container=constants.CONTAINER_NAME).strip()
    assert uid == "584792"

    for path in (
        constants.DATA_DIR,
        constants.CONTENT_REPO_DIR,
        constants.PROVENANCE_REPO_DIR,
    ):
        probe = f"{path}/.charm-write-probe"
        output = juju.ssh(
            UNIT,
            f"touch {probe} && rm {probe} && echo WRITABLE",
            container=constants.CONTAINER_NAME,
        )
        assert "WRITABLE" in output


def test_provenance_repository_is_persistent(juju: jubilant.Juju) -> None:
    """NiFi starts the write-ahead provenance repository, not the in-memory fallback.

    With the implementation unset NiFi creates the VolatileProvenanceRepository,
    which never writes to the provenance-repo mount and loses provenance whenever
    the pod is replaced.
    """
    created = juju.ssh(
        UNIT,
        f"grep -h 'Creating Provenance Repository' {constants.NIFI_LOG_DIR}/nifi-app*.log",
        container=constants.CONTAINER_NAME,
    )
    assert created.strip(), "NiFi did not log which provenance repository it created"
    assert "VolatileProvenanceRepository" not in created
    assert "WriteAheadProvenanceRepository" in created


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
    output = nifi_get(juju, api_path)
    data = json.loads(output)
    assert expected_key in data, (
        f"Expected '{expected_key}' in {api_path} response: {output[:500]}"
    )


def test_nifi_create_and_list_process_group(juju: jubilant.Juju):
    """NiFi can create a process group and list it back via the REST API."""
    pg_name = "integration-test-group"
    payload = {
        "revision": {"version": 0},
        "component": {"name": pg_name, "position": {"x": 0, "y": 0}},
    }

    create_output = nifi_post(juju, "/nifi-api/process-groups/root/process-groups", payload)
    assert pg_name in create_output, f"Failed to create process group: {create_output}"

    list_output = nifi_get(juju, "/nifi-api/process-groups/root/process-groups")
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
    output = nifi_get(juju, "/nifi-api/flow/status")
    data = json.loads(output)
    assert "controllerStatus" in data, f"NiFi API not accessible after rotation: {output[:500]}"


def test_data_persists_across_rotation(juju: jubilant.Juju):
    """Process groups created before rotation remain accessible after rotation."""
    # Create a process group before rotation
    pg_name = "rotation-persistence-test"
    payload = {
        "revision": {"version": 0},
        "component": {"name": pg_name, "position": {"x": 100, "y": 100}},
    }
    create_output = nifi_post(juju, "/nifi-api/process-groups/root/process-groups", payload)
    assert pg_name in create_output, f"Failed to create process group: {create_output}"

    # Rotate the key
    new_key = make_sensitive_key()
    juju.update_secret("nifi-sensitive-key", {constants.SENSITIVE_PROPS_KEY_FIELD: new_key})
    juju.wait(jubilant.all_active, delay=10, timeout=300)

    # Verify the process group still exists after rotation
    list_output = nifi_get(juju, "/nifi-api/process-groups/root/process-groups")
    group_names = [
        pg["component"]["name"] for pg in json.loads(list_output).get("processGroups", [])
    ]
    assert pg_name in group_names, f"Process group lost after rotation: {group_names}"


def _set_run_status(juju: jubilant.Juju, processor: dict, state: str) -> dict:
    """Set a processor's run status and return the updated processor entity."""
    return json.loads(
        nifi_request(
            juju,
            "PUT",
            f"/nifi-api/processors/{processor['id']}/run-status",
            {"revision": processor["revision"], "state": state},
        )
    )


def _provenance_events(juju: jubilant.Juju, processor_id: str) -> list[dict]:
    """Return the provenance events of one processor, searched via the ProcessorID index."""
    request = {
        "maxResults": 10000,
        "searchTerms": {"ProcessorID": {"value": processor_id, "inverse": False}},
    }
    query = json.loads(
        nifi_post(juju, "/nifi-api/provenance", {"provenance": {"request": request}})
    )["provenance"]
    try:
        for _ in range(60):
            if query["finished"]:
                return query["results"]["provenanceEvents"]
            time.sleep(1)
            query = json.loads(nifi_get(juju, f"/nifi-api/provenance/{query['id']}"))["provenance"]
        raise TimeoutError(f"Provenance query {query['id']} did not finish")
    finally:
        nifi_request(juju, "DELETE", f"/nifi-api/provenance/{query['id']}")


def test_provenance_and_content_survive_pod_replacement(juju: jubilant.Juju):
    """Provenance events and their archived content survive a pod replacement.

    The pod is deleted so Kubernetes recreates it on the same Juju storage, which
    is the point where the in-memory provenance repository used to lose history.
    The generated FlowFiles are auto-terminated, so their content only remains
    viewable if the content repository archives it.
    """
    processor = json.loads(
        nifi_post(
            juju,
            "/nifi-api/process-groups/root/processors",
            {
                "revision": {"version": 0},
                "component": {
                    "name": "provenance-persistence-test",
                    "type": "org.apache.nifi.processors.standard.GenerateFlowFile",
                    "config": {
                        "schedulingPeriod": "1 sec",
                        "autoTerminatedRelationships": ["success"],
                        "properties": {"File Size": "1 KB"},
                    },
                },
            },
        )
    )
    processor = _set_run_status(juju, processor, "RUNNING")
    time.sleep(10)
    _set_run_status(juju, processor, "STOPPED")

    before = {e["eventId"]: e for e in _provenance_events(juju, processor["id"])}
    created = [e for e in before.values() if e["eventType"] == "CREATE"]
    assert created, f"No CREATE provenance events recorded: {list(before.values())[:3]}"

    lightkube_client = lightkube.Client()
    replace_pod(
        lightkube_client, get_pod_names(lightkube_client, juju.model, APP_NAME)[0], juju.model
    )
    juju.wait(jubilant.all_active, delay=10, timeout=600)

    # After a restart NiFi re-indexes recovered events in the background.
    after: set[int] = set()
    for _ in range(36):
        after = {e["eventId"] for e in _provenance_events(juju, processor["id"])}
        if before.keys() <= after:
            break
        time.sleep(5)
    assert before.keys() <= after, (
        f"{len(before.keys() - after)}/{len(before)} provenance events lost on pod replacement"
    )

    event_id = created[0]["eventId"]
    event = json.loads(nifi_get(juju, f"/nifi-api/provenance-events/{event_id}"))[
        "provenanceEvent"
    ]
    assert event["outputContentAvailable"], f"Content of event {event_id} was not archived"
    content = nifi_get(juju, f"/nifi-api/provenance-events/{event_id}/content/output")
    assert len(content) == 1024
