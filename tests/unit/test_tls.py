# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the certificates relation: requests, files and statuses."""

import datetime
import pathlib
import shutil
import stat
import xml.etree.ElementTree as ET
from unittest.mock import patch

import ops
import ops.testing
import pytest
from charmlibs.interfaces.tls_certificates import (
    CertificateError,
    CertificateRequestAttributes,
    ProviderCertificate,
    ProviderCertificateError,
    RequirerCertificateRequest,
    TLSCertificatesRequiresV4,
    generate_ca,
    generate_certificate,
    generate_private_key,
)
from conftest import _SENSITIVE_KEY_SECRET, make_ingress_relation
from cryptography import x509

import constants
from charm import NifiK8SOperatorCharm

MODEL = "test-model"
APP = "nifi-k8s"
POD_FQDN = f"{APP}-0.{APP}-endpoints.{MODEL}.svc.cluster.local"
SERVICE_FQDN = f"{APP}.{MODEL}.svc.cluster.local"
CHARM_IDENTITY_CN = f"charm.{APP}.{MODEL}.svc.cluster.local"

_CA_KEY = generate_private_key()
_CA = generate_ca(_CA_KEY, datetime.timedelta(days=30), "test-ca")
_UNIT_KEY = generate_private_key()


_ISSUED: dict[tuple, ProviderCertificate] = {}


def _issue(request: CertificateRequestAttributes) -> ProviderCertificate:
    """Sign a request with the test CA, the way a provider would (one per distinct request)."""
    cache_key = (
        request.common_name,
        frozenset(request.sans_dns or ()),
        frozenset(request.sans_ip or ()),
    )
    if cache_key in _ISSUED:
        return _ISSUED[cache_key]
    csr = request.generate_csr(_UNIT_KEY)
    cert = generate_certificate(csr, _CA, _CA_KEY, datetime.timedelta(days=1))
    issued = ProviderCertificate(
        relation_id=1,
        certificate=cert,
        certificate_signing_request=csr,
        ca=_CA,
        chain=[cert, _CA],
    )
    _ISSUED[cache_key] = issued
    return issued


def _assigned(issued: set[str]):
    """Fake get_assigned_certificate: issue only the requests whose CN is in `issued`."""

    def _get(_self, request):
        if request.common_name in issued:
            return _issue(request), _UNIT_KEY
        return None, None

    return _get


def _state(container, *, tls_relation=True, relations=(), model=None):
    """State with a certificates relation; pass a Relation to reuse it across runs."""
    rels = set(relations)
    if isinstance(tls_relation, ops.testing.Relation):
        rels.add(tls_relation)
    elif tls_relation:
        rels.add(ops.testing.Relation(constants.CERTIFICATES_RELATION))
    return ops.testing.State(
        containers=[container],
        relations=rels,
        secrets={_SENSITIVE_KEY_SECRET},
        config={constants.SENSITIVE_PROPS_KEY_CONFIG: _SENSITIVE_KEY_SECRET.id},
        model=model or ops.testing.Model(name=MODEL),
    )


def _workload_file(out, context, path: str) -> pathlib.Path:
    root = out.get_container(constants.CONTAINER_NAME).get_filesystem(context)
    return root / path.lstrip("/")


@pytest.fixture(autouse=True)
def _pod_fqdn():
    with patch("charm.socket.getfqdn", return_value=POD_FQDN):
        yield


@pytest.fixture()
def client_dir(tmp_path):
    """Keep the charm's client certificate files in a temp dir during tests."""
    directory = tmp_path / constants.CLIENT_TLS_DIR_NAME
    with patch("charm.NifiK8SOperatorCharm._client_tls_dir", return_value=directory):
        yield directory


def _run_with_requests(context, state):
    """Run update-status and return the requests the charm built."""
    with context(context.on.update_status(), state) as mgr:
        server = mgr.charm._server_certificate_request()
        identity = mgr.charm._charm_identity_request()
        mgr.run()
    return server, identity


