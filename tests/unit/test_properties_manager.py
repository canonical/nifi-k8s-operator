# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the NifiPropertiesManager."""

import pathlib
import re
import xml.etree.ElementTree as ET

import pytest
import yaml
from conftest import java_properties

import constants
from properties_manager import NifiPropertiesManager

_TEST_KEY = "test-sensitive-key-32-characters!"
_IDENTITY = "CN=charm.nifi-k8s.test-model.svc.cluster.local"
_POD_FQDN = "nifi-k8s-0.nifi-k8s-endpoints.test-model.svc.cluster.local"
_HTTP_SNAPSHOT = pathlib.Path(__file__).parent / "snapshots" / "nifi.properties.http"

# Settings that belong to HTTPS only; none of them may appear in an HTTP render.
_HTTPS_ONLY_PREFIXES = (
    "nifi.web.https.",
    "nifi.security.keystore",
    "nifi.security.truststore",
    "nifi.security.key",
    "nifi.security.autoreload.",
    "nifi.security.identity.mapping.",
    "nifi.security.user.",
    "nifi.authorizer.",
    "nifi.remote.input.secure",
)


# Juju storage sizes (M/G/T/P/E) and NiFi data sizes (KB/MB/GB/TB) are both binary.
_SIZE_EXPONENTS = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}


