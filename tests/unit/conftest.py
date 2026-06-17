# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ops
import ops.testing
import pytest

import constants
from charm import NifiK8SOperatorCharm


@pytest.fixture()
def context():
    return ops.testing.Context(NifiK8SOperatorCharm)


# Pre-built layer so tests can reference the check without duplicating it.
_NIFI_READY_LAYER = ops.pebble.Layer(
    {
        "checks": {
            "nifi-ready": {
                "override": "replace",
                "level": "ready",
                "startup": "enabled",
                "threshold": 3,
                "http": {"url": f"http://localhost:{constants.NIFI_PORT}/nifi"},
            }
        }
    }
)


_CHOWN_EXEC = ops.testing.Exec(
    [
        "chown",
        "-R",
        f"{constants.WORKLOAD_USER}:{constants.WORKLOAD_GROUP}",
        constants.DATA_DIR,
        constants.CONTENT_REPO_DIR,
        constants.PROVENANCE_REPO_DIR,
    ],
)

# stat execs for each mount root — simulate root ownership so chown is triggered on first boot.
_STAT_EXECS = {
    ops.testing.Exec(
        ["stat", "-c", "%U:%G", root],
        stdout="root:root\n",
    )
    for root in [constants.DATA_DIR, constants.CONTENT_REPO_DIR, constants.PROVENANCE_REPO_DIR]
}


@pytest.fixture()
def container():
    return ops.testing.Container(
        name=constants.CONTAINER_NAME,
        can_connect=True,
        # Pre-populate the plan so check_infos passes the consistency check.
        layers={"nifi-check": _NIFI_READY_LAYER},
        execs={_CHOWN_EXEC, *_STAT_EXECS},
        check_infos={
            ops.testing.CheckInfo(
                "nifi-ready",
                level=ops.pebble.CheckLevel.READY,
                status=ops.pebble.CheckStatus.UP,
            ),
        },
    )


@pytest.fixture()
def state(container):
    return ops.testing.State(containers=[container])


@pytest.fixture()
def disconnected_state():
    """State with a container that cannot be connected to."""
    container = ops.testing.Container(name=constants.CONTAINER_NAME, can_connect=False)
    return ops.testing.State(containers=[container]), container


@pytest.fixture()
def running_container(container):
    """Container with NiFi already active — for testing reconcile with a live service."""
    service_layer = ops.pebble.Layer(
        {
            "services": {
                constants.SERVICE_NAME: {
                    "override": "replace",
                    "summary": "Apache NiFi",
                    "command": f"{constants.NIFI_HOME}/bin/nifi.sh run",
                    "startup": "enabled",
                    "user": constants.WORKLOAD_USER,
                    "group": constants.WORKLOAD_GROUP,
                }
            }
        }
    )
    return ops.testing.Container(
        name=constants.CONTAINER_NAME,
        can_connect=True,
        service_statuses={constants.SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE},
        layers={**container.layers, "nifi": service_layer},
        execs=container.execs,
        check_infos=container.check_infos,
    )


@pytest.fixture()
def booting_container(container):
    """Container where the NiFi readiness check is DOWN (workload still starting)."""
    return ops.testing.Container(
        name=container.name,
        can_connect=container.can_connect,
        layers=container.layers,
        execs=container.execs,
        check_infos={
            ops.testing.CheckInfo(
                "nifi-ready",
                level=ops.pebble.CheckLevel.READY,
                status=ops.pebble.CheckStatus.DOWN,
                failures=1,
            ),
        },
    )