class TestCertificateRequests:
    def test_server_request_names(self, context, container):
        server, _ = _run_with_requests(context, _state(container, tls_relation=False))
        assert server.common_name == POD_FQDN
        assert server.sans_dns == {POD_FQDN, SERVICE_FQDN}
        assert server.sans_ip is None

    def test_random_id_is_kept(self, context, container):
        server, identity = _run_with_requests(context, _state(container, tls_relation=False))
        assert server.add_unique_id_to_subject_name is True
        assert identity.add_unique_id_to_subject_name is True

    def test_charm_identity_request_name(self, context, container):
        _, identity = _run_with_requests(context, _state(container, tls_relation=False))
        assert identity.common_name == CHARM_IDENTITY_CN

    def test_ingress_hostname_added_as_dns_san(self, context, container):
        ingress = make_ingress_relation("http://nifi.example.com/nifi-test")
        state = _state(container, tls_relation=False, relations=[ingress])
        server, _ = _run_with_requests(context, state)
        assert server.sans_dns == {POD_FQDN, SERVICE_FQDN, "nifi.example.com"}
        assert server.sans_ip is None

    def test_ingress_ip_added_as_ip_san(self, context, container):
        ingress = make_ingress_relation("http://10.2.0.6/nifi-test")
        state = _state(container, tls_relation=False, relations=[ingress])
        server, _ = _run_with_requests(context, state)
        assert server.sans_dns == {POD_FQDN, SERVICE_FQDN}
        assert server.sans_ip == {"10.2.0.6"}


class TestLongNames:
    LONG_MODEL = "nifi-production-env"  # pod FQDN: 67 characters
    UUID = "3f2a9c1b-0000-4000-8000-000000000000"

    def _long_state(self, container, **kwargs):
        model = ops.testing.Model(name=self.LONG_MODEL, uuid=self.UUID)
        return _state(container, model=model, **kwargs)

    def test_falls_back_to_short_names(self, context, container):
        long_pod_fqdn = f"{APP}-0.{APP}-endpoints.{self.LONG_MODEL}.svc.cluster.local"
        with patch("charm.socket.getfqdn", return_value=long_pod_fqdn):
            server, identity = _run_with_requests(
                context, self._long_state(container, tls_relation=False)
            )
        assert server.common_name == f"{APP}-0.3f2a9c1b.svc.cluster.local"
        # Each name falls back on its own; this identity still fits (52 characters).
        assert identity.common_name == f"charm.{APP}.{self.LONG_MODEL}.svc.cluster.local"
        # The full names still go in the SANs.
        assert long_pod_fqdn in server.sans_dns
        assert f"{APP}.{self.LONG_MODEL}.svc.cluster.local" in server.sans_dns

    def test_identity_falls_back_when_too_long(self, context, container):
        model = ops.testing.Model(name="x" * 32, uuid=self.UUID)  # identity: 65 characters
        server, identity = _run_with_requests(
            context, _state(container, tls_relation=False, model=model)
        )
        assert identity.common_name == f"charm.{APP}.3f2a9c1b.svc.cluster.local"

    def test_app_name_too_long_goes_blocked(self, container, client_dir):
        app = "nifi-" + "x" * 40
        context = ops.testing.Context(NifiK8SOperatorCharm, app_name=app)
        long_pod_fqdn = f"{app}-0.{app}-endpoints.{MODEL}.svc.cluster.local"
        with patch("charm.socket.getfqdn", return_value=long_pod_fqdn):
            with context(context.on.update_status(), _state(container)) as mgr:
                assert mgr.charm._certificate_requests() == []
                out = mgr.run()
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_NAME_TOO_LONG)

    def test_only_identity_too_long_sends_no_requests(self, container, client_dir):
        # A 32-character app: the server's short name fits (61 characters), but the
        # identity doesn't (67 full, 65 short), so neither request is sent.
        app = "nifi-" + "x" * 27
        context = ops.testing.Context(NifiK8SOperatorCharm, app_name=app)
        long_pod_fqdn = f"{app}-0.{app}-endpoints.{MODEL}.svc.cluster.local"
        with patch("charm.socket.getfqdn", return_value=long_pod_fqdn):
            with context(context.on.update_status(), _state(container)) as mgr:
                assert mgr.charm._server_certificate_request() is not None
                assert mgr.charm._charm_identity_request() is None
                assert mgr.charm._certificate_requests() == []
                out = mgr.run()
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_NAME_TOO_LONG)


