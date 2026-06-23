# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""NiFi configuration file renderer."""

from jinja2 import Environment, FileSystemLoader, TemplateError

import constants

_TEMPLATES_DIR = "src/templates"


class NifiPropertiesGenerator:
    """Renders NiFi configuration files from Jinja2 templates."""

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
