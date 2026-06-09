# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

import ops.testing
import pytest

import constants
from charm import NifiK8SOperatorCharm


@pytest.fixture()
def context():
    return ops.testing.Context(NifiK8SOperatorCharm)


@pytest.fixture()
def container():
    return ops.testing.Container(
        name=constants.CONTAINER_NAME,
        can_connect=True,
        execs={
            ops.testing.Exec(
                [
                    "chown",
                    "-R",
                    f"{constants.WORKLOAD_USER}:{constants.WORKLOAD_GROUP}",
                    constants.DATA_DIR,
                    constants.CONTENT_REPO_DIR,
                    constants.PROVENANCE_REPO_DIR,
                ],
            ),
        },
    )


@pytest.fixture()
def state(container):
    return ops.testing.State(containers=[container])