class TestStatuses:
    def test_no_relation_stays_active(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container, tls_relation=False))
        assert out.unit_status == ops.ActiveStatus()
        assert not _workload_file(out, context, constants.TLS_CERT_PATH).exists()
        assert not client_dir.exists()

    @patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned(set()))
    def test_waiting_while_pending_and_nifi_still_configured(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.WaitingStatus(constants.MSG_TLS_WAITING)
        # NiFi is still configured and running on HTTP.
        assert _workload_file(out, context, constants.NIFI_PROPERTIES_PATH).exists()
        assert constants.SERVICE_NAME in out.get_container(constants.CONTAINER_NAME).plan.services

    @pytest.mark.parametrize("event", ["relation_created", "relation_changed"])
    @patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned(set()))
    def test_waiting_right_after_integrate(self, context, container, client_dir, event):
        relation = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        state = _state(container, tls_relation=relation)
        out = context.run(getattr(context.on, event)(relation), state)
        assert out.unit_status == ops.WaitingStatus(constants.MSG_TLS_WAITING)

    @patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned({POD_FQDN}))
    def test_waiting_until_charm_identity_issued(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.WaitingStatus(constants.MSG_TLS_WAITING)
        assert _workload_file(out, context, constants.TLS_CERT_PATH).exists()

    @patch.object(
        TLSCertificatesRequiresV4,
        "get_assigned_certificate",
        _assigned({POD_FQDN, CHARM_IDENTITY_CN}),
    )
    def test_active_once_both_issued(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.ActiveStatus()

    def test_denied_request_goes_blocked_with_reason(self, context, container, client_dir):
        with context(context.on.update_status(), _state(container)) as mgr:
            server = mgr.charm._server_certificate_request()
            csr = server.generate_csr(_UNIT_KEY)
            requirer_csr = RequirerCertificateRequest(
                relation_id=1, certificate_signing_request=csr, is_ca=False
            )
            error = ProviderCertificateError(
                relation_id=1,
                certificate_signing_request=csr,
                error=CertificateError(code=1, name="denied", message="domain not allowed"),
            )
            with (
                patch.object(
                    TLSCertificatesRequiresV4,
                    "get_csrs_from_requirer_relation_data",
                    return_value=[requirer_csr],
                ),
                patch.object(TLSCertificatesRequiresV4, "get_request_error", return_value=error),
            ):
                out = mgr.run()
        assert out.unit_status == ops.BlockedStatus(
            constants.MSG_TLS_DENIED.format(reason="domain not allowed")
        )


@patch.object(
    TLSCertificatesRequiresV4,
    "get_assigned_certificate",
    _assigned({POD_FQDN, CHARM_IDENTITY_CN}),
)
class TestWriteFailures:
    def test_workload_push_failure_goes_blocked(self, context, container, client_dir):
        def _push(self, path, *args, **kwargs):
            if path.startswith(constants.TLS_DIR):
                raise ops.pebble.APIError({}, 500, "error", "push failed")

        with patch.object(ops.Container, "push", autospec=True, side_effect=_push):
            out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_WRITE_FAILED)

    def test_client_file_failure_goes_blocked(self, context, container, tmp_path):
        # A plain file where the directory should be makes mkdir fail with an OSError.
        not_a_dir = tmp_path / constants.CLIENT_TLS_DIR_NAME
        not_a_dir.write_text("")
        with patch("charm.NifiK8SOperatorCharm._client_tls_dir", return_value=not_a_dir):
            out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_WRITE_FAILED)


