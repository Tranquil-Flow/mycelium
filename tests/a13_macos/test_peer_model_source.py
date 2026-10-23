# SPDX-License-Identifier: AGPL-3.0-only
"""Joined Macs fetch pinned model bytes from the owner's Mac (run 17 follow-up).

Real: the owner ``DemoApp`` with its signed local-TLS managed-control plane, a
real member (the owner's local contributor: its own membership key, binding
and pinned owner key), ``SelectedModelAcquisition`` and ``fetch_node_slice``
on the member side, and the LAN-direct HTTPS listener on loopback.  The
Hugging Face fallback is the loopback fixture of ``test_acquisition`` (or an
in-memory ranged origin for slices).  No device, no real network.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from mycelium_a13_macos import slice_acquisition as sa
from mycelium_a13_macos.acquisition import AcquisitionError, SelectedModelAcquisition
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.local_contributor import create_local_contributor
from mycelium_a13_macos.managed_control import ManagedControlError
from mycelium_a13_macos import peer_model_source as pms
from mycelium_a13_macos.public_relay import _MAX_FRAME
from mycelium_a13_macos.serving import CANDIDATE_MODEL
from mycelium_qualification.evidence import canonical_json_bytes
from test_acquisition import _FixtureServer, _consent, _file_requests, _injected_manifest, _repository_files
from test_app_authority import CONSENT

MODEL = CANDIDATE_MODEL


def _files() -> dict[str, bytes]:
    files = _repository_files()
    # A file larger than one relay chunk, so chunking and resume are exercised.
    files["README.md"] = bytes(range(256)) * 4096 + b"tail"  # 1 MiB + 4 B
    return files


def _snapshot(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir(mode=0o700, parents=True)
    for name, data in files.items():
        path = root / name
        path.write_bytes(data)
        path.chmod(0o600)
    return root


@pytest.fixture
def owner(tmp_path):
    app = DemoApp(tmp_path / "owner", check_platform=False).start()
    actor = None
    try:
        app.action("/api/create", {"consent": CONSENT})
        actor = create_local_contributor(app)
        files = _files()
        root = _snapshot(tmp_path / "owner-snapshot", files)
        manifest = _injected_manifest(files)
        source = pms.OwnerModelSource(app.authority.control_mailbox,
                                      source=lambda: (MODEL, root, manifest)).install()
        yield app, actor, source, root, files, manifest
    finally:
        if actor is not None:
            actor.stop()
        app.close()


def _relay(actor, *, chunk=pms.RELAY_CHUNK_BYTES):
    return pms.peer_source("owner_relay", pms.PeerRangeClient(actor.peer), chunk_bytes=chunk)


def _code(error: BaseException) -> str:
    return getattr(error, "code", str(error))


# ---------------------------------------------------------------- the route


def test_member_reads_exact_ranges_over_the_signed_control_plane(owner):
    _, actor, source, _, files, _ = owner
    client = pms.PeerRangeClient(actor.peer)
    readme = files["README.md"]
    assert client.read(MODEL.model_id, MODEL.resolved_commit, "README.md", 0, 10) == readme[:10]
    assert client.read(MODEL.model_id, MODEL.resolved_commit, "README.md", len(readme) - 4, 4) == b"tail"
    chunked = pms.MultiSourceTransport([_relay(actor, chunk=100_000)], fallback=None)
    assert chunked.read_range(MODEL.model_id, MODEL.resolved_commit, "README.md", 7, 300_001) \
        == readme[7:300_008]
    assert source.served_bytes(actor.member.node_id) == 14 + 300_001


@pytest.mark.parametrize("path", ["../owner-snapshot/config.json", "/etc/passwd", "sub/../config.json",
                                  "extra.txt", "config.json/..", ".", ""])
def test_paths_outside_the_verified_manifest_are_refused(owner, path):
    _, actor, source, root, _, _ = owner
    (root / "extra.txt").write_bytes(b"not in the pinned manifest")
    with pytest.raises(ManagedControlError) as caught:
        pms.PeerRangeClient(actor.peer).read(MODEL.model_id, MODEL.resolved_commit, path, 0, 1)
    assert _code(caught.value) == "model_source_path_refused"
    assert source.served_bytes(actor.member.node_id) == 0


def test_links_and_wrong_sizes_in_the_owner_snapshot_are_refused(owner, tmp_path):
    _, actor, _, root, files, _ = owner
    outside = tmp_path / "outside-tokenizer.json"
    outside.write_bytes(files["tokenizer.json"])
    (root / "tokenizer.json").unlink()
    (root / "tokenizer.json").symlink_to(outside)
    os.link(root / "vocab.json", tmp_path / "vocab-hardlink")
    (root / "merges.txt").write_bytes(files["merges.txt"] + b"x")
    client = pms.PeerRangeClient(actor.peer)
    for path in ("tokenizer.json", "vocab.json", "merges.txt"):
        with pytest.raises(ManagedControlError) as caught:
            client.read(MODEL.model_id, MODEL.resolved_commit, path, 0, 1)
        assert _code(caught.value) == "model_source_file_unsafe", path


def test_bounds_identity_busy_and_budget(owner):
    app, actor, source, _, files, manifest = owner
    client = pms.PeerRangeClient(actor.peer)
    size = len(files["README.md"])
    cases = [((MODEL.model_id, MODEL.resolved_commit, "README.md", 0, pms.MAX_CHUNK_BYTES + 1),
              "model_source_request_invalid"),
             ((MODEL.model_id, MODEL.resolved_commit, "README.md", size - 1, 2), "model_source_range_invalid"),
             ((MODEL.model_id, MODEL.resolved_commit, "README.md", -1, 2), "model_source_request_invalid"),
             (("Qwen/Qwen3-0.6B", MODEL.resolved_commit, "README.md", 0, 1), "model_source_model_mismatch")]
    for args, code in cases:
        with pytest.raises(ManagedControlError) as caught:
            client.read(*args)
        assert _code(caught.value) == code
    assert source._slots.acquire(blocking=False)
    held = [source._slots.acquire(blocking=False) for _ in range(pms.MAX_CONCURRENT_READS - 1)]
    try:
        with pytest.raises(ManagedControlError) as caught:
            client.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 1)
        assert _code(caught.value) == "model_source_busy"
    finally:
        for _ in range(1 + sum(held)):
            source._slots.release()
    tight = pms.OwnerModelSource(app.authority.control_mailbox, budget_factor=1,
                                 source=source._source).install()
    total = manifest["total_bytes"]
    done = 0
    while done < size:
        count = min(pms.RELAY_CHUNK_BYTES, size - done)
        client.read(MODEL.model_id, MODEL.resolved_commit, "README.md", done, count)
        done += count
    remaining = total - size
    for name in sorted(files):
        if name != "README.md":
            client.read(MODEL.model_id, MODEL.resolved_commit, name, 0, len(files[name]))
            remaining -= len(files[name])
    assert remaining == 0 and tight.served_bytes(actor.member.node_id) == total
    with pytest.raises(ManagedControlError) as caught:
        client.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 1)
    assert _code(caught.value) == "model_source_budget_exhausted"


def test_unavailable_snapshot_and_unknown_route_are_closed(owner):
    app, actor, source, _, _, _ = owner
    source._source = lambda: None
    with pytest.raises(ManagedControlError) as caught:
        pms.PeerRangeClient(actor.peer).read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 1)
    assert _code(caught.value) == "model_source_unavailable"
    assert source.hint() is None


def test_revoked_member_and_tampered_request_get_no_bytes(owner):
    app, actor, source, _, _, _ = owner
    client = pms.PeerRangeClient(actor.peer)
    assert client.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 2)
    # Tamper with a signed request after signing: the signature no longer holds.
    control = actor.peer.staging_control_client()
    original = control._request
    member = actor.peer.staging_control_snapshot()

    def tampered(path, envelope):
        forged = json.loads(json.dumps(envelope))
        forged["statement"]["path"] = "README.md"
        forged["statement"]["length"] = 100
        return original(path, forged)

    control._request = tampered
    client._client = lambda: control
    with pytest.raises(ManagedControlError) as caught:
        client.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 2)
    assert _code(caught.value) in {"managed_control_signature_invalid", "managed_control_remote_error"}
    served = source.served_bytes(member["node_id"])
    # A captured (relay-visible) request replayed inside its freshness window.
    captured = []
    control2 = actor.peer.staging_control_client()
    send = control2._request
    control2._request = lambda path, envelope: captured.append(envelope) or send(path, envelope)
    replay = pms.PeerRangeClient(actor.peer)
    replay._client = lambda: control2
    replay.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 2)
    with pytest.raises(ManagedControlError) as caught:
        send(pms.PATH_READ, captured[0])
    assert _code(caught.value) == "model_source_request_replayed"
    served = source.served_bytes(member["node_id"])
    app.action("/api/revoke", {"node_id": actor.member.node_id, "generation": actor.member.generation})
    with pytest.raises(Exception):
        pms.PeerRangeClient(actor.peer).read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 2)
    assert source.served_bytes(member["node_id"]) == served


def test_relay_chunk_response_fits_the_relay_frame(owner):
    app, actor, source, _, files, _ = owner
    sizes = []
    routes = app.authority.control_mailbox.route_extensions
    handler = routes[pms.PATH_READ]

    def measured(envelope):
        response = handler(envelope)
        sizes.append(len(canonical_json_bytes(dict(response))))
        return response

    routes[pms.PATH_READ] = measured
    data = pms.PeerRangeClient(actor.peer).read(MODEL.model_id, MODEL.resolved_commit, "README.md", 0,
                                                pms.RELAY_CHUNK_BYTES)
    assert data == files["README.md"][:pms.RELAY_CHUNK_BYTES]
    assert sizes and max(sizes) < _MAX_FRAME


# --------------------------------------------------------- whole snapshot


def _stage(state: Path) -> Path:
    return (state / ("models--" + MODEL.model_id.replace("/", "--")) / "snapshots"
            / ("." + MODEL.resolved_commit + ".acquiring"))


def test_joiner_prepares_whole_snapshot_from_owner_without_hugging_face(owner, tmp_path):
    _, actor, source, _, files, manifest = owner
    total = manifest["total_bytes"]
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=None)
    acquisition = SelectedModelAcquisition(tmp_path / "joiner", _manifest=manifest,
                                           _transport=transport)
    try:
        acquisition.set_byte_source(transport)
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "prepared", result
        assert result["downloaded_bytes"] == total
        prepared = acquisition.prepared_source()
        for name, data in files.items():
            assert (prepared.source_root / name).read_bytes() == data
        assert source.served_bytes(actor.member.node_id) == total
        assert [row["name"] for row in transport.status() if row["bytes"]] == ["owner_relay"]
    finally:
        acquisition.close()


def test_tampered_owner_byte_is_rejected_and_refetched_from_hugging_face(owner, tmp_path):
    _, actor, _, root, files, manifest = owner
    target = root / "model.safetensors"
    data = bytearray(target.read_bytes())
    data[-1] ^= 0xFF
    target.write_bytes(bytes(data))
    total = manifest["total_bytes"]
    with _FixtureServer(tmp_path, files) as hf:
        transport = pms.MultiSourceTransport([_relay(actor)], fallback=hf.transport())
        acquisition = SelectedModelAcquisition(tmp_path / "joiner", _manifest=manifest,
                                               _transport=hf.transport())
        try:
            acquisition.set_byte_source(transport)
            # A wasted (tampered) file still counts against the same consent budget.
            result = acquisition._run_blocking(_consent(total, download=2 * total))
            assert result["state"] == "prepared", result
            assert (acquisition.prepared_source().source_root / "model.safetensors").read_bytes() \
                == files["model.safetensors"]
            fetched = {path.rsplit("/", 1)[-1] for path, _ in _file_requests(hf)}
            assert "model.safetensors" in fetched and "config.json" not in fetched
            relay = next(row for row in transport.status() if row["name"] == "owner_relay")
            assert relay["enabled"] is False and relay["reason"] == "digest_mismatch"
        finally:
            acquisition.close()


def test_tampered_owner_byte_fails_closed_without_another_source(owner, tmp_path):
    _, actor, _, root, _, manifest = owner
    target = root / "config.json"
    target.write_bytes(target.read_bytes().replace(b"qwen2", b"qwen3"))
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=None)
    acquisition = SelectedModelAcquisition(tmp_path / "joiner", _manifest=manifest, _transport=transport)
    try:
        acquisition.set_byte_source(transport)
        result = acquisition._run_blocking(_consent(manifest["total_bytes"], download=10 ** 9,
                                                    storage=10 ** 9))
        assert result["state"] == "failed" and result["error_code"] == "artifact_digest_mismatch"
        with pytest.raises(AcquisitionError):
            acquisition.prepared_source()
    finally:
        acquisition.close()


def test_interrupted_peer_transfer_resumes_at_the_partial_offset(owner, tmp_path):
    _, actor, source, _, files, manifest = owner
    total = manifest["total_bytes"]
    readme = files["README.md"]
    client = pms.PeerRangeClient(actor.peer)
    calls = {"n": 0}

    def flaky(model_id, commit, path, offset, length):
        calls["n"] += 1
        if path == "README.md" and offset >= pms.RELAY_CHUNK_BYTES:
            raise pms.ModelSourceError("model_source_busy")
        return client.read(model_id, commit, path, offset, length)

    broken = pms._Source("owner_relay", chunk_bytes=pms.RELAY_CHUNK_BYTES, trusted=False, read=flaky)
    first = pms.MultiSourceTransport([broken], fallback=None)
    state = tmp_path / "joiner"
    acquisition = SelectedModelAcquisition(state, _manifest=manifest, _transport=first)
    try:
        acquisition.set_byte_source(first)
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "failed" and result["error_code"] == "model_source_busy"
    finally:
        acquisition.close()
    partial = _stage(state) / "README.md.partial"
    assert partial.stat().st_size == pms.RELAY_CHUNK_BYTES
    before = source.served_bytes(actor.member.node_id)
    second = pms.MultiSourceTransport([_relay(actor)], fallback=None)
    acquisition = SelectedModelAcquisition(state, _manifest=manifest, _transport=second)
    try:
        acquisition.set_byte_source(second)
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "prepared", result
        # Only the missing tail of README.md (then the other files) crossed the wire.
        assert source.served_bytes(actor.member.node_id) - before == total - pms.RELAY_CHUNK_BYTES
        assert (acquisition.prepared_source().source_root / "README.md").read_bytes() == readme
    finally:
        acquisition.close()


def test_byte_source_cannot_change_during_an_active_acquisition(tmp_path):
    acquisition = SelectedModelAcquisition(tmp_path / "joiner")
    try:
        with pytest.raises(AcquisitionError):
            acquisition.set_byte_source(object())
        acquisition._active = True
        with pytest.raises(AcquisitionError) as caught:
            acquisition.set_byte_source(None)
        assert caught.value.code == "acquisition_in_progress"
    finally:
        acquisition._active = False
        acquisition.close()


def test_cancel_reaches_the_byte_source_between_chunks(tmp_path):
    clock = _Clock()
    data = os.urandom(10_000)
    transport = pms.MultiSourceTransport([_timed_source("owner_relay", clock, 1000, data)], fallback=None,
                                         clock=clock)
    acquisition = SelectedModelAcquisition(tmp_path / "joiner")
    try:
        acquisition.set_byte_source(transport)
        acquisition.cancel()
        with pytest.raises(AcquisitionError) as caught:
            transport.read_range("m/x", "c" * 40, "f", 0, 10)
        assert caught.value.code == "acquisition_cancelled"
        acquisition._begin(_consent(10))  # a new attempt re-arms the source
        assert transport.read_range("m/x", "c" * 40, "f", 0, 10) == data[:10]
    finally:
        acquisition._active = False
        acquisition.close()


# ------------------------------------------------------------------ slices


class _MemoryOrigin:
    """In-memory pinned origin with the ranged ``read_range`` interface."""

    timeout_seconds = 2.0

    def __init__(self, files):
        self.files = files
        self.calls = []

    def read_range(self, model_id, commit, path, offset, length):
        self.calls.append((path, offset, length))
        return self.files[path][offset:offset + length]

    def resolve(self, model):
        raise AssertionError("not used")


def _sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _node_plan(source: bytes, *, path="README.md", start=1000, length=700_000):
    header = b"HDR!"
    body = source[start:start + length]
    segments = [{"offset": 0, "length": len(header), "sha256": _sha(header),
                 "inline_base64": base64.b64encode(header).decode("ascii")}]
    done = 0
    while done < length:
        size = min(300_000, length - done)
        segments.append({"offset": len(header) + done, "length": size,
                         "sha256": _sha(body[done:done + size]), "source_path": path,
                         "source_offset": start + done})
        done += size
    whole = header + body
    plan = {"protocol": sa.PLAN_PROTOCOL, "model_id": MODEL.model_id,
            "resolved_commit": MODEL.resolved_commit,
            "source_files": [{"path": path, "size_bytes": len(source), "content_digest": _sha(source)}],
            "nodes": {"node-a": {"files": [{"path": "deployment/stage.safetensors",
                                            "size_bytes": len(whole), "content_digest": _sha(whole),
                                            "segments": segments}]}}}
    plan["plan_digest"] = sa._digest_value(plan)
    return sa.node_slice_plan(plan, "node-a"), whole


def test_slice_ranges_come_from_the_owner_and_verify_against_plan_digests(owner, tmp_path):
    _, actor, source, _, files, _ = owner
    node, whole = _node_plan(files["README.md"])
    destination = (tmp_path / "slice").resolve()
    destination.mkdir()
    origin = _MemoryOrigin(files)
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=origin)
    report = sa.fetch_node_slice(node, expected_node_plan_digest=node["node_plan_digest"],
                                 destination_root=destination, transport=transport,
                                 max_download_bytes=10 ** 9, max_disk_bytes=10 ** 9)
    assert (destination / "deployment/stage.safetensors").read_bytes() == whole
    assert report["downloaded_bytes"] == 700_000 and origin.calls == []
    assert source.served_bytes(actor.member.node_id) == 700_000


def test_tampered_slice_segment_is_refetched_from_the_pinned_origin(owner, tmp_path):
    _, actor, _, root, files, _ = owner
    node, whole = _node_plan(files["README.md"])
    tampered = bytearray(files["README.md"])
    tampered[1000 + 450_000] ^= 0x01  # inside the second plan segment
    (root / "README.md").write_bytes(bytes(tampered))
    destination = (tmp_path / "slice").resolve()
    destination.mkdir()
    origin = _MemoryOrigin(files)
    transport = pms.MultiSourceTransport([_relay(actor)], fallback=origin)
    sa.fetch_node_slice(node, expected_node_plan_digest=node["node_plan_digest"],
                        destination_root=destination, transport=transport,
                        max_download_bytes=10 ** 9, max_disk_bytes=10 ** 9)
    assert (destination / "deployment/stage.safetensors").read_bytes() == whole
    assert origin.calls and all(call[0] == "README.md" for call in origin.calls)
    relay = next(row for row in transport.status() if row["name"] == "owner_relay")
    assert relay["enabled"] is False and relay["reason"] == "digest_mismatch"
    # Without a pinned origin the same tamper fails closed.
    other = (tmp_path / "slice-2").resolve()
    other.mkdir()
    lone = pms.MultiSourceTransport([_relay(actor)], fallback=None)
    with pytest.raises(sa.SliceError) as caught:
        sa.fetch_node_slice(node, expected_node_plan_digest=node["node_plan_digest"],
                            destination_root=other, transport=lone,
                            max_download_bytes=10 ** 9, max_disk_bytes=10 ** 9)
    assert caught.value.code == "slice_segment_digest_mismatch"
    assert not (other / "deployment/stage.safetensors").exists()


# -------------------------------------------------------------- LAN direct


def test_lan_direct_listener_serves_members_with_pinned_tls(owner):
    _, actor, source, _, files, _ = owner
    lan = pms.OwnerLanModelSource(source, host="127.0.0.1").start()
    try:
        client = pms.PeerRangeClient(actor.peer, lan_origin=lan.origin,
                                     tls_certificate_pem=lan.certificate_pem)
        readme = files["README.md"]  # one LAN chunk: twice a relay chunk
        assert client.read(MODEL.model_id, MODEL.resolved_commit, "README.md", 3, len(readme) - 3) \
            == readme[3:]
        # The certificate the member pins is the one the listener presents.
        other = pms.OwnerLanModelSource(source, host="127.0.0.1")
        try:
            wrong = pms.PeerRangeClient(actor.peer, lan_origin=lan.origin,
                                        tls_certificate_pem=other.certificate_pem)
            with pytest.raises(ManagedControlError) as caught:
                wrong.read(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 1)
            assert _code(caught.value) == "managed_control_unreachable"
        finally:
            other.close()
        transport = pms.MultiSourceTransport(
            [pms.peer_source("owner_lan", wrong, chunk_bytes=pms.LAN_CHUNK_BYTES, fatal=("managed_control_unreachable",)),
             _relay(actor)], fallback=None)
        assert transport.read_range(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 5) \
            == files["config.json"][:5]
        assert [row["enabled"] for row in transport.status()] == [False, True]
    finally:
        lan.close()


def test_lan_listener_refuses_non_members_and_oversized_frames(owner, monkeypatch):
    import http.client
    import ssl
    import threading
    _, actor, source, _, _, _ = owner
    # Hold the oversized-body write until the listener has refused its header.
    # A client still writing at that point can receive EPIPE rather than a status.
    refused = threading.Event()
    original_send_response = pms._LanHandler._send
    original_client_send = http.client.HTTPSConnection.send

    def observed_response(handler, status, document):
        original_send_response(handler, status, document)
        if document.get("error", {}).get("code") == "model_source_frame_invalid":
            refused.set()

    def race_body_write(connection, data):
        if len(data) > pms.MAX_LAN_REQUEST_BYTES:
            assert refused.wait(5), "listener did not refuse the oversized header"
            raise BrokenPipeError(32, "server already refused and closed")
        return original_client_send(connection, data)

    monkeypatch.setattr(pms._LanHandler, "_send", observed_response)
    monkeypatch.setattr(http.client.HTTPSConnection, "send", race_body_write)
    lan = pms.OwnerLanModelSource(source, host="127.0.0.1").start()
    try:
        context = ssl.create_default_context(cadata=lan.certificate_pem)
        port = int(lan.origin.rsplit(":", 1)[1])
        for body, path in ((b"{}", pms.PATH_READ), (None, pms.PATH_READ),
                           (b"{}", "/managed-control/poll")):
            connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=5)
            try:
                if body is None:
                    # The listener refuses solely from Content-Length. Sending
                    # an oversized body after that header races its close and
                    # tests the client's write, not the listener's refusal.
                    connection.putrequest("POST", path)
                    connection.putheader("Content-Type", "application/json")
                    connection.putheader("Content-Length", str(pms.MAX_LAN_REQUEST_BYTES + 1))
                    connection.endheaders()
                else:
                    connection.request("POST", path, body=body,
                                       headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                document = json.loads(response.read())
                assert response.status == (404 if path != pms.PATH_READ else 400)
                assert "data_base64" not in json.dumps(document)
                if body is None:
                    # Reading the response can beat the server callback's set()
                    # after _send returns; join that observation explicitly.
                    assert refused.wait(5), "listener refusal observation did not finish"
                    assert document == {"error": {"code": "model_source_frame_invalid"}}
            finally:
                connection.close()
        assert source.served_bytes(actor.member.node_id) == 0
        assert source._seen == {}  # no oversized/non-member request reached the signed handler
    finally:
        lan.close()


def test_hint_validation_accepts_only_private_https_literals():
    pem = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n"
    base = {"protocol": pms.HINT, "relay": True, "lan_origin": "https://192.168.1.20:5123",
            "tls_certificate_pem": pem}
    assert pms.validate_hint(base)["lan_origin"] == "https://192.168.1.20:5123"
    for origin in ("http://192.168.1.20:5123", "https://8.8.8.8:443", "https://evil.example:443",
                   "https://192.168.1.20:5123/x", "https://user@192.168.1.20:5123", "https://0.0.0.0:1"):
        assert pms.validate_hint({**base, "lan_origin": origin})["lan_origin"] is None
    assert pms.validate_hint({**base, "extra": 1}) is None
    assert pms.validate_hint({**base, "protocol": "other"}) is None
    assert pms.validate_hint(None) is None


# --------------------------------------------------------------- selection


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def _timed_source(name, clock, rate, data, *, trusted=False, fail=None):
    def read(model_id, commit, path, offset, length):
        if fail is not None and fail():
            raise pms.ModelSourceError("model_source_busy")
        clock.now += length / rate
        return data[offset:offset + length]
    return pms._Source(name, chunk_bytes=1000, trusted=trusted, read=read)


def test_slow_peer_is_probed_against_the_next_source_and_overtaken():
    clock = _Clock()
    data = os.urandom(100_000)
    slow = _timed_source("owner_relay", clock, 10_000, data)
    fast = _timed_source("other", clock, 1_000_000, data)
    transport = pms.MultiSourceTransport([slow, fast], fallback=None, clock=clock, probe_bytes=5000,
                                         slow_bytes_per_second=50_000)
    assert transport.read_range("m/x", "c" * 40, "f", 0, 20_000) == data[:20_000]
    status = {row["name"]: row for row in transport.status()}
    assert status["owner_relay"]["bytes"] == 5000  # probe after 5 KB; the probe wins
    assert status["other"]["bytes"] == 15_000
    assert [row["name"] for row in transport.status()] == ["other", "owner_relay"]
    # A fast peer is never probed away from.
    clock2 = _Clock()
    lan = _timed_source("owner_lan", clock2, 10_000_000, data)
    hf = _timed_source("hf", clock2, 100_000_000, data, trusted=True)
    stay = pms.MultiSourceTransport([lan, hf], fallback=None, clock=clock2, probe_bytes=5000,
                                    slow_bytes_per_second=1_000_000)
    stay.read_range("m/x", "c" * 40, "f", 0, 50_000)
    assert {row["name"]: row["bytes"] for row in stay.status()} == {"owner_lan": 50_000, "hf": 0}


def test_failing_source_is_demoted_after_bounded_strikes():
    clock = _Clock()
    data = os.urandom(10_000)
    failures = {"n": 0}

    def fail():
        failures["n"] += 1
        return True

    broken = _timed_source("owner_relay", clock, 1000, data, fail=fail)
    backup = _timed_source("hf", clock, 1000, data, trusted=True)
    transport = pms.MultiSourceTransport([broken, backup], fallback=None, clock=clock)
    assert transport.read_range("m/x", "c" * 40, "f", 0, 3000) == data[:3000]
    assert failures["n"] == 3 and transport.status()[0]["enabled"] is False
    lone = pms.MultiSourceTransport([_timed_source("x", clock, 1000, data, fail=lambda: True)], fallback=None,
                                    clock=clock)
    with pytest.raises(AcquisitionError) as caught:
        lone.read_range("m/x", "c" * 40, "f", 0, 10)
    assert caught.value.code == "model_source_busy"


# ----------------------------------------------------------------- wiring


def test_announcement_carries_the_owner_hint_only_during_a_run(owner, monkeypatch):
    from mycelium_a13_macos.run_exchange import RunExchange, RunExchangeClient
    monkeypatch.setenv(pms.ENVIRONMENT, "relay")
    app, actor, source, _, files, _ = owner
    exchange = RunExchange(app.authority.control_mailbox).install()
    exchange.set_model_source_provider(source.hint)
    client = RunExchangeClient(actor.peer)
    assert client.read()["model_source"] is None  # idle
    exchange.announce(phase="collect", run_id="a13-x-qwen-20261013T150000Z", deployment_id="dep-1",
                      model={"choice": "candidate", "model_id": MODEL.model_id,
                             "resolved_commit": MODEL.resolved_commit})
    announcement = client.read()
    assert announcement["model_source"] == {"protocol": pms.HINT, "relay": True, "lan_origin": None,
                                            "tls_certificate_pem": None}

    class JoinedApp:
        authority = type("A", (), {"peer": actor.peer})()

    transport = pms.joiner_model_source(JoinedApp(), announcement, fallback=_MemoryOrigin(files))
    assert [row["name"] for row in transport.status()] == ["owner_relay", "huggingface"]
    assert transport.read_range(MODEL.model_id, MODEL.resolved_commit, "config.json", 0, 4) \
        == files["config.json"][:4]
    monkeypatch.setenv(pms.ENVIRONMENT, "off")
    assert pms.joiner_model_source(JoinedApp(), announcement) is None
    assert source.hint() is None


def test_owner_exchange_installs_the_model_source_route(tmp_path, monkeypatch):
    from mycelium_a13_macos import demo_app
    monkeypatch.setenv(pms.ENVIRONMENT, "relay")
    app = DemoApp(tmp_path / "owner", check_platform=False).start()
    try:
        app.action("/api/create", {"consent": CONSENT})
        exchange = demo_app.ensure_exchange(app)
        assert pms.PATH_READ in app.authority.control_mailbox.route_extensions
        assert app.model_source_server.available() is False  # nothing prepared yet
        assert exchange.announcement()["model_source"] is None
    finally:
        app.close()


def test_joined_agent_hands_the_peer_source_to_the_acquisition_before_download():
    from mycelium_a13_macos.demo_target import DemoTargetAgent
    events = []
    sentinel = object()

    class Acquisition:
        def status(self):
            return {"state": "idle"}

        def set_byte_source(self, source):
            events.append(("source", source))

    class App:
        acquisition = Acquisition()

        def action(self, path, body):
            events.append(("action", path))

    class Consent:
        def allows(self, app, action):
            return None

        def storage_cap(self, app):
            return 10 ** 9

    agent = DemoTargetAgent(App(), consent=Consent(), source=lambda: None, sink=lambda report: None,
                            model_source=lambda announcement: sentinel)
    announcement = {"model": {"choice": "qwen3-0.6b", "model_id": "Qwen/Qwen3-0.6B",
                              "resolved_commit": "c" * 40}}
    assert agent._ensure_whole_model(announcement) is False
    assert events == [("action", "/api/run-model"), ("source", sentinel),
                      ("action", "/api/model/download")]