def _parse_properties(content: str) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def _size_in_bytes(size: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([KMGTPE])B?\s*", size, re.IGNORECASE)
    assert match, f"Invalid size: {size!r}"
    return int(match.group(1)) * 1024 ** _SIZE_EXPONENTS[match.group(2).upper()]


@pytest.fixture()
def generator():
    return NifiPropertiesManager(templates_dir="src/templates")


@pytest.fixture()
def properties(generator) -> dict[str, str]:
    return _parse_properties(generator.render_nifi_properties(sensitive_props_key=_TEST_KEY))


class TestRenderNifiProperties:
    def test_contains_expected_properties(self, generator):
        """Rendered nifi.properties contains all expected key=value pairs."""
        content = generator.render_nifi_properties(sensitive_props_key=_TEST_KEY)
        assert f"nifi.web.http.host={constants.NIFI_HTTP_HOST}" in content
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content
        assert f"nifi.sensitive.props.key={_TEST_KEY}" in content
        assert f"nifi.database.directory={constants.DATA_DIR}/database_repository" in content
        assert (
            f"nifi.flowfile.repository.directory={constants.DATA_DIR}/flowfile_repository"
            in content
        )
        assert f"nifi.content.repository.directory.default={constants.CONTENT_REPO_DIR}" in content
        assert (
            f"nifi.provenance.repository.directory.default={constants.PROVENANCE_REPO_DIR}"
            in content
        )
        assert (
            f"nifi.state.management.configuration.file={constants.STATE_MANAGEMENT_XML_PATH}"
            in content
        )
        assert "nifi.security.allow.anonymous.authentication=true" in content

    def test_provenance_repository_is_persistent(self, properties):
        """Provenance uses the write-ahead repository on the provenance-repo storage.

        NiFi falls back to the in-memory VolatileProvenanceRepository when the
        implementation is unset, leaving the provenance-repo volume unused.
        """
        assert (
            properties["nifi.provenance.repository.implementation"]
            == "org.apache.nifi.provenance.WriteAheadProvenanceRepository"
        )
        assert (
            properties["nifi.provenance.repository.directory.default"]
            == constants.PROVENANCE_REPO_DIR
        )

    def test_provenance_events_are_searchable(self, properties):
        """Provenance indexes the fields the UI searches on, not only EventTime."""
        indexed = {
            field.strip()
            for field in properties["nifi.provenance.repository.indexed.fields"].split(",")
        }
        assert {"EventType", "FlowFileUUID", "Filename", "ProcessorID", "Relationship"} <= indexed

    def test_provenance_max_storage_fits_storage_volume(self, properties):
        """The provenance size cap leaves headroom on the smallest provenance-repo volume."""
        charmcraft = yaml.safe_load(pathlib.Path("charmcraft.yaml").read_text())
        volume_min = charmcraft["storage"]["provenance-repo"]["minimum-size"]
        max_storage = properties["nifi.provenance.repository.max.storage.size"]
        assert _size_in_bytes(max_storage) < _size_in_bytes(volume_min)

    def test_content_archive_enabled(self, properties):
        """Content archiving is on, so provenance can show and replay completed FlowFiles.

        NiFi disables archiving when the property is unset and deletes content
        as soon as a FlowFile completes.
        """
        assert properties["nifi.content.repository.archive.enabled"] == "true"

    def test_template_error_raises_runtime_error(self):
        """A missing template raises RuntimeError."""
        gen = NifiPropertiesManager(templates_dir="nonexistent/path")
        with pytest.raises(RuntimeError, match="Failed to render nifi.properties"):
            gen.render_nifi_properties(sensitive_props_key=_TEST_KEY)


class TestRenderNifiPropertiesHttps:
    @pytest.fixture()
    def https(self, generator) -> dict[str, str]:
        return java_properties(
            generator.render_nifi_properties(sensitive_props_key=_TEST_KEY, https_host=_POD_FQDN)
        )

    def test_http_has_no_https_settings(self, generator):
        """Without an HTTPS host the file is HTTP as before, with nothing extra.

        Any change to the HTTP render restarts NiFi on every existing deployment.
        """
        content = generator.render_nifi_properties(sensitive_props_key=_TEST_KEY)
        assert not [k for k in java_properties(content) if k.startswith(_HTTPS_ONLY_PREFIXES)]
        assert f"nifi.web.http.port={constants.NIFI_PORT}" in content

    def test_http_render_matches_the_snapshot(self, generator):
        """The HTTP render must match the snapshot exactly.

        Any change restarts NiFi on every existing deployment at upgrade.
        The missing final newline is intended (Jinja drops it).
        """
        content = generator.render_nifi_properties(sensitive_props_key=_TEST_KEY)
        assert content == _HTTP_SNAPSHOT.read_text()

    def test_https_listens_on_the_pod_fqdn_only(self, https):
        assert https["nifi.web.http.host"] == ""
        assert https["nifi.web.http.port"] == ""
        assert https["nifi.web.https.host"] == _POD_FQDN
        assert https["nifi.web.https.port"] == str(constants.NIFI_HTTPS_PORT)

    def test_https_uses_the_pem_files(self, https):
        assert https["nifi.security.keystoreType"] == "PEM"
        assert https["nifi.security.keystore.certificate"] == constants.TLS_CERT_PATH
        assert https["nifi.security.keystore.privateKey"] == constants.TLS_KEY_PATH
        assert https["nifi.security.truststoreType"] == "PEM"
        assert https["nifi.security.truststore.certificate"] == constants.TLS_CA_PATH
        # Keep store paths and passwords empty so NiFi takes the PEM branch.
        for key in (
            "nifi.security.keystore",
            "nifi.security.keystorePasswd",
            "nifi.security.keyPasswd",
            "nifi.security.truststore",
            "nifi.security.truststorePasswd",
        ):
            assert https[key] == "", key
        assert https["nifi.security.autoreload.enabled"] == "true"
        assert https["nifi.remote.input.secure"] == "true"

    def test_https_uses_the_managed_authorizer(self, https):
        assert https["nifi.security.user.authorizer"] == "managed-authorizer"
        assert https["nifi.authorizer.configuration.file"] == constants.AUTHORIZERS_XML_PATH
        assert https["nifi.security.user.login.identity.provider"] == ""
        assert https["nifi.security.identity.mapping.transform.dn"] == "NONE"
        # Anonymous stays on: it has no permissions under the managed authorizer.
        assert https["nifi.security.allow.anonymous.authentication"] == "true"

    @pytest.mark.parametrize(
        ("identity", "mapped"),
        [
            # NiFi formats certificate names as RFC 1779; the library's ID comes first.
            (f"OID.2.5.4.45=3f2a9c1b-0000-4000-8000-000000000000, {_IDENTITY}", _IDENTITY),
            (_IDENTITY, _IDENTITY),
            ("alice@example.com", "alice@example.com"),
            ("CN=x, OID.2.5.4.45=#03110000", "CN=x, OID.2.5.4.45=#03110000"),
            ("OIDx2x5x4x45=#0311, CN=x", "OIDx2x5x4x45=#0311, CN=x"),
            # Only the ID goes: a subject with more names keeps all of them.
            ("OID.2.5.4.45=#0311, CN=alice, O=Org", "CN=alice, O=Org"),
        ],
        ids=[
            "random-id",
            "already-mapped",
            "email",
            "id-not-first",
            "dots-are-literal",
            "more-names",
        ],
    )
    def test_identity_mapping(self, https, identity, mapped):
        """The pattern, as NiFi reads it, removes only a leading random ID."""
        pattern = https["nifi.security.identity.mapping.pattern.dn"]
        value = https["nifi.security.identity.mapping.value.dn"].replace("$1", r"\1")
        match = re.match(pattern, identity)
        assert (match.expand(value) if match else identity) == mapped


class TestRenderStateManagementXml:
    def test_contains_local_state_directory(self, generator):
        """Rendered state-management.xml contains the expected local state path."""
        content = generator.render_state_management_xml()
        assert f'<property name="Directory">{constants.DATA_DIR}/state/local</property>' in content

    def test_template_error_raises_runtime_error(self):
        """A missing template raises RuntimeError."""
        gen = NifiPropertiesManager(templates_dir="nonexistent/path")
        with pytest.raises(RuntimeError, match="Failed to render state-management.xml"):
            gen.render_state_management_xml()


class TestRenderAuthorizersXml:
    def test_identity_is_xml_escaped(self, generator):
        identity = 'CN=a<b&c"d'
        root = ET.fromstring(generator.render_authorizers_xml(identity))
        seeds = [p.text for p in root.iter("property") if "Identity" in p.get("name")]
        assert seeds == [identity, identity]

    def test_seeds_charm_identity_with_files_on_the_volume(self, generator):
        root = ET.fromstring(generator.render_authorizers_xml(_IDENTITY))
        values = {}
        for section in root:
            identifier = section.findtext("identifier")
            for prop in section.findall("property"):
                values[(identifier, prop.get("name"))] = prop.text
        assert values[("file-user-group-provider", "Users File")] == constants.USERS_XML_PATH
        assert values[("file-user-group-provider", "Initial User Identity 1")] == _IDENTITY
        assert (
            values[("file-access-policy-provider", "Authorizations File")]
            == constants.AUTHORIZATIONS_XML_PATH
        )
        assert values[("file-access-policy-provider", "Initial Admin Identity")] == _IDENTITY
        assert (
            values[("managed-authorizer", "Access Policy Provider")]
            == "file-access-policy-provider"
        )
        assert constants.USERS_XML_PATH.startswith(constants.DATA_DIR)
        assert constants.AUTHORIZATIONS_XML_PATH.startswith(constants.DATA_DIR)

    def test_no_group_seeds(self, generator):
        """Groups and their policies come from the identity work, not from seeds."""
        content = generator.render_authorizers_xml(_IDENTITY)
        assert "Initial Admin Group" not in content
        assert "Initial Group Identity" not in content

    def test_template_error_raises_runtime_error(self):
        gen = NifiPropertiesManager(templates_dir="nonexistent/path")
        with pytest.raises(RuntimeError, match="Failed to render authorizers.xml"):
            gen.render_authorizers_xml(_IDENTITY)
