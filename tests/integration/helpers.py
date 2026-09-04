# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Utilities for testing security context and user privileges in charms."""

import subprocess
from typing import Dict, TypedDict

import lightkube
from lightkube.resources.core_v1 import Pod


class ContainerSecurityContext(TypedDict):
    """TypedDict representing Kubernetes container security context settings.

    Attributes:
        runAsUser (int | None): The UID to run the container's entry point as.
        runAsGroup (int | None): The GID to run the container's entry point as.
        runAsNonRoot (bool | None): Whether the container must run as a non-root user.
    """

    runAsUser: int | None  # noqa N815
    runAsGroup: int | None  # noqa N815
    runAsNonRoot: bool | None  # noqa N815


def generate_container_securitycontext_map(
    metadata_yaml: dict, juju_user_id: int = 170
) -> dict[str, ContainerSecurityContext]:
    """Generate a mapping of container names to their security context UID/GID settings.

    Args:
        metadata_yaml (dict): The charm's metadata/charmcraft dictionary, expected to
            contain a "containers" key with container definitions including "uid" and
            "gid" fields.
        juju_user_id (int): The user/group ID to use for the charm container. Defaults
            to 170, the standard Juju user ID.

    Returns:
        dict: A mapping of container names to security context dictionaries, plus a
            "charm" entry for the Juju agent container.
    """
    c_uid_map = {}
    for k, v in metadata_yaml.get("containers", {}).items():
        c_uid_map[k] = ContainerSecurityContext(
            runAsUser=v["uid"],
            runAsGroup=v["gid"],
        )
    c_uid_map["charm"] = {"runAsUser": juju_user_id, "runAsGroup": juju_user_id}
    return c_uid_map


def get_pod_names(model: str, application_name: str) -> list[str]:
    """Retrieve names of all pods belonging to a specific Juju application.

    Args:
        model (str): The Juju model name (Kubernetes namespace).
        application_name (str): The Juju application name.

    Returns:
        list[str]: A list of matching pod names, empty if none are found.
    """
    cmd = [
        "kubectl",
        "get",
        "pods",
        f"-n{model}",
        f"-lapp.kubernetes.io/name={application_name}",
        "--no-headers",
        "-o=custom-columns=NAME:.metadata.name",
    ]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
    )
    stdout = proc.stdout.decode("utf8")
    return stdout.split()


def assert_security_context(
    lightkube_client: lightkube.Client,
    pod_name: str,
    container_name: str,
    container_securitycontext_map: Dict[str, ContainerSecurityContext],
    model_name: str,
) -> None:
    """Assert that a container's security context matches the expected UID/GID settings.

    Args:
        lightkube_client (lightkube.Client): A configured lightkube client.
        pod_name (str): The pod containing the container to check.
        container_name (str): The container whose security context is verified.
        container_securitycontext_map (dict): Expected security context per container.
        model_name (str): The Juju model name (Kubernetes namespace).

    Raises:
        AssertionError: If a security context attribute does not match the expected value.
    """
    containers: list = lightkube_client.get(Pod, pod_name, namespace=model_name).spec.containers
    container = next((c for c in containers if c.name == container_name), None)
    security_context = container.securityContext
    for key, value in container_securitycontext_map.get(container_name).items():
        assert getattr(security_context, key) == value