@patch.object(
    TLSCertificatesRequiresV4,
    "get_assigned_certificate",
    _assigned({POD_FQDN, CHARM_IDENTITY_CN}),
)
class TestFiles:
    def test_workload_files(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container))
        cert = _workload_file(out, context, constants.TLS_CERT_PATH).read_text()
        key = _workload_file(out, context, constants.TLS_KEY_PATH).read_text()
        ca = _workload_file(out, context, constants.TLS_CA_PATH).read_text()
        # The leaf (no intermediates here), the unit key and the provider CA.
        assert cert.count("BEGIN CERTIFICATE") == 1
        assert str(_CA) not in cert
        assert key == f"{_UNIT_KEY}\n"
        assert ca == f"{_CA}\n"

    def test_workload_files_owner_and_mode(self, context, container, client_dir):
        with patch.object(ops.Container, "push", autospec=True) as push:
            context.run(context.on.update_status(), _state(container))
        pushed = {c.args[1]: c.kwargs for c in push.call_args_list}
        assert pushed[constants.TLS_KEY_PATH]["permissions"] == 0o600
        assert pushed[constants.TLS_CERT_PATH]["permissions"] == 0o644
        assert pushed[constants.TLS_CA_PATH]["permissions"] == 0o644
        for path in (constants.TLS_KEY_PATH, constants.TLS_CERT_PATH, constants.TLS_CA_PATH):
            assert pushed[path]["user"] == constants.WORKLOAD_USER
            assert pushed[path]["group"] == constants.WORKLOAD_GROUP

    def test_client_files_only_in_charm_container(self, context, container, client_dir):
        context.run(context.on.update_status(), _state(container))
        assert stat.S_IMODE(client_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE((client_dir / "key.pem").stat().st_mode) == 0o600
        cert = (client_dir / "cert.pem").read_text()
        assert CHARM_IDENTITY_CN in _issue_subject(cert)

    def test_unchanged_files_not_pushed_again(self, context, container, client_dir, tmp_path):
        relation = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        mounted = _container_with_written_tls(context, container, tmp_path, relation)
        with patch.object(ops.Container, "push", autospec=True) as push:
            out = context.run(context.on.update_status(), _state(mounted, tls_relation=relation))
        tls_pushes = [c for c in push.call_args_list if c.args[1].startswith(constants.TLS_DIR)]
        assert tls_pushes == []
        assert out.unit_status == ops.ActiveStatus()


def _container_with_written_tls(
    context, container, tmp_path, relation: ops.testing.Relation
) -> ops.testing.Container:
    """Issue both certificates once, then return a container with those files mounted."""
    with patch.object(
        TLSCertificatesRequiresV4,
        "get_assigned_certificate",
        _assigned({POD_FQDN, CHARM_IDENTITY_CN}),
    ):
        out = context.run(context.on.update_status(), _state(container, tls_relation=relation))
    # The run's filesystem is temporary, so copy the files out of it.
    tls_dir = tmp_path / "tls"
    shutil.copytree(_workload_file(out, context, constants.TLS_DIR), tls_dir)
    return ops.testing.Container(
        name=container.name,
        can_connect=True,
        layers=container.layers,
        check_infos=container.check_infos,
        mounts={"tls": ops.testing.Mount(location=constants.TLS_DIR, source=tls_dir)},
    )


def _issue_subject(pem: str) -> str:
    return x509.load_pem_x509_certificate(pem.encode()).subject.rfc4514_string()


class TestKeepFiles:
    def test_renewal_pending_keeps_files_and_stays_active(
        self, context, container, client_dir, tmp_path
    ):
        relation = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        mounted = _container_with_written_tls(context, container, tmp_path, relation)
        before = (tmp_path / "tls" / "cert.pem").read_text()
        with patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned(set())):
            out = context.run(context.on.update_status(), _state(mounted, tls_relation=relation))
        assert out.unit_status == ops.ActiveStatus()
        assert (tmp_path / "tls" / "cert.pem").read_text() == before

    def test_relation_added_again_waits_for_new_certificates(
        self, context, container, client_dir, tmp_path
    ):
        old = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        mounted = _container_with_written_tls(context, container, tmp_path, old)
        new = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        assert new.id != old.id
        # The new relation's provider hasn't issued yet: old files kept, unit waits.
        with patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned(set())):
            out = context.run(context.on.update_status(), _state(mounted, tls_relation=new))
        assert out.unit_status == ops.WaitingStatus(constants.MSG_TLS_WAITING)
        assert (tmp_path / "tls" / "cert.pem").exists()
        # Once both are issued on the new relation, the unit is active again.
        with patch.object(
            TLSCertificatesRequiresV4,
            "get_assigned_certificate",
            _assigned({POD_FQDN, CHARM_IDENTITY_CN}),
        ):
            out = context.run(context.on.update_status(), _state(mounted, tls_relation=new))
        assert out.unit_status == ops.ActiveStatus()

    def test_relation_removed_keeps_files(self, context, container, client_dir, tmp_path):
        relation = ops.testing.Relation(constants.CERTIFICATES_RELATION)
        mounted = _container_with_written_tls(context, container, tmp_path, relation)
        out = context.run(context.on.update_status(), _state(mounted, tls_relation=False))
        assert out.unit_status == ops.ActiveStatus()
        assert (tmp_path / "tls" / "cert.pem").exists()
        assert (client_dir / "cert.pem").exists()


