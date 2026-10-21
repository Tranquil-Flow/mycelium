# SPDX-License-Identifier: AGPL-3.0-only
"""The owner serves the pinned model manifest, so a member can be fully offline.

Real: the owner ``DemoApp`` with its signed local-TLS managed-control plane, a
real member (the owner's local contributor) and ``SelectedModelAcquisition`` on
the member side.  Offline mode is on, no Hugging Face source exists, and every
network opener that could reach it is never built.  No device, no real network.
"""
from __future__ import annotations

import types

import pytest

from mycelium_a13_macos import peer_model_source as pms
from mycelium_a13_macos.acquisition import AcquisitionError, SelectedModelAcquisition
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos.serving import CANDIDATE_MODEL
from mycelium_platform_neutral.offline import OFFLINE_ENV, OfflineModelSourceError
from test_acquisition import _consent
from test_peer_model_source import _relay, owner  # noqa: F401  (pytest fixture)

MODEL = CANDIDATE_MODEL


def _normal(manifest):
    """The owner serves the validated manifest: files sorted by path."""
    return {**manifest, "files": sorted(manifest["files"], key=lambda row: row["path"])}


def _code(error: BaseException) -> str:
    return getattr(error, "code", str(error))


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    monkeypatch.setenv(OFFLINE_ENV, "1")
    monkeypatch.delenv(pms.ENVIRONMENT, raising=False)


# ----------------------------------------------------------- the owner route


def test_member_reads_the_pinned_manifest_from_the_owner(owner):  # noqa: F811
    _, actor, _, _, _, manifest = owner
    served = pms.PeerRangeClient(actor.peer).read_manifest(MODEL.model_id, MODEL.resolved_commit)
    assert served == _normal(manifest)
    assert served["resolved_commit"] == MODEL.resolved_commit


def test_owner_refuses_a_manifest_for_another_commit(owner):  # noqa: F811
    _, actor, _, _, _, _ = owner
    with pytest.raises(ManagedControlError) as caught:
        pms.PeerRangeClient(actor.peer).read_manifest(MODEL.model_id, "f" * 40)
    assert _code(caught.value) == "model_source_model_mismatch"


def test_owner_without_a_prepared_snapshot_serves_no_manifest(owner):  # noqa: F811
    _, actor, source, _, _, _ = owner
    source._source = lambda: None
    with pytest.raises(ManagedControlError) as caught:
        pms.PeerRangeClient(actor.peer).read_manifest(MODEL.model_id, MODEL.resolved_commit)
    assert _code(caught.value) == "model_source_unavailable"


# --------------------------------------------------------------- the member


def test_offline_transport_resolves_the_manifest_from_the_owner(owner):  # noqa: F811
    _, actor, _, _, _, manifest = owner
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=None, offline=True)
    assert transport.resolve(MODEL) == _normal(manifest)


def test_online_fallback_still_resolves_from_the_pinned_origin(owner):  # noqa: F811
    _, actor, source, _, _, manifest = owner
    asked: list[object] = []
    fallback = types.SimpleNamespace(resolve=lambda model: asked.append(model) or manifest,
                                     read_range=lambda *a: b"", timeout_seconds=30.0)
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=fallback)
    assert transport.resolve(MODEL) == manifest
    assert asked == [MODEL]
    assert source.served_bytes(actor.member.node_id) == 0


def _lying_source(document):
    client = types.SimpleNamespace(read=lambda *a: b"", read_manifest=lambda model_id, commit: document)
    return pms.peer_source("owner_lan", client, chunk_bytes=1)


def test_a_manifest_for_a_different_commit_is_refused(owner):  # noqa: F811
    _, _, _, _, _, manifest = owner
    other = {**manifest, "resolved_commit": "e" * 40}
    transport = pms.MultiSourceTransport([_lying_source(other)], fallback=None, offline=True)
    with pytest.raises(AcquisitionError) as caught:
        transport.resolve(MODEL)
    assert _code(caught.value) == "model_manifest_commit_mismatch"


