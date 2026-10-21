# SPDX-License-Identifier: AGPL-3.0-only
"""TDD coverage for bounded A13 selected-model acquisition.

All network positives use tiny loopback HTTP/HTTPS fixtures. They are not public
model-download, conversion, tensor-load, serving, or physical-host evidence.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import socket
import ssl
from pathlib import Path
import struct
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit

import pytest

from mycelium_a13_macos.acquisition import (
    AcquisitionError,
    MANIFEST_PROTOCOL,
    SelectedModelAcquisition,
    _HTTPRepositoryTransport,
)
from mycelium_a13_macos.serving import CANDIDATE_MODEL, PreparedSource


def _git_blob_id(payload: bytes) -> str:
    return hashlib.sha1(b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload).hexdigest()


def _tiny_safetensors() -> bytes:
    names = (
        "model.embed_tokens.weight",
        "model.layers.0.self_attn.q_proj.weight",
        "model.norm.weight",
    )
    header: dict[str, object] = {}
    data = bytearray()
    for name in names:
        start = len(data)
        data.extend(struct.pack("<f", 1.0))
        header[name] = {"dtype": "F32", "shape": [1], "data_offsets": [start, len(data)]}
    encoded = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded + data


def _repository_files(*, slow_payload: bool = False) -> dict[str, bytes]:
    config = {
        "architectures": ["Qwen2ForCausalLM"],
        "model_type": "qwen2",
        "torch_dtype": "bfloat16",
        "num_hidden_layers": 1,
        "hidden_size": 4,
        "num_attention_heads": 1,
        "num_key_value_heads": 1,
        "head_dim": 4,
        "intermediate_size": 8,
        "vocab_size": 8,
        "max_position_embeddings": 16,
        "rms_norm_eps": 1e-6,
        "rope_theta": 10_000.0,
        "hidden_act": "silu",
        "tie_word_embeddings": True,
    }
    files = {
        "config.json": json.dumps(config, sort_keys=True).encode("utf-8"),
        "model.safetensors": _tiny_safetensors(),
        "tokenizer.json": b"{}",
        "tokenizer_config.json": b"{}",
        "vocab.json": b"{}",
        "merges.txt": b"#version: 0.2\n",
    }
    if slow_payload:
        files["README.md"] = b"local-fixture-only\n" * 65_536
    return files


class _FixtureState:
    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.model_id = CANDIDATE_MODEL.model_id
        self.revision = CANDIDATE_MODEL.resolved_commit
        self.reported_model_id = self.model_id
        self.reported_revision = self.revision
        self.requests: list[tuple[str, str | None]] = []
        self.disconnect_file: str | None = None
        self.disconnected = False
        self.ignore_range = False
        self.corrupt_file: str | None = None
        self.forbidden_file: str | None = None
        self.oversized_length_file: str | None = None
        self.slow_file: str | None = None
        self.release_slow = threading.Event()

    def api_document(self) -> dict[str, object]:
        siblings = []
        for name, payload in self.files.items():
            item: dict[str, object] = {
                "rfilename": name,
                "size": len(payload),
                "blobId": _git_blob_id(payload),
            }
            if name.endswith(".safetensors"):
                item["lfs"] = {
                    "sha256": hashlib.sha256(payload).hexdigest(),
                    "size": len(payload),
                    "pointerSize": 128,
                }
            siblings.append(item)
        return {
            "id": self.reported_model_id,
            "modelId": self.reported_model_id,
            "private": False,
            "sha": self.reported_revision,
            "siblings": siblings,
        }


class _Handler(BaseHTTPRequestHandler):
    server_version = "A13Fixture/1"

    def log_message(self, format: str, *args: object) -> None:
        del format, args
        pass

    @property
    def fixture(self) -> _FixtureState:
        return self.server.fixture  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        self.fixture.requests.append((parsed.path, self.headers.get("Range")))
        api_path = (
            "/api/models/"
            + CANDIDATE_MODEL.model_id
            + "/revision/"
            + CANDIDATE_MODEL.resolved_commit
        )
        if parsed.path == api_path:
            body = json.dumps(self.fixture.api_document(), separators=(",", ":")).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        prefix = (
            "/"
            + CANDIDATE_MODEL.model_id
            + "/resolve/"
            + CANDIDATE_MODEL.resolved_commit
            + "/"
        )
        if not parsed.path.startswith(prefix):
            self.send_error(404)
            return
        filename = unquote(parsed.path[len(prefix) :])
        payload = self.fixture.files.get(filename)
        if payload is None:
            self.send_error(404)
            return
        if self.fixture.forbidden_file == filename:
            self.send_response(403)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.fixture.corrupt_file == filename:
            payload = bytes([payload[0] ^ 1]) + payload[1:]
        range_header = self.headers.get("Range")
        start = 0
        partial = False
        if range_header and not self.fixture.ignore_range:
            assert range_header.startswith("bytes=") and range_header.endswith("-")
            start = int(range_header[6:-1])
            partial = True
        selected = payload[start:]
        self.send_response(206 if partial else 200)
        self.send_header("Accept-Ranges", "bytes")
        if partial:
            self.send_header(
                "Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}"
            )
        declared = len(selected)
        if self.fixture.oversized_length_file == filename:
            declared += 1
        self.send_header("Content-Length", str(declared))
        self.send_header("Content-Type", "application/octet-stream")
        self.end_headers()
        if (
            self.fixture.disconnect_file == filename
            and not self.fixture.disconnected
            and len(selected) > 2
        ):
            self.fixture.disconnected = True
            prefix_bytes = selected[: max(1, len(selected) // 2)]
            self.wfile.write(prefix_bytes)
            self.wfile.flush()
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        if self.fixture.slow_file == filename:
            for offset in range(0, len(selected), 1024):
                try:
                    self.wfile.write(selected[offset : offset + 1024])
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                if self.fixture.release_slow.wait(0.005):
                    return
            return
        self.wfile.write(selected)


class _FixtureServer:
    def __init__(self, tmp_path: Path, files: dict[str, bytes], *, https: bool = False) -> None:
        self.tmp_path = tmp_path
        self.fixture = _FixtureState(files)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.fixture = self.fixture  # type: ignore[attr-defined]
        self.server.daemon_threads = True
        self.server.block_on_close = False
        self.context: ssl.SSLContext | None = None
        self.scheme = "http"
        if https:
            openssl = shutil.which("openssl")
            if openssl is None:
                pytest.fail("openssl is required for the real local HTTPS fixture")
            key = tmp_path / "fixture-key.pem"
            certificate = tmp_path / "fixture-cert.pem"
            config = tmp_path / "openssl.cnf"
            config.write_text(
                "[req]\nprompt=no\ndistinguished_name=dn\nx509_extensions=ext\n"
                "[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\n"
                "basicConstraints=critical,CA:TRUE\nkeyUsage=critical,keyCertSign,digitalSignature\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    openssl,
                    "req",
                    "-x509",
                    "-newkey",
                    "rsa:2048",
                    "-nodes",
                    "-keyout",
                    str(key),
                    "-out",
                    str(certificate),
                    "-days",
                    "1",
                    "-config",
                    str(config),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_context.load_cert_chain(certificate, key)
            self.server.socket = server_context.wrap_socket(self.server.socket, server_side=True)
            self.context = ssl.create_default_context(cafile=str(certificate))
            self.scheme = "https"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        host = "localhost" if self.scheme == "https" else "127.0.0.1"
        return f"{self.scheme}://{host}:{self.server.server_address[1]}"

    def transport(self) -> _HTTPRepositoryTransport:
        return _HTTPRepositoryTransport(
            api_origin=self.origin,
            download_origin=self.origin,
            ssl_context=self.context,
            allow_test_http=self.scheme == "http",
            timeout_seconds=2.0,
        )

    def close(self) -> None:
        self.fixture.release_slow.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def __enter__(self) -> _FixtureServer:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _consent(total: int, *, download: int | None = None, storage: int | None = None) -> dict[str, object]:
    return {
        "download_acknowledged": True,
        "max_download_bytes": total if download is None else download,
        "storage_limit_bytes": total if storage is None else storage,
    }


def _file_requests(server: _FixtureServer) -> list[tuple[str, str | None]]:
    return [item for item in server.fixture.requests if "/resolve/" in item[0]]


def _injected_manifest(files: dict[str, bytes], *, expires_at_unix_ms: int | None = None) -> dict[str, object]:
    records = []
    for name, payload in files.items():
        if name.endswith(".safetensors"):
            digest = {"algorithm": "sha256", "value": hashlib.sha256(payload).hexdigest()}
        else:
            digest = {"algorithm": "git-sha1", "value": _git_blob_id(payload)}
        records.append({"path": name, "size_bytes": len(payload), "digest": digest})
    return {
        "protocol": MANIFEST_PROTOCOL,
        "model_id": CANDIDATE_MODEL.model_id,
        "resolved_commit": CANDIDATE_MODEL.resolved_commit,
        "files": records,
        "total_bytes": sum(len(payload) for payload in files.values()),
        "expires_at_unix_ms": expires_at_unix_ms,
    }


class _BombTransport:
    def __init__(self) -> None:
        self.calls = 0

    def resolve(self, _model: object) -> object:
        self.calls += 1
        raise AssertionError("network must not be reached")

    def open_file(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("network must not be reached")


def test_constructor_and_status_do_not_touch_network_and_consent_is_exact(tmp_path: Path) -> None:
    transport = _BombTransport()
    acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=transport)
    assert acquisition.status() == {
        "state": "idle",
        "downloaded_bytes": 0,
        "total_bytes": 0,
        "error_code": None,
        "model": asdict(CANDIDATE_MODEL),
    }
    assert transport.calls == 0
    with pytest.raises(AcquisitionError, match="acquisition_consent_invalid"):
        acquisition.start({"download_acknowledged": True, "max_download_bytes": 1})
    with pytest.raises(AcquisitionError, match="acquisition_consent_invalid"):
        acquisition.start(
            {
                "download_acknowledged": True,
                "max_download_bytes": True,
                "storage_limit_bytes": 1,
            }
        )
    assert transport.calls == 0
    with pytest.raises(AcquisitionError, match="source_not_prepared"):
        acquisition.prepared_source()
    acquisition.close()


def test_production_transport_rejects_plaintext_and_disabled_tls_verification() -> None:
    with pytest.raises(AcquisitionError, match="https_required"):
        _HTTPRepositoryTransport(
            api_origin="http://127.0.0.1:49152",
            download_origin="http://127.0.0.1:49152",
        )
    insecure = ssl._create_unverified_context()  # noqa: SLF001 - negative test only
    with pytest.raises(AcquisitionError, match="transport_configuration_invalid"):
        _HTTPRepositoryTransport(ssl_context=insecure)


def test_real_local_http_download_compiles_native_prepared_source(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "prepared"
        assert result["downloaded_bytes"] == total
        assert result["total_bytes"] == total
        assert result["error_code"] is None
        prepared = acquisition.prepared_source()
        assert isinstance(prepared, PreparedSource)
        assert prepared.model == CANDIDATE_MODEL
        assert prepared.source_bytes == len(files["model.safetensors"])
        assert prepared.manifest["resolved_commit"] == CANDIDATE_MODEL.resolved_commit
        assert prepared.manifest["runtime_model"]["architecture"] == "qwen2"
        assert "fixture-sensitive-canary" not in json.dumps(result)
        assert len(_file_requests(server)) == len(files)
        acquisition.close()


def _seed_stage(
    tmp_path: Path, files: dict[str, bytes], *, omit: tuple[str, ...] = ()
) -> Path:
    """Materialise a LAN-seeded acquisition stage exactly as the 7B seed lands.

    Mirrors the real seed shape (runs/wp8-01-two-mac-05b-terminal/round_setup.py
    target_seed): regular files mode 0600 inside mode-0700 directories at
    ``models--<id>/snapshots/.<commit>.acquiring``.
    """

    state = tmp_path / "state"
    state.mkdir(mode=0o700, exist_ok=True)
    state.chmod(0o700)
    model_root = state / ("models--" + CANDIDATE_MODEL.model_id.replace("/", "--"))
    snapshots = model_root / "snapshots"
    stage = snapshots / ("." + CANDIDATE_MODEL.resolved_commit + ".acquiring")
    stage.mkdir(parents=True, mode=0o700)
    for directory in (snapshots, stage):
        directory.chmod(0o700)
    for name, payload in files.items():
        if name in omit:
            continue
        path = stage / name
        path.write_bytes(payload)
        path.chmod(0o600)
    return stage


def test_complete_seeded_stage_prepares_within_a_small_network_budget(tmp_path: Path) -> None:
    """D-1: model bytes come from the seed; the network budget stays small.

    A complete retained/seeded snapshot must reach ``prepared`` without fetching
    anything, even when the granted download budget is far below the repository
    total (the whole point of D-1: the app cannot download 15.2 GB of weights).
    """

    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        _seed_stage(tmp_path, files)
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        result = acquisition._run_blocking(
            _consent(total, download=len(b"small-metadata-allowance"), storage=total)
        )
        assert result["state"] == "prepared"
        assert result["downloaded_bytes"] == total
        assert result["error_code"] is None
        assert _file_requests(server) == []
        prepared = acquisition.prepared_source()
        assert prepared.model == CANDIDATE_MODEL
        assert prepared.source_bytes == len(files["model.safetensors"])
        acquisition.close()


def test_partial_seed_fetches_only_the_missing_small_asset(tmp_path: Path) -> None:
    """wp8-01's owner leg shape: the seed omits small text files only."""

    files = {**_repository_files(), "README.md": b"readme-asset\n"}
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        _seed_stage(tmp_path, files, omit=("README.md",))
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        result = acquisition._run_blocking(_consent(total, download=9_999, storage=total))
        assert result["state"] == "prepared"
        requested = _file_requests(server)
        assert len(requested) == 1
        assert "README.md" in requested[0][0]
        acquisition.close()


