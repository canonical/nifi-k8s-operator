# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Constants for the NiFi K8s charm."""

CONTAINER_NAME = "nifi"
SERVICE_NAME = "nifi"

# The upstream apache/nifi image runs as the nifi user.
# This will change to "ubuntu" when the Canonical rock replaces the upstream image.
WORKLOAD_USER = "nifi"
WORKLOAD_GROUP = "nifi"

# TODO Match it to NIFI_HOME when switching to the Canonical rock image. 
# The upstream apache/nifi image uses /opt/nifi/nifi-current as NIFI_HOME, 
# which is a symlink to the actual versioned directory (e.g. /opt/nifi/nifi-2.9.0).
NIFI_HOME = "/opt/nifi/nifi-current"
NIFI_PROPERTIES_PATH = f"{NIFI_HOME}/conf/nifi.properties"

NIFI_HTTP_HOST = "0.0.0.0"
NIFI_PORT = 8080

# Juju storage mount paths (defined in charmcraft.yaml)
# Placed under NIFI_HOME to match upstream NiFi defaults.
DATA_DIR = "/var/lib/nifi/data"
CONTENT_REPO_DIR = "/var/lib/nifi/content_repository"
PROVENANCE_REPO_DIR = "/var/lib/nifi/provenance_repository"

# TODO: Make configurable via Juju secret (nifi.sensitive.props.key).
# Must be at least 12 characters. 32 characters recommended.
SENSITIVE_PROPS_KEY = "placeholder-sensitive-props-key-change-me"

# TODO: Set JAVA_HOME when switching to the Canonical rock image.
# The upstream apache/nifi image already provides JAVA_HOME via the base Eclipse Temurin image.
# The rock will use: /usr/lib/jvm/java-21-openjdk-amd64
