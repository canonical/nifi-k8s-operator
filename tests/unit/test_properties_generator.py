# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NifiPropertiesGenerator."""

import pytest
from jinja2 import TemplateNotFound

import constants
from properties_generator import NifiPropertiesGenerator


@pytest.fixture()
def generator():
    return NifiPropertiesGenerator(templates_dir="src/templates")


class TestRenderNifiProperties:
    def test_contains_expected_properties(self, generator):
        """Rendered nifi.properties contains all expected key=value pairs."""
        content = generator.render_nifi_properties()
        assert f"nifi.web.http.host={constants.NIFI_HTTP_HOST}" in content
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content
        assert f"nifi.sensitive.props.key={constants.SENSITIVE_PROPS_KEY}" in content
        assert f"nifi.database.directory={constants.DATA_DIR}/database_repository" in content
        assert f"nifi.flowfile.repository.directory={constants.DATA_DIR}/flowfile_repository" in content
        assert f"nifi.content.repository.directory.default={constants.CONTENT_REPO_DIR}" in content
        assert f"nifi.provenance.repository.directory.default={constants.PROVENANCE_REPO_DIR}" in content
        assert f"nifi.state.management.configuration.file={constants.STATE_MANAGEMENT_XML_PATH}" in content
        assert "nifi.security.allow.anonymous.authentication=true" in content

    def test_template_error_raises_runtime_error(self):
        """A missing template raises RuntimeError."""
        gen = NifiPropertiesGenerator(templates_dir="nonexistent/path")
        with pytest.raises(RuntimeError, match="Failed to render nifi.properties"):
            gen.render_nifi_properties()


class TestRenderStateManagementXml:
    def test_contains_local_state_directory(self, generator):
        """Rendered state-management.xml contains the expected local state path."""
        content = generator.render_state_management_xml()
        assert f'<property name="Directory">{constants.DATA_DIR}/state/local</property>' in content

    def test_template_error_raises_runtime_error(self):
        """A missing template raises RuntimeError."""
        gen = NifiPropertiesGenerator(templates_dir="nonexistent/path")
        with pytest.raises(RuntimeError, match="Failed to render state-management.xml"):
            gen.render_state_management_xml()