_INTERMEDIATE_KEY = generate_private_key()
_INTERMEDIATE = generate_certificate(
    CertificateRequestAttributes(common_name="test-intermediate").generate_csr(_INTERMEDIATE_KEY),
    _CA,
    _CA_KEY,
    datetime.timedelta(days=30),
    is_ca=True,
)


def _assigned_via_intermediate(_self, request):
    """Fake get_assigned_certificate: sign through root -> intermediate -> leaf."""
    csr = request.generate_csr(_UNIT_KEY)
    cert = generate_certificate(csr, _INTERMEDIATE, _INTERMEDIATE_KEY, datetime.timedelta(days=1))
    issued = ProviderCertificate(
        relation_id=1,
        certificate=cert,
        certificate_signing_request=csr,
        ca=_CA,
        chain=[cert, _INTERMEDIATE, _CA],
    )
    return issued, _UNIT_KEY


@patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned_via_intermediate)
class TestIntermediateChain:
    """ca.pem holds only the root, so both cert.pem files must carry the intermediate."""

    def test_server_cert_includes_intermediate(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container))
        cert = _workload_file(out, context, constants.TLS_CERT_PATH).read_text()
        assert cert.count("BEGIN CERTIFICATE") == 2
        assert cert.rstrip("\n").endswith(str(_INTERMEDIATE))
        assert str(_CA) not in cert

    def test_client_cert_includes_intermediate(self, context, container, client_dir):
        context.run(context.on.update_status(), _state(container))
        cert = (client_dir / "cert.pem").read_text()
        assert cert.count("BEGIN CERTIFICATE") == 2
        assert cert.rstrip("\n").endswith(str(_INTERMEDIATE))
        assert str(_CA) not in cert
        assert CHARM_IDENTITY_CN in _issue_subject(cert)


def _authorizer_seeds(out, context) -> dict[str, str]:
    """The identities seeded in the written authorizers.xml."""
    root = ET.fromstring(_workload_file(out, context, constants.AUTHORIZERS_XML_PATH).read_text())
    return {p.get("name"): p.text for p in root.iter("property") if "Identity" in p.get("name")}