def test_seed_missing_model_bytes_refuses_without_fetching(tmp_path: Path) -> None:
    """ERRATA-1 lesson, fail-closed: a missing weight file must refuse.

    The granted budget is below the missing safetensors file (the fixture's
    stand-in for the 7B's 3.5-3.9 GB shards vs the 11,535,382 B small-assets
    allowance), so the acquisition must refuse before any byte is requested.
    """

    files = _repository_files()
    total = sum(map(len, files.values()))
    budget = 64
    assert len(files["model.safetensors"]) > budget
    assert total > budget
    with _FixtureServer(tmp_path, files) as server:
        _seed_stage(tmp_path, files, omit=("model.safetensors",))
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        result = acquisition._run_blocking(
            _consent(total, download=budget, storage=total)
        )
        assert result["state"] == "failed"
        assert result["error_code"] == "download_limit_exceeded"
        assert _file_requests(server) == []
        acquisition.close()


def test_real_local_https_uses_authenticated_context_and_pinned_paths(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files, https=True) as server:
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        assert acquisition._run_blocking(_consent(total))["state"] == "prepared"
        paths = [path for path, _range in server.fixture.requests]
        assert all(CANDIDATE_MODEL.resolved_commit in path for path in paths)
        assert any(path.startswith("/api/models/") for path in paths)
        acquisition.close()


