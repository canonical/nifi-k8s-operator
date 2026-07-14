# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Constants for the NiFi K8s charm."""

CONTAINER_NAME = "nifi"
SERVICE_NAME = "nifi"
READY_CHECK_NAME = "nifi-ready"

# The upstream apache/nifi image runs as the nifi user.
# This will change to "ubuntu" when the Canonical rock replaces the upstream image.
WORKLOAD_USER = "nifi"
WORKLOAD_GROUP = "nifi"

# TODO Match it to NIFI_HOME when switching to the Canonical rock image(/opt/nifi).
# The upstream apache/nifi image uses /opt/nifi/nifi-current as NIFI_HOME,
# which is a symlink to the actual versioned directory (e.g. /opt/nifi/nifi-2.9.0).
NIFI_HOME = "/opt/nifi/nifi-current"
NIFI_PROPERTIES_PATH = f"{NIFI_HOME}/conf/nifi.properties"
STATE_MANAGEMENT_XML_PATH = f"{NIFI_HOME}/conf/state-management.xml"

NIFI_HTTP_HOST = "0.0.0.0"
NIFI_PORT = 8080

# Juju storage mount paths (defined in charmcraft.yaml)
# Placed under NIFI_HOME to match upstream NiFi defaults.
DATA_DIR = "/var/lib/nifi/data"
CONTENT_REPO_DIR = "/var/lib/nifi/content_repository"
PROVENANCE_REPO_DIR = "/var/lib/nifi/provenance_repository"

# TODO: Set JAVA_HOME when switching to the Canonical rock image.
# The upstream apache/nifi image already provides JAVA_HOME via the base Eclipse Temurin image.
# The rock will use: /usr/lib/jvm/java-21-openjdk-amd64

# Juju config & secret schema
SENSITIVE_PROPS_KEY_CONFIG = "sensitive-props-key"
SENSITIVE_PROPS_KEY_FIELD = "sensitive-props-key"
SENSITIVE_PROPS_KEY_MIN_LENGTH = 12

# Unit status messages
MSG_PEBBLE_NOT_READY = "Cannot connect to workload container"
MSG_NIFI_STARTING = "NiFi is starting"
MSG_SERVICE_START_FAILED = "Failed to (re)start NiFi service"
MSG_CONFIG_WRITE_FAILED = "Failed to write NiFi configuration"
MSG_GIT_REGISTRY_NOT_READY = "Waiting for git-registry relation data"

# Relation names
GIT_REGISTRY_RELATION = "git-registry"
MSG_SENSITIVE_KEY_MISSING = (
    f"Missing required config '{SENSITIVE_PROPS_KEY_CONFIG}' (Juju user secret)"
)
MSG_SENSITIVE_KEY_INVALID = (
    f"Cannot read '{SENSITIVE_PROPS_KEY_CONFIG}' secret; "
    f"ensure it exists, is granted to the application, "
    f"and exposes field '{SENSITIVE_PROPS_KEY_FIELD}'"
)
MSG_SENSITIVE_KEY_TOO_SHORT = (
    f"'{SENSITIVE_PROPS_KEY_CONFIG}' must be at least "
    f"{SENSITIVE_PROPS_KEY_MIN_LENGTH} characters long"
)
MSG_PROPERTY_READ_ERROR = "Failed to read nifi.properties from workload; will retry"
MSG_ROTATING_SENSITIVE_KEY = "Rotating nifi.sensitive.props.key"
MSG_KEY_ROTATION_FAILED = "Failed to rotate nifi.sensitive.props.key"
