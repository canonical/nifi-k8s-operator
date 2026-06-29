# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi configuration file renderer and property manager."""

import logging

import ops
from jinja2 import Environment, FileSystemLoader, TemplateError

import constants

logger = logging.getLogger(__name__)

_TEMPLATES_DIR = "src/templates"


class NifiPropertiesManager:
    """Manages NiFi configuration files: renders from templates and reads from workload."""

    def __init__(self, templates_dir: str = _TEMPLATES_DIR):
        self._env = Environment(loader=FileSystemLoader(str(templates_dir)), autoescape=False)

    def render_nifi_properties(self, sensitive_props_key: str) -> str:
        """Render nifi.properties from the Jinja2 template.

        Args:
            sensitive_props_key: Value for nifi.sensitive.props.key, sourced from
                the Juju user secret bound to the `sensitive-props-key` config.
        """
        context = {
            "data_dir": constants.DATA_DIR,
            "content_repo_dir": constants.CONTENT_REPO_DIR,
            "provenance_repo_dir": constants.PROVENANCE_REPO_DIR,
            "http_host": constants.NIFI_HTTP_HOST,
            "http_port": constants.NIFI_PORT,
            "sensitive_props_key": sensitive_props_key,
            "state_management_xml_path": constants.STATE_MANAGEMENT_XML_PATH,
        }
        try:
            return self._env.get_template("nifi.properties.j2").render(**context)
        except TemplateError as e:
            raise RuntimeError(f"Failed to render nifi.properties: {e}") from e

    def render_state_management_xml(self) -> str:
        """Render state-management.xml from the Jinja2 template."""
        try:
            return self._env.get_template("state-management.xml.j2").render(
                data_dir=constants.DATA_DIR
            )
        except TemplateError as e:
            raise RuntimeError(f"Failed to render state-management.xml: {e}") from e

    @staticmethod
    def get_nifi_property(container, property_name: str) -> str | None:
        """Read a single property value from the on-disk nifi.properties file.

        Returns None if the file does not exist or the property is not found.
        Raises ops.pebble.Error if the file exists but cannot be read.
        """
        if not container.exists(constants.NIFI_PROPERTIES_PATH):
            return None

        try:
            content = container.pull(constants.NIFI_PROPERTIES_PATH).read()
        except ops.pebble.Error:
            logger.exception("Failed to read %s from nifi.properties", property_name)
            raise

        prefix = f"{property_name}="
        for line in content.splitlines():
            if line.startswith(prefix):
                return line.split("=", 1)[1]
        return None