def test_metadata_identity_and_manifest_paths_fail_before_file_download(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.reported_revision = "0" * 40
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "failed"
        assert result["error_code"] == "model_metadata_identity_mismatch"
        assert _file_requests(server) == []
        acquisition.close()

    manifest = _injected_manifest(files)
    manifest["files"][0]["path"] = "../escape"  # type: ignore[index]
    transport = _BombTransport()
    acquisition = SelectedModelAcquisition(
        tmp_path / "state-traversal", _transport=transport, _manifest=manifest
    )
    result = acquisition._run_blocking(_consent(total))
    assert result["error_code"] == "model_manifest_invalid"
    assert transport.calls == 0
    assert not (tmp_path / "escape").exists()
    acquisition.close()


@pytest.mark.parametrize(
    ("limits", "expected"),
    [
        ({"download": -1}, "download_limit_exceeded"),
        ({"storage": -1}, "storage_limit_exceeded"),
    ],
)
def test_consent_caps_reject_before_any_file_body(
    tmp_path: Path, limits: dict[str, int], expected: str
) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    values = {key: total + delta for key, delta in limits.items()}
    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        result = acquisition._run_blocking(_consent(total, **values))
        assert result["error_code"] == expected
        assert _file_requests(server) == []
        acquisition.close()


def test_disk_availability_is_checked_before_and_during_transfer(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(
            tmp_path / "state",
            _transport=server.transport(),
            _disk_free_bytes=lambda _path: total - 1,
        )
        result = acquisition._run_blocking(_consent(total))
        assert result["error_code"] == "insufficient_disk"
        assert _file_requests(server) == []
        acquisition.close()

    checks = 0

    def shrinking_disk(_path: Path) -> int:
        nonlocal checks
        checks += 1
        return total * 2 if checks < 4 else 0

    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(
            tmp_path / "state-changing-disk",
            _transport=server.transport(),
            _disk_free_bytes=shrinking_disk,
        )
        result = acquisition._run_blocking(_consent(total))
        assert result["error_code"] == "insufficient_disk"
        assert result["downloaded_bytes"] < total
        acquisition.close()


def test_disconnect_retains_partial_and_resume_uses_valid_range(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.disconnect_file = "model.safetensors"
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        failed = acquisition._run_blocking(_consent(total))
        assert failed["state"] == "failed"
        assert failed["error_code"] == "source_disconnected"
        partial_bytes = failed["downloaded_bytes"]
        assert 0 < partial_bytes < total
        prepared = acquisition._run_blocking(_consent(total))
        assert prepared["state"] == "prepared"
        range_headers = [
            value
            for path, value in _file_requests(server)
            if path.endswith("/model.safetensors") and value is not None
        ]
        assert range_headers and range_headers[-1].startswith("bytes=")
        assert acquisition.prepared_source().source_root.is_dir()
        acquisition.close()


def test_ignored_range_response_restarts_without_appending_duplicate_bytes(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.disconnect_file = "model.safetensors"
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        assert acquisition._run_blocking(_consent(total))["state"] == "failed"
        server.fixture.ignore_range = True
        result = acquisition._run_blocking(_consent(total))
        assert result["state"] == "prepared"
        assert result["downloaded_bytes"] == total
        snapshot = acquisition.prepared_source().source_root
        assert (snapshot / "model.safetensors").read_bytes() == files["model.safetensors"]
        acquisition.close()


def test_checksum_mismatch_and_oversized_response_never_prepare(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.corrupt_file = "model.safetensors"
        acquisition = SelectedModelAcquisition(tmp_path / "bad-digest", _transport=server.transport())
        result = acquisition._run_blocking(_consent(total))
        assert result["error_code"] == "artifact_digest_mismatch"
        with pytest.raises(AcquisitionError, match="source_not_prepared"):
            acquisition.prepared_source()
        acquisition.close()

    with _FixtureServer(tmp_path, files) as server:
        server.fixture.oversized_length_file = "config.json"
        acquisition = SelectedModelAcquisition(tmp_path / "oversized", _transport=server.transport())
        result = acquisition._run_blocking(_consent(total))
        assert result["error_code"] == "source_response_invalid"
        assert result["downloaded_bytes"] == 0
        acquisition.close()


def test_prepared_source_rechecks_complete_snapshot_before_return(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        acquisition = SelectedModelAcquisition(
            tmp_path / "state", _transport=server.transport()
        )
        assert acquisition._run_blocking(_consent(total))["state"] == "prepared"
        source_root = acquisition.prepared_source().source_root
        (source_root / "config.json").write_bytes(b"changed-after-compile")
        with pytest.raises(AcquisitionError, match="artifact_digest_mismatch"):
            acquisition.prepared_source()
        assert acquisition.status()["state"] == "failed"
        assert acquisition.status()["error_code"] == "artifact_digest_mismatch"
        acquisition.close()


def test_expired_or_forbidden_source_fails_with_sanitized_code(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    transport = _BombTransport()
    expired = _injected_manifest(files, expires_at_unix_ms=1)
    acquisition = SelectedModelAcquisition(
        tmp_path / "expired", _transport=transport, _manifest=expired
    )
    result = acquisition._run_blocking(_consent(total))
    assert result["error_code"] == "model_manifest_expired"
    assert transport.calls == 0
    acquisition.close()

    with _FixtureServer(tmp_path, files) as server:
        server.fixture.forbidden_file = "config.json"
        acquisition = SelectedModelAcquisition(tmp_path / "forbidden", _transport=server.transport())
        result = acquisition._run_blocking(_consent(total))
        assert result["error_code"] == "source_expired"
        assert set(result) == {"state", "downloaded_bytes", "total_bytes", "error_code", "model"}
        assert server.origin not in json.dumps(result)
        acquisition.close()


def test_symlink_partial_is_rejected_without_network(tmp_path: Path) -> None:
    files = _repository_files()
    total = sum(map(len, files.values()))
    root = tmp_path / "state"
    acquisition = SelectedModelAcquisition(
        root, _transport=_BombTransport(), _manifest=_injected_manifest(files)
    )
    stage = (
        root
        / ("models--" + CANDIDATE_MODEL.model_id.replace("/", "--"))
        / "snapshots"
        / ("." + CANDIDATE_MODEL.resolved_commit + ".acquiring")
    )
    stage.mkdir(parents=True, mode=0o700)
    target = tmp_path / "outside"
    target.write_bytes(b"outside")
    (stage / "config.json.partial").symlink_to(target)
    result = acquisition._run_blocking(_consent(total))
    assert result["error_code"] == "artifact_storage_unsafe"
    assert target.read_bytes() == b"outside"
    acquisition.close()


def test_background_cancel_retains_partial_and_close_is_bounded(tmp_path: Path) -> None:
    files = _repository_files(slow_payload=True)
    total = sum(map(len, files.values()))
    with _FixtureServer(tmp_path, files) as server:
        server.fixture.slow_file = "README.md"
        acquisition = SelectedModelAcquisition(tmp_path / "state", _transport=server.transport())
        acquisition.start(_consent(total))
        with pytest.raises(AcquisitionError, match="acquisition_in_progress"):
            acquisition.start(_consent(total))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and acquisition.status()["downloaded_bytes"] == 0:
            time.sleep(0.01)
        assert acquisition.status()["downloaded_bytes"] > 0
        acquisition.cancel()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and acquisition.status()["state"] not in {
            "cancelled",
            "failed",
        }:
            time.sleep(0.01)
        assert acquisition.status()["state"] == "cancelled"
        assert acquisition.status()["downloaded_bytes"] < total
        started = time.monotonic()
        acquisition.close()
        assert time.monotonic() - started < 3
        assert acquisition.status()["state"] == "closed"


def test_recovery_marks_prior_active_status_interrupted_without_network(tmp_path: Path) -> None:
    root = tmp_path / "state"
    first = SelectedModelAcquisition(root, _transport=_BombTransport())
    first._set_status(state="downloading", downloaded_bytes=7, total_bytes=11, error_code=None)
    # Simulate process death while preserving only durable state; release the test lock.
    first._release_lock_for_test()
    second_transport = _BombTransport()
    second = SelectedModelAcquisition(root, _transport=second_transport)
    assert second.status()["state"] == "failed"
    assert second.status()["error_code"] == "interrupted_transfer"
    assert second.status()["downloaded_bytes"] == 7
    assert second_transport.calls == 0
    second.close()