@patch.object(TLSCertificatesRequiresV4, "get_assigned_certificate", _assigned(set()))
class TestAuthorizer:
    def test_seeded_identity_is_the_charm_certificate_name(self, context, container, client_dir):
        """The seed must match the identity certificate, or the charm is locked out of NiFi."""
        out = context.run(context.on.update_status(), _state(container))
        assert _authorizer_seeds(out, context) == {
            "Initial User Identity 1": f"CN={CHARM_IDENTITY_CN}",
            "Initial Admin Identity": f"CN={CHARM_IDENTITY_CN}",
        }

    def test_short_identity_is_seeded(self, context, container, client_dir):
        model = ops.testing.Model(name="x" * 32, uuid="3f2a9c1b-0000-4000-8000-000000000000")
        out = context.run(context.on.update_status(), _state(container, model=model))
        seeds = _authorizer_seeds(out, context)
        assert seeds["Initial Admin Identity"] == f"CN=charm.{APP}.3f2a9c1b.svc.cluster.local"

    def test_written_with_the_relation_before_certificates(self, context, container, client_dir):
        """It must be on disk before NiFi's first HTTPS start, when the seeds are read."""
        out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.WaitingStatus(constants.MSG_TLS_WAITING)
        assert _workload_file(out, context, constants.AUTHORIZERS_XML_PATH).exists()

    def test_nothing_without_the_relation(self, context, container, client_dir):
        out = context.run(context.on.update_status(), _state(container, tls_relation=False))
        assert out.unit_status == ops.ActiveStatus()
        assert not _workload_file(out, context, constants.AUTHORIZERS_XML_PATH).exists()

    def test_nothing_when_the_name_does_not_fit(self, container, client_dir):
        app = "nifi-" + "x" * 27  # the identity can't fit (see TestLongNames)
        context = ops.testing.Context(NifiK8SOperatorCharm, app_name=app)
        with patch("charm.socket.getfqdn", return_value=f"{app}-0.{app}-endpoints.{MODEL}"):
            out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_NAME_TOO_LONG)
        assert not _workload_file(out, context, constants.AUTHORIZERS_XML_PATH).exists()

    def test_nothing_when_only_the_server_name_does_not_fit(self, container, client_dir):
        app = "nifi-" + "x" * 31  # server name too long; identity fits with model "m"
        context = ops.testing.Context(NifiK8SOperatorCharm, app_name=app)
        model = ops.testing.Model(name="m")
        with patch("charm.socket.getfqdn", return_value=f"{app}-0.{app}-endpoints.m"):
            with context(context.on.update_status(), _state(container, model=model)) as mgr:
                assert mgr.charm._server_certificate_request() is None
                assert mgr.charm._charm_identity_request() is not None
                out = mgr.run()
        assert out.unit_status == ops.BlockedStatus(constants.MSG_TLS_NAME_TOO_LONG)
        assert not _workload_file(out, context, constants.AUTHORIZERS_XML_PATH).exists()

    def test_owner_and_mode(self, context, container, client_dir):
        with patch.object(ops.Container, "push", autospec=True) as push:
            context.run(context.on.update_status(), _state(container))
        pushed = {c.args[1]: c.kwargs for c in push.call_args_list}
        assert pushed[constants.AUTHORIZERS_XML_PATH]["permissions"] == 0o644
        assert pushed[constants.AUTHORIZERS_XML_PATH]["user"] == constants.WORKLOAD_USER
        assert pushed[constants.AUTHORIZERS_XML_PATH]["group"] == constants.WORKLOAD_GROUP

    def test_push_failure_goes_blocked(self, context, container, client_dir):
        def _push(self, path, *args, **kwargs):
            if path == constants.AUTHORIZERS_XML_PATH:
                raise ops.pebble.APIError({}, 500, "error", "push failed")

        with patch.object(ops.Container, "push", autospec=True, side_effect=_push):
            out = context.run(context.on.update_status(), _state(container))
        assert out.unit_status == ops.BlockedStatus(constants.MSG_CONFIG_WRITE_FAILED)

    def test_relation_removed_leaves_the_file(self, context, container, client_dir, tmp_path):
        """NiFi ignores the file over HTTP, and the users it seeded stay on the volume."""
        out = context.run(context.on.update_status(), _state(container))
        conf = tmp_path / "conf"
        shutil.copytree(_workload_file(out, context, f"{constants.NIFI_HOME}/conf"), conf)
        mounted = ops.testing.Container(
            name=container.name,
            can_connect=True,
            layers=container.layers,
            check_infos=container.check_infos,
            mounts={
                "conf": ops.testing.Mount(location=f"{constants.NIFI_HOME}/conf", source=conf)
            },
        )
        out = context.run(context.on.update_status(), _state(mounted, tls_relation=False))
        assert out.unit_status == ops.ActiveStatus()
        assert (conf / "authorizers.xml").exists()


class TestNifiPropertiesMode:
    @pytest.mark.parametrize("tls_relation", [False, True], ids=["http", "certificates"])
    def test_pushed_0600(self, context, container, client_dir, tls_relation):
        """It holds the sensitive properties key."""
        out = context.run(context.on.update_status(), _state(container, tls_relation=tls_relation))
        mode = _workload_file(out, context, constants.NIFI_PROPERTIES_PATH).stat().st_mode
        assert stat.S_IMODE(mode) == 0o600

    def test_readable_file_with_same_content_is_fixed_without_restart(
        self, context, container, tmp_path
    ):
        """An upgrade from a 0644 file fixes the mode but doesn't restart NiFi."""
        out = context.run(context.on.update_status(), _state(container, tls_relation=False))
        conf = tmp_path / "conf"
        shutil.copytree(_workload_file(out, context, f"{constants.NIFI_HOME}/conf"), conf)
        (conf / "nifi.properties").chmod(0o644)
        running = ops.testing.Container(
            name=container.name,
            can_connect=True,
            layers=out.get_container(container.name).layers,
            service_statuses={constants.SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE},
            check_infos=container.check_infos,
            mounts={
                "conf": ops.testing.Mount(location=f"{constants.NIFI_HOME}/conf", source=conf)
            },
        )
        with patch.object(ops.Container, "restart") as restart:
            out = context.run(context.on.update_status(), _state(running, tls_relation=False))
        restart.assert_not_called()
        assert stat.S_IMODE((conf / "nifi.properties").stat().st_mode) == 0o600
        assert out.unit_status == ops.ActiveStatus()
