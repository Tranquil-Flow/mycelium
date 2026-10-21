# SPDX-License-Identifier: AGPL-3.0-only
"""Per-member slice fetch: owner-compiled, digest-pinned, ranged, resumable.

Loopback HTTP fixture stands in for the pinned Hugging Face revision (the
production transport is Web-PKI HTTPS only). Tiny synthetic Qwen2/Qwen3
checkpoints; no model load, device or real network.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
from pathlib import Path
import threading

import pytest

from mycelium_a13_macos import slice_acquisition as sa
from mycelium_a13_macos.acquisition import AcquisitionError, _HTTPRepositoryTransport
from mycelium_qualification.safetensors_sharding import shard_qwen2_checkpoint
from _nhost_fixtures import COMMIT, file_rows, tiny_dense_snapshot

MODEL_ID = "test-org/tiny-dense"
RANGES = (range(0, 1), range(1, 3), range(3, 6))


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _operator_plan(shard_root: Path, assignment: dict[str, list[str]]) -> dict:
    manifests = {}
    for node, names in assignment.items():
        rows = [{"path": "physical_inference_node.py", "size_bytes": 3, "content_digest": _sha(b"abc")},
                {"path": "deployment/config.json", "size_bytes": 2, "content_digest": _sha(b"{}")}]
        for name in names:
            data = (shard_root / name).read_bytes()
            rows.append({"path": f"deployment/{name}", "size_bytes": len(data),
                         "content_digest": _sha(data)})
        manifests[node] = {"protocol": "mycelium.controller_transfer_manifest.v1",
                           "files": sorted(rows, key=lambda r: r["path"])}
    return {"controller": {"node_transfer_manifests": {
        "protocol": "mycelium.controller_node_transfer_manifests.v1", "manifests": manifests}}}


def _assignment(count=3):
    return {
        "node-0": ["model-static-embedding.safetensors", "model-stage-001-of-003.safetensors"],
        "node-1": ["model-stage-002-of-003.safetensors"],
        "node-2": ["model-static-embedding.safetensors", "model-static-output.safetensors",
                   "model-stage-003-of-003.safetensors"],
    }


@pytest.fixture(params=[("qwen2", 1), ("qwen3", 2)], ids=["qwen2-single", "qwen3-indexed"])
def owner(request, tmp_path):
    model_type, split_files = request.param
    snapshot = tiny_dense_snapshot(tmp_path / "hf", model_type=model_type, num_layers=6,
                                   split_files=split_files)
    shard_root = tmp_path / "owner" / "stage-shards"
    shard_root.parent.mkdir()
    shard_qwen2_checkpoint(snapshot, shard_root, shard_count=3, layer_ranges=RANGES)
    operator_plan = _operator_plan(shard_root, _assignment())
    plan = sa.compile_slice_plan(model_id=MODEL_ID, resolved_commit=COMMIT, source_root=snapshot,
        source_manifest_files=file_rows(snapshot),
        node_files=sa.node_weight_files(operator_plan), shard_root=shard_root,
        max_segment_bytes=1024)
    return {"snapshot": snapshot, "shard_root": shard_root, "plan": plan,
            "operator_plan": operator_plan, "tmp": tmp_path}


class _Server:
    def __init__(self, snapshot: Path):
        self.snapshot = snapshot
        self.requests: list[tuple[str, str | None]] = []
        self.tamper: set[int] = set()
        self.ignore_range = False
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                prefix = f"/{MODEL_ID}/resolve/{COMMIT}/"
                path = self.path.split("?", 1)[0]
                header = self.headers.get("Range")
                outer.requests.append((path, header))
                if not path.startswith(prefix):
                    self.send_error(404)
                    return
                data = (outer.snapshot / path[len(prefix):]).read_bytes()
                if header is None or outer.ignore_range:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                start, end = (int(x) for x in header.split("=", 1)[1].split("-"))
                body = bytearray(data[start:end + 1])
                if start in outer.tamper:
                    body[0] ^= 0xFF
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(bytes(body))

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def transport(self):
        return _HTTPRepositoryTransport(api_origin=self.origin, download_origin=self.origin,
                                        allow_test_http=True)

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server(owner):
    value = _Server(owner["snapshot"])
    yield value
    value.close()


def _dest(owner, node="node-1") -> Path:
    root = owner["tmp"] / f"target-{node}" / "slices"
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _fetch(owner, server, node="node-1", dest=None, **kw):
    node_plan = sa.node_slice_plan(owner["plan"], node)
    args = dict(expected_node_plan_digest=node_plan["node_plan_digest"],
                destination_root=dest or _dest(owner, node), transport=server.transport(),
                max_download_bytes=1 << 30, max_disk_bytes=1 << 30)
    args.update(kw)
    return node_plan, sa.fetch_node_slice(node_plan, **args)


def test_plan_pins_every_member_file_to_the_compiled_plan_digests(owner):
    plan = owner["plan"]
    assert plan["protocol"] == sa.PLAN_PROTOCOL and plan["plan_digest"].startswith("sha256:")
    for node, names in _assignment().items():
        files = plan["nodes"][node]["files"]
        assert [f["path"] for f in files] == sorted(f"deployment/{n}" for n in names)
        for row in files:
            data = (owner["shard_root"] / row["path"].split("/", 1)[1]).read_bytes()
            assert row["content_digest"] == _sha(data) and row["size_bytes"] == len(data)
            covered = sum(s["length"] for s in row["segments"])
            assert covered == len(data)


def test_plan_compilation_is_deterministic(owner):
    again = sa.compile_slice_plan(model_id=MODEL_ID, resolved_commit=COMMIT,
        source_root=owner["snapshot"], source_manifest_files=file_rows(owner["snapshot"]),
        node_files=sa.node_weight_files(owner["operator_plan"]), shard_root=owner["shard_root"],
        max_segment_bytes=1024)
    assert again == owner["plan"]


def test_member_downloads_only_its_slice(owner, server):
    node_plan, report = _fetch(owner, server, "node-1")
    stage = (owner["shard_root"] / "model-stage-002-of-003.safetensors").read_bytes()
    out = report["destination_root"] / "deployment/model-stage-002-of-003.safetensors"
    assert out.read_bytes() == stage
    assert report["state"] == "slice_verified_not_loaded" and report["route_ready"] is False
    total_weights = sum(p.stat().st_size for p in owner["snapshot"].glob("*.safetensors"))
    assert report["downloaded_bytes"] == node_plan["download_bytes"]
    assert 0 < report["downloaded_bytes"] < total_weights / 2
    assert all(rng is not None and rng.startswith("bytes=") for _, rng in server.requests)
    # Only that member's files exist at the destination: no other stage, no source shard.
    present = sorted(p.relative_to(report["destination_root"]).as_posix()
                     for p in report["destination_root"].rglob("*") if p.is_file())
    assert present == ["deployment/model-stage-002-of-003.safetensors"]


@pytest.mark.parametrize("node", ["node-0", "node-2"])
def test_edge_members_reconstruct_exact_shards(owner, server, node):
    _, report = _fetch(owner, server, node)
    for name in _assignment()[node]:
        assert ((report["destination_root"] / "deployment" / name).read_bytes()
                == (owner["shard_root"] / name).read_bytes())


def test_tampered_range_is_refused_and_nothing_is_published(owner, server):
    node_plan = sa.node_slice_plan(owner["plan"], "node-1")
    source = next(s for s in node_plan["files"][0]["segments"] if "source_path" in s)
    server.tamper.add(source["source_offset"])
    dest = _dest(owner)
    with pytest.raises(sa.SliceError) as caught:
        _fetch(owner, server, "node-1", dest=dest)
    assert caught.value.code == "slice_segment_digest_mismatch"
    assert not (dest / "deployment/model-stage-002-of-003.safetensors").exists()


def test_interrupted_fetch_resumes_without_refetching_verified_segments(owner, server):
    dest = _dest(owner)
    transport = server.transport()
    calls = {"n": 0}
    real = transport.read_range

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            raise AcquisitionError("network_error")
        return real(*args, **kwargs)

    transport.read_range = flaky
    with pytest.raises(sa.SliceError) as caught:
        _fetch(owner, server, "node-1", dest=dest, transport=transport)
    assert caught.value.code == "slice_transport_network_error"
    first = len(server.requests)
    node_plan, report = _fetch(owner, server, "node-1", dest=dest)
    sources = sum(1 for f in node_plan["files"] for s in f["segments"] if "source_path" in s)
    assert first == 2 and len(server.requests) - first == sources - 2
    assert report["reused_bytes"] > 0


def test_refetch_of_verified_slice_is_idempotent_and_offline(owner, server):
    dest = _dest(owner)
    _fetch(owner, server, "node-1", dest=dest)
    before = len(server.requests)
    _, report = _fetch(owner, server, "node-1", dest=dest)
    assert len(server.requests) == before and report["downloaded_bytes"] == 0


def test_download_bound_and_plan_digest_refuse_before_any_request(owner, server):
    node_plan = sa.node_slice_plan(owner["plan"], "node-1")
    with pytest.raises(sa.SliceError) as caught:
        _fetch(owner, server, "node-1", max_download_bytes=node_plan["download_bytes"] - 1)
    assert caught.value.code == "slice_download_limit_exceeded"
    with pytest.raises(sa.SliceError) as caught:
        _fetch(owner, server, "node-1", expected_node_plan_digest="sha256:" + "0" * 64)
    assert caught.value.code == "slice_plan_digest_mismatch"
    tampered = json.loads(json.dumps(node_plan))
    tampered["files"][0]["segments"][1]["source_offset"] += 2
    with pytest.raises(sa.SliceError) as caught:
        sa.fetch_node_slice(tampered, expected_node_plan_digest=node_plan["node_plan_digest"],
            destination_root=_dest(owner, "x"), transport=server.transport(),
            max_download_bytes=1 << 30, max_disk_bytes=1 << 30)
    assert caught.value.code == "slice_plan_digest_mismatch"
    assert server.requests == []


def test_unranged_full_body_response_is_refused(owner, server):
    server.ignore_range = True
    with pytest.raises(sa.SliceError) as caught:
        _fetch(owner, server, "node-1")
    assert caught.value.code == "slice_transport_range_response_invalid"


def test_compile_refuses_shard_disagreeing_with_compiled_plan(owner):
    node_files = sa.node_weight_files(owner["operator_plan"])
    node_files["node-1"][0] = {**node_files["node-1"][0], "content_digest": "sha256:" + "1" * 64}
    with pytest.raises(sa.SliceError) as caught:
        sa.compile_slice_plan(model_id=MODEL_ID, resolved_commit=COMMIT,
            source_root=owner["snapshot"], source_manifest_files=file_rows(owner["snapshot"]),
            node_files=node_files, shard_root=owner["shard_root"])
    assert caught.value.code == "slice_plan_file_digest_mismatch"


def test_compile_refuses_models_above_the_relaxed_d1_size(owner):
    with pytest.raises(sa.SliceError) as caught:
        sa.compile_slice_plan(model_id=MODEL_ID, resolved_commit=COMMIT,
            source_root=owner["snapshot"], source_manifest_files=file_rows(owner["snapshot"]),
            node_files=sa.node_weight_files(owner["operator_plan"]),
            shard_root=owner["shard_root"], max_source_bytes=100)
    assert caught.value.code == "slice_fetch_model_too_large"


def test_inline_bytes_are_header_only_and_bounded(owner):
    for node in _assignment():
        node_plan = sa.node_slice_plan(owner["plan"], node)
        for row in node_plan["files"]:
            inline = [s for s in row["segments"] if "inline_base64" in s]
            assert len(inline) == 1 and inline[0]["offset"] == 0
            assert len(base64.b64decode(inline[0]["inline_base64"])) == inline[0]["length"]
        assert node_plan["inline_bytes"] <= sa.MAX_INLINE_BYTES_PER_NODE


def test_slice_prepositions_bind_into_target_staging(owner, server):
    from mycelium_a13_macos.managed_staging import TargetStaging
    dest = _dest(owner, "node-1")
    node_plan, _ = _fetch(owner, server, "node-1", dest=dest)
    manifests = owner["operator_plan"]["controller"]["node_transfer_manifests"]["manifests"]
    base = {row["path"]: row for m in manifests.values() for row in m["files"]}
    controller = {
        "peers": [{"node_id": n, "process_transport": "managed_https"} for n in manifests],
        "transfer_manifest": {"protocol": "mycelium.controller_transfer_manifest.v1",
                              "files": sorted(base.values(), key=lambda r: r["path"])},
        "node_transfer_manifests": owner["operator_plan"]["controller"]["node_transfer_manifests"],
    }
    roots = {n: str(_dest(owner, n) if n != "node-1" else dest) for n in manifests}
    plans = {n: sa.node_slice_plan(owner["plan"], n) for n in manifests}
    bound = sa.bind_slice_prepositions(controller, node_plans=plans, target_slice_roots=roots)
    retained = bound["node_transfer_manifests"]["manifests"]["node-1"]["files"]
    assert [r["path"] for r in retained] == ["deployment/config.json", "physical_inference_node.py"]
    member = bound["prepositioned_artifacts"]["members"]["node-1"]
    assert [r["destination_path"] for r in member] == ["deployment/model-stage-002-of-003.safetensors"]
    staging = TargetStaging(owner["tmp"] / "stage-state", node_id="node-1",
        max_archive_bytes=1 << 20, max_disk_bytes=1 << 30, current_member=lambda: True,
        authorize=lambda intent: True, artifact_cache_root=dest)
    import io, tarfile
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        info = tarfile.TarInfo("deployment/config.json"); info.size = 2
        tar.addfile(info, io.BytesIO(b"{}"))
    files, _ = staging._files(archive.getvalue(), {
        "protocol": "mycelium.controller_prepositioned_member_artifacts.v1", "files": member})
    row = node_plan["files"][0]
    assert files[row["path"]] == (row["size_bytes"], row["content_digest"])
    # A node plan that disagrees with the transfer manifest refuses binding.
    bad = json.loads(json.dumps(plans))
    bad["node-1"]["files"][0]["content_digest"] = "sha256:" + "2" * 64
    with pytest.raises(sa.SliceError):
        sa.bind_slice_prepositions(controller, node_plans=bad, target_slice_roots=roots)
