# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Constants for the NiFi K8s charm."""

CONTAINER_NAME = "nifi"
SERVICE_NAME = "nifi"
READY_CHECK_NAME = "nifi-ready"

# The Canonical NiFi rock runs the workload as the shared non-root user
# _daemon_ (uid/gid 584792), which owns /opt/nifi and the /var/lib/nifi repositories.
WORKLOAD_USER = "_daemon_"
WORKLOAD_GROUP = "_daemon_"

# The rock installs NiFi directly at /opt/nifi (no nifi-current symlink).
NIFI_HOME = "/opt/nifi"
NIFI_PROPERTIES_PATH = f"{NIFI_HOME}/conf/nifi.properties"
STATE_MANAGEMENT_XML_PATH = f"{NIFI_HOME}/conf/state-management.xml"

NIFI_HTTP_HOST = "0.0.0.0"
NIFI_PORT = 8080

# Juju storage mount paths (defined in charmcraft.yaml). Absolute /var/lib/nifi
# paths, matching the rock's default repository locations, so the storage mounts
# line up without overriding any NiFi properties.
DATA_DIR = "/var/lib/nifi/data"
CONTENT_REPO_DIR = "/var/lib/nifi/content_repository"
PROVENANCE_REPO_DIR = "/var/lib/nifi/provenance_repository"

# The rock installs openjdk-21-jre-headless via stage-packages, which does not
# register the /usr/bin/java alternative, so nifi.sh cannot find Java on PATH.
# JAVA_HOME must be set in the Pebble service environment or NiFi exits at start.
JAVA_HOME = "/usr/lib/jvm/java-21-openjdk-amd64"

# NiFi log directory. The rock pre-creates /var/log/nifi (owned _daemon_:_daemon_)
# and it is not a Juju storage mount, so the workload can write to it directly.
# bin/nifi-env.sh only honours NIFI_LOG_DIR when NIFI_OVERRIDE_NIFIENV is "true";
# without that flag it silently falls back to $NIFI_HOME/logs.
NIFI_LOG_DIR = "/var/log/nifi"

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
MSG_INGRESS_NOT_READY = "Waiting for ingress URL"
MSG_GIT_REGISTRY_API_ERROR = "Failed to configure NiFi flow registry client"
MSG_GIT_REGISTRY_SSH_UNSUPPORTED = (
    "NiFi flow registry does not support SSH auth; "
    "reconfigure git-integrator with credentials (personal access token)"
)

# Relation names
GIT_REGISTRY_RELATION = "git-registry"
INGRESS_RELATION = "ingress"

# NiFi flow registry client
FLOW_REGISTRY_CLIENT_NAME = "juju-git-registry"

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
MSG_KEY_ROTATION_FAILED = "Failed to rotate nifi.sensitive.props.key"