def test_a_manifest_for_a_different_model_is_refused(owner):  # noqa: F811
    _, _, _, _, _, manifest = owner
    other = {**manifest, "model_id": "someone/else"}
    transport = pms.MultiSourceTransport([_lying_source(other)], fallback=None, offline=True)
    with pytest.raises(AcquisitionError) as caught:
        transport.resolve(MODEL)
    assert _code(caught.value) == "model_manifest_commit_mismatch"


def test_a_malformed_manifest_is_refused(owner):  # noqa: F811
    _, _, _, _, _, manifest = owner
    broken = {**manifest, "files": []}
    transport = pms.MultiSourceTransport([_lying_source(broken)], fallback=None, offline=True)
    with pytest.raises(AcquisitionError) as caught:
        transport.resolve(MODEL)
    assert _code(caught.value) == "model_manifest_invalid"


def test_no_owner_manifest_offline_still_names_the_offline_fix():
    peer = pms.peer_source("owner_lan", types.SimpleNamespace(read=lambda *a: b""), chunk_bytes=1)
    transport = pms.MultiSourceTransport([peer], fallback=None, offline=True)
    with pytest.raises(OfflineModelSourceError):
        transport.resolve(MODEL)


# ------------------------------------------------- the whole offline download


def test_offline_member_prepares_the_model_with_no_hugging_face_contact(owner, tmp_path):  # noqa: F811
    _, actor, source, _, files, manifest = owner
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=None, offline=True)
    # No injected manifest and the default Hugging Face transport: offline mode
    # must route the manifest to the owner and never open a Hugging Face URL.
    acquisition = SelectedModelAcquisition(tmp_path / "joiner")
    try:
        acquisition.set_byte_source(transport)
        result = acquisition._run_blocking(_consent(manifest["total_bytes"]))
        assert result["state"] == "prepared", result
        prepared = acquisition.prepared_source()
        for name, data in files.items():
            assert (prepared.source_root / name).read_bytes() == data
        assert source.served_bytes(actor.member.node_id) == manifest["total_bytes"]
    finally:
        acquisition.close()


def test_every_file_is_still_hash_verified_offline(owner, tmp_path):  # noqa: F811
    _, actor, _, root, _, manifest = owner
    target = root / "model.safetensors"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=None, offline=True)
    acquisition = SelectedModelAcquisition(tmp_path / "joiner")
    try:
        acquisition.set_byte_source(transport)
        result = acquisition._run_blocking(_consent(manifest["total_bytes"], download=10 ** 9,
                                                    storage=10 ** 9))
        assert result["state"] == "failed" and result["error_code"] == "artifact_digest_mismatch"
        with pytest.raises(AcquisitionError):
            acquisition.prepared_source()
    finally:
        acquisition.close()


def test_offline_member_refuses_a_foreign_commit_manifest_before_any_byte(owner, tmp_path):  # noqa: F811
    _, actor, source, _, _, manifest = owner
    foreign = {**manifest, "resolved_commit": "d" * 40}
    transport = pms.MultiSourceTransport([_lying_source(foreign)], fallback=None, offline=True)
    acquisition = SelectedModelAcquisition(tmp_path / "joiner")
    try:
        acquisition.set_byte_source(transport)
        result = acquisition._run_blocking(_consent(manifest["total_bytes"]))
        assert result["state"] == "failed"
        assert result["error_code"] == "model_manifest_commit_mismatch"
        assert source.served_bytes(actor.member.node_id) == 0
    finally:
        acquisition.close()


def test_lan_direct_listener_serves_the_manifest_with_pinned_tls(owner):  # noqa: F811
    _, actor, source, _, _, manifest = owner
    lan = pms.OwnerLanModelSource(source, host="127.0.0.1").start()
    try:
        client = pms.PeerRangeClient(actor.peer, lan_origin=lan.origin,
                                     tls_certificate_pem=lan.certificate_pem)
        assert client.read_manifest(MODEL.model_id, MODEL.resolved_commit) == _normal(manifest)
        transport = pms.MultiSourceTransport(
            [pms.peer_source("owner_lan", client, chunk_bytes=pms.LAN_CHUNK_BYTES)],
            fallback=None, offline=True)
        assert transport.resolve(MODEL) == _normal(manifest)
    finally:
        lan.close()
