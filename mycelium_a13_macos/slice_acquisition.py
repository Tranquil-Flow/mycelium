# SPDX-License-Identifier: AGPL-3.0-only
"""Per-member slice acquisition for small models (owner decision D-1, relaxed).

Owner side: ``compile_slice_plan`` maps every byte of each member's compiled
stage-shard files (the builder's ``--stage-sharded`` output, read from where
the builder emitted them: ``<--emit-stage-shards>/model``) either to an
exact byte range of a file in the pinned Hugging Face revision or to a small
inline header, and pins a sha256 for every segment and every whole file. The
whole-file digests must equal the compiled operator plan's transfer rows, so a
member's reconstructed file is byte-identical to what qualification expects.

Owner disk: the owner's own slice files are byte-identical to the deployment
files its compile just wrote, so a transport may offer ``clone_candidate(path)``.
``fetch_node_slice`` then makes each such file an APFS clone (shared blocks,
own inode, ``st_nlink`` 1 -- never a hardlink), hashes the clone against the
plan's pinned digest, and only publishes it on a match; any other outcome falls
back to the ranged fetch. A cloned file needs no new data blocks, so it is not
counted against free disk.

The stage shards carry the source tensors' raw bf16 bytes: the builder's
shard writer copies tensors unchanged, and the runtime dtype and int8
weight-only quantization are applied when a stage is loaded, not written into
the shards. Rebuilding a shard therefore needs no converter on the member, and
the same plan serves every runtime. A shard tensor whose dtype, shape or bytes
differ from its source tensor fails closed (``slice_source_tensor_mismatch``).

Member side: ``fetch_node_slice`` fetches ONLY its own segments with HTTP
Range requests from the pinned revision, verifies each segment before it is
journalled, verifies each whole file before an atomic publish, resumes at
segment granularity and is idempotent. No tensor runtime, model load, worker
start or route readiness happens here.

Why ranged HF fetch instead of owner-served packs (relay-only venue): the
owner's reachability is a cloudflared quick tunnel (30-3600 s lifetime) with
target-initiated polling over ~40 ms hops and <=32 MiB control archives, and
every member's slice would cross the owner's single venue uplink. Ranged HTTPS
needs only outbound connections (no inbound, no peer-to-peer), spreads load
over the CDN, survives a tunnel restart, and loses no integrity: trust comes
from the owner-compiled digests, not from the byte source. The ``transport``
seam takes any object with ``read_range(model_id, commit, path, offset,
length)``: ``peer_model_source.MultiSourceTransport`` reads the same ranges from
the owner's verified snapshot (LAN or relay) first, with this ranged HTTPS as
the fallback, and ``reject`` re-fetches a segment that failed its digest.
"""
from __future__ import annotations

import base64
import binascii
import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
from typing import Any, Callable, Mapping

PLAN_PROTOCOL = "mycelium.a13_slice_plan.v1"
NODE_PLAN_PROTOCOL = "mycelium.a13_node_slice_plan.v1"
REPORT_PROTOCOL = "mycelium.a13_slice_fetch_report.v1"
MAX_SEGMENT_BYTES = 16 * 1024 * 1024
MAX_INLINE_BYTES_PER_NODE = 4 * 1024 * 1024
MAX_FILES_PER_NODE = 64
MAX_SEGMENTS_PER_FILE = 16384
_MAX_HEADER_BYTES = 100 * 1024 * 1024
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_WORK = ".slice-work"


class SliceError(ValueError):
    """A stable, non-sensitive slice plan/fetch refusal code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _digest_value(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _relative(value: Any) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > 1024:
        raise SliceError("slice_path_invalid")
    path = PurePosixPath(value)
    if (path.is_absolute() or path.as_posix() != value or len(path.parts) > 16
            or any(part in {"", ".", ".."} or part.startswith(".") for part in path.parts)):
        raise SliceError("slice_path_invalid")
    return path


def _header(path: Path) -> tuple[list[tuple[str, dict[str, Any]]], int]:
    """Tensor records in data order and the absolute data start."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(8)
            if len(raw) != 8:
                raise ValueError
            length = struct.unpack("<Q", raw)[0]
            if not 2 <= length <= _MAX_HEADER_BYTES:
                raise ValueError
            body = handle.read(length)
            if len(body) != length:
                raise ValueError
        document = json.loads(body)
        if not isinstance(document, dict):
            raise ValueError
        document.pop("__metadata__", None)
        records = []
        for name, record in document.items():
            start, end = record["data_offsets"]
            if (type(start) is not int or type(end) is not int or not 0 <= start <= end
                    or set(record) != {"dtype", "shape", "data_offsets"}):
                raise ValueError
            records.append((name, record))
        records.sort(key=lambda item: item[1]["data_offsets"][0])
        previous = 0
        for _name, record in records:
            if record["data_offsets"][0] != previous:
                raise ValueError
            previous = record["data_offsets"][1]
        if path.stat().st_size != 8 + length + previous:
            raise ValueError
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise SliceError("slice_safetensors_invalid") from None
    return records, 8 + length


def _read(path: Path, offset: int, length: int) -> bytes:
    with path.open("rb") as handle:
        handle.seek(offset)
        data = handle.read(length)
    if len(data) != length:
        raise SliceError("slice_source_truncated")
    return data


def node_weight_files(operator_plan: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Each node's deployment ``*.safetensors`` rows from a compiled plan."""
    try:
        document = operator_plan["controller"]["node_transfer_manifests"]
        if document["protocol"] != "mycelium.controller_node_transfer_manifests.v1":
            raise ValueError
        result = {}
        for node, manifest in document["manifests"].items():
            rows = [copy.deepcopy(dict(row)) for row in manifest["files"]
                    if row["path"].startswith("deployment/") and row["path"].endswith(".safetensors")]
            result[node] = sorted(rows, key=lambda row: row["path"])
        return result
    except (KeyError, TypeError, ValueError, AttributeError):
        raise SliceError("slice_operator_plan_invalid") from None


def _source_index(source_root: Path, rows: list[Mapping[str, Any]], max_source_bytes: int):
    weights = []
    for row in rows:
        path = _relative(row.get("path"))
        if path.suffix != ".safetensors":
            continue
        digest = row.get("content_digest")
        size = row.get("size_bytes")
        if (not isinstance(digest, Mapping) or digest.get("algorithm") != "sha256"
                or type(size) is not int or size <= 0):
            raise SliceError("slice_source_manifest_invalid")
        weights.append({"path": path.as_posix(), "size_bytes": size,
                        "content_digest": "sha256:" + str(digest.get("value"))})
    if not weights:
        raise SliceError("slice_source_manifest_invalid")
    if sum(row["size_bytes"] for row in weights) > max_source_bytes:
        raise SliceError("slice_fetch_model_too_large")
    tensors: dict[str, tuple[str, int, dict[str, Any]]] = {}
    for row in weights:
        local = source_root / row["path"]
        if not local.is_file() or local.stat().st_size != row["size_bytes"]:
            raise SliceError("slice_source_manifest_invalid")
        records, start = _header(local)
        for name, record in records:
            if name in tensors:
                raise SliceError("slice_source_tensor_duplicate")
            tensors[name] = (row["path"], start + record["data_offsets"][0], record)
    return {row["path"]: row for row in weights}, tensors


def _file_segments(shard: Path, source_root: Path, tensors, max_segment_bytes: int):
    records, data_start = _header(shard)
    header = _read(shard, 0, data_start)
    segments: list[dict[str, Any]] = [{"offset": 0, "length": data_start, "sha256": _sha(header),
        "inline_base64": base64.b64encode(header).decode("ascii")}]
    raw: list[list[Any]] = []  # [offset, length, source_path, source_offset]
    for name, record in records:
        if name not in tensors:
            raise SliceError("slice_source_tensor_missing")
        source_path, source_offset, source_record = tensors[name]
        start, end = record["data_offsets"]
        if (source_record["dtype"] != record["dtype"] or source_record["shape"] != record["shape"]
                or source_record["data_offsets"][1] - source_record["data_offsets"][0] != end - start):
            raise SliceError("slice_source_tensor_mismatch")
        offset = data_start + start
        if (raw and raw[-1][2] == source_path and raw[-1][0] + raw[-1][1] == offset
                and raw[-1][3] + raw[-1][1] == source_offset):
            raw[-1][1] += end - start
        else:
            raw.append([offset, end - start, source_path, source_offset])
    for offset, length, source_path, source_offset in raw:
        done = 0
        while done < length:
            size = min(max_segment_bytes, length - done)
            data = _read(shard, offset + done, size)
            if _sha(_read(source_root / source_path, source_offset + done, size)) != _sha(data):
                raise SliceError("slice_source_tensor_mismatch")
            segments.append({"offset": offset + done, "length": size, "sha256": _sha(data),
                             "source_path": source_path, "source_offset": source_offset + done})
            done += size
    if len(segments) > MAX_SEGMENTS_PER_FILE:
        raise SliceError("slice_segment_count_exceeded")
    return segments


def compile_slice_plan(*, model_id: str, resolved_commit: str, source_root: str | Path,
                       source_manifest_files: list[Mapping[str, Any]],
                       node_files: Mapping[str, list[Mapping[str, Any]]],
                       shard_root: str | Path, max_source_bytes: int | None = None,
                       max_segment_bytes: int = MAX_SEGMENT_BYTES) -> dict[str, Any]:
    """Owner-side, deterministic, digest-pinned per-member slice recipes."""
    from .serving import SLICE_FETCH_MAX_SOURCE_BYTES
    limit = SLICE_FETCH_MAX_SOURCE_BYTES if max_source_bytes is None else max_source_bytes
    if (not isinstance(model_id, str) or "/" not in model_id
            or not isinstance(resolved_commit, str) or _COMMIT_RE.fullmatch(resolved_commit) is None
            or type(max_segment_bytes) is not int or not 0 < max_segment_bytes <= MAX_SEGMENT_BYTES
            or type(limit) is not int or limit <= 0
            or not isinstance(node_files, Mapping) or not node_files):
        raise SliceError("slice_plan_request_invalid")
    source_root = Path(source_root).resolve(strict=True)
    shard_root = Path(shard_root).resolve(strict=True)
    sources, tensors = _source_index(source_root, list(source_manifest_files), limit)
    nodes: dict[str, Any] = {}
    used: set[str] = set()
    for node in sorted(node_files):
        if not isinstance(node, str) or _NODE_RE.fullmatch(node) is None:
            raise SliceError("slice_plan_request_invalid")
        rows = node_files[node]
        if not rows or len(rows) > MAX_FILES_PER_NODE:
            raise SliceError("slice_plan_request_invalid")
        files = []
        inline = 0
        for row in sorted(rows, key=lambda item: item["path"]):
            path = _relative(row.get("path"))
            if path.parts[0] != "deployment" or len(path.parts) != 2 or path.suffix != ".safetensors":
                raise SliceError("slice_plan_request_invalid")
            shard = shard_root / path.parts[1]
            if (not shard.is_file() or shard.is_symlink()
                    or shard.stat().st_size != row.get("size_bytes")
                    or _file_sha(shard) != row.get("content_digest")):
                raise SliceError("slice_plan_file_digest_mismatch")
            segments = _file_segments(shard, source_root, tensors, max_segment_bytes)
            inline += sum(s["length"] for s in segments if "inline_base64" in s)
            used.update(s["source_path"] for s in segments if "source_path" in s)
            files.append({"path": path.as_posix(), "size_bytes": row["size_bytes"],
                          "content_digest": row["content_digest"], "segments": segments})
        if inline > MAX_INLINE_BYTES_PER_NODE:
            raise SliceError("slice_inline_limit_exceeded")
        nodes[node] = {"files": files}
    plan = {"protocol": PLAN_PROTOCOL, "model_id": model_id, "resolved_commit": resolved_commit,
            "source_files": [sources[path] for path in sorted(used)], "nodes": nodes}
    plan["plan_digest"] = _digest_value(plan)
    return plan


def node_slice_plan(plan: Mapping[str, Any], node_id: str) -> dict[str, Any]:
    """The single-member projection a target receives (and pins by digest)."""
    body = {k: v for k, v in plan.items() if k != "plan_digest"}
    if plan.get("protocol") != PLAN_PROTOCOL or _digest_value(body) != plan.get("plan_digest"):
        raise SliceError("slice_plan_digest_mismatch")
    if node_id not in plan["nodes"]:
        raise SliceError("slice_plan_node_unknown")
    files = copy.deepcopy(plan["nodes"][node_id]["files"])
    used = {s["source_path"] for f in files for s in f["segments"] if "source_path" in s}
    value = {"protocol": NODE_PLAN_PROTOCOL, "plan_digest": plan["plan_digest"],
             "model_id": plan["model_id"], "resolved_commit": plan["resolved_commit"],
             "node_id": node_id,
             "source_files": [row for row in plan["source_files"] if row["path"] in used],
             "files": files,
             "download_bytes": sum(s["length"] for f in files for s in f["segments"]
                                   if "source_path" in s),
             "inline_bytes": sum(s["length"] for f in files for s in f["segments"]
                                 if "inline_base64" in s)}
    value["node_plan_digest"] = _digest_value(value)
    return value


def _validated_node_plan(node_plan: Mapping[str, Any], expected: str) -> dict[str, Any]:
    try:
        plan = json.loads(json.dumps(node_plan, allow_nan=False))
        if not isinstance(expected, str) or _DIGEST_RE.fullmatch(expected) is None:
            raise SliceError("slice_plan_digest_mismatch")
        body = {k: v for k, v in plan.items() if k != "node_plan_digest"}
        if (plan.get("protocol") != NODE_PLAN_PROTOCOL or plan.get("node_plan_digest") != expected
                or _digest_value(body) != expected):
            raise SliceError("slice_plan_digest_mismatch")
        sizes = {}
        for row in plan["source_files"]:
            _relative(row["path"])
            if type(row["size_bytes"]) is not int or row["size_bytes"] <= 0:
                raise ValueError
            sizes[row["path"]] = row["size_bytes"]
        files = plan["files"]
        if not isinstance(files, list) or not 0 < len(files) <= MAX_FILES_PER_NODE:
            raise ValueError
        paths = [row["path"] for row in files]
        if paths != sorted(set(paths)):
            raise ValueError
        download = inline = 0
        for row in files:
            _relative(row["path"])
            if (type(row["size_bytes"]) is not int or row["size_bytes"] <= 0
                    or _DIGEST_RE.fullmatch(row["content_digest"]) is None
                    or not 0 < len(row["segments"]) <= MAX_SEGMENTS_PER_FILE):
                raise ValueError
            cursor = 0
            for segment in row["segments"]:
                if (segment["offset"] != cursor or type(segment["length"]) is not int
                        or not 0 < segment["length"] <= MAX_SEGMENT_BYTES
                        or _DIGEST_RE.fullmatch(segment["sha256"]) is None):
                    raise ValueError
                if "inline_base64" in segment:
                    if set(segment) != {"offset", "length", "sha256", "inline_base64"}:
                        raise ValueError
                    data = base64.b64decode(segment["inline_base64"], validate=True)
                    if len(data) != segment["length"] or _sha(data) != segment["sha256"]:
                        raise ValueError
                    inline += len(data)
                else:
                    if set(segment) != {"offset", "length", "sha256", "source_path", "source_offset"}:
                        raise ValueError
                    size = sizes[segment["source_path"]]
                    if (type(segment["source_offset"]) is not int or segment["source_offset"] < 0
                            or segment["source_offset"] + segment["length"] > size):
                        raise ValueError
                    download += segment["length"]
                cursor += segment["length"]
            if cursor != row["size_bytes"]:
                raise ValueError
        if (download != plan["download_bytes"] or inline != plan["inline_bytes"]
                or inline > MAX_INLINE_BYTES_PER_NODE):
            raise ValueError
        return plan
    except SliceError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError, binascii.Error):
        raise SliceError("slice_plan_invalid") from None


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise SliceError("slice_destination_invalid")
    return path


def _atomic_json(path: Path, value: Any) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as handle:
        handle.write(_canonical(value))
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _clone_file(source_fd: int, destination: Path) -> bool:
    """APFS clone of an open regular file; False where cloning is unsupported."""
    try:
        import ctypes
        function = ctypes.CDLL(None, use_errno=True).fclonefileat
    except (AttributeError, OSError, ImportError):
        return False
    function.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    function.restype = ctypes.c_int
    # AT_FDCWD (-2), CLONE_NOFOLLOW (1): the destination path is never followed.
    if function(source_fd, -2, os.fsencode(destination), 1) == 0:
        return True
    return False


def _clone_verified(row: Mapping[str, Any], root: Path, clone_candidate: Callable[[str], Any]) -> bool:
    """Publish ``row`` as a verified clone of a local candidate, or return False."""
    try:
        candidate = clone_candidate(row["path"])
    except Exception:
        return False
    if candidate is None:
        return False
    work = root / _WORK / ("clone-" + _digest_value(row).split(":", 1)[1])
    shutil.rmtree(work, ignore_errors=True)
    _private_dir(work)
    partial = work / "partial"
    try:
        try:
            fd = os.open(Path(candidate), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            return False
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid()
                    or info.st_size != row["size_bytes"] or not _clone_file(fd, partial)):
                return False
        finally:
            os.close(fd)
        info = partial.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size != row["size_bytes"]
                or _file_sha(partial) != row["content_digest"]):
            return False
        os.chmod(partial, 0o600)
        final = root / row["path"]
        _private_dir(final.parent)
        os.replace(partial, final)
        return True
    except OSError:
        return False
    finally:
        shutil.rmtree(work, ignore_errors=True)


_MAX_SEGMENT_RETRIES = 2


def _fetch_segment(transport: Any, plan: Mapping[str, Any],
                   segment: Mapping[str, Any]) -> tuple[bytes, int, int]:
    """One plan segment, digest-verified; (data, range requests, bytes received).

    A transport with ``reject`` (peer_model_source) quarantines the source of a
    segment that failed its pinned digest and the segment is fetched again from
    what remains, at most ``_MAX_SEGMENT_RETRIES`` times; otherwise fail closed.
    """
    from .acquisition import AcquisitionError
    requests = received = 0
    for attempt in range(_MAX_SEGMENT_RETRIES + 1):
        try:
            data = transport.read_range(plan["model_id"], plan["resolved_commit"],
                segment["source_path"], segment["source_offset"], segment["length"])
        except AcquisitionError as exc:
            raise SliceError("slice_transport_" + exc.code) from None
        requests += 1
        if isinstance(data, bytes):
            received += len(data)
        if (isinstance(data, bytes) and len(data) == segment["length"]
                and _sha(data) == segment["sha256"]):
            return data, requests, received
        reject = getattr(transport, "reject", None)
        if (reject is None or attempt == _MAX_SEGMENT_RETRIES
                or reject(segment["source_path"], segment["source_offset"], segment["length"]) is not True):
            break
    raise SliceError("slice_segment_digest_mismatch")


def fetch_node_slice(node_plan: Mapping[str, Any], *, expected_node_plan_digest: str,
                     destination_root: str | Path, transport: Any, max_download_bytes: int,
                     max_disk_bytes: int,
                     should_stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Fetch, verify and publish exactly this member's slice files.

    Every segment is sha256-checked before it is journalled; every file is
    sha256-checked against the plan before an atomic publish. Already-verified
    files and journalled segments are reused (resumable, idempotent). Files the
    transport can offer locally (``clone_candidate``) are cloned and verified
    first; only the rest are fetched by range and need free disk.
    """
    plan = _validated_node_plan(node_plan, expected_node_plan_digest)
    if (type(max_download_bytes) is not int or type(max_disk_bytes) is not int
            or max_download_bytes < 0 or max_disk_bytes <= 0 or not callable(getattr(transport, "read_range", None))):
        raise SliceError("slice_fetch_request_invalid")
    if plan["download_bytes"] > max_download_bytes:
        raise SliceError("slice_download_limit_exceeded")
    if sum(row["size_bytes"] for row in plan["files"]) > max_disk_bytes:
        raise SliceError("slice_disk_limit_exceeded")
    root = Path(destination_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir() or root.resolve() != root:
        raise SliceError("slice_destination_invalid")
    stop = should_stop or (lambda: False)
    downloaded = reused = requests = cloned = 0
    pending = []
    for row in plan["files"]:
        final = root / row["path"]
        if final.is_symlink():
            raise SliceError("slice_destination_conflict")
        if final.exists():
            if (not final.is_file() or final.stat().st_size != row["size_bytes"]
                    or _file_sha(final) != row["content_digest"]):
                raise SliceError("slice_destination_conflict")
            reused += row["size_bytes"]
            continue
        pending.append(row)
    clone_candidate = getattr(transport, "clone_candidate", None)
    if callable(clone_candidate):
        remaining = []
        for row in pending:
            if stop():
                raise SliceError("slice_fetch_cancelled")
            if _clone_verified(row, root, clone_candidate):
                cloned += row["size_bytes"]
            else:
                remaining.append(row)
        pending = remaining
    needed = sum(row["size_bytes"] for row in pending)
    if needed and shutil.disk_usage(root).free < needed:
        raise SliceError("slice_disk_unavailable")
    for row in pending:
        work = _private_dir(root / _WORK / _digest_value(row).split(":", 1)[1])
        partial, journal_path = work / "partial", work / "journal.json"
        completed: set[int] = set()
        if journal_path.is_file() and partial.is_file() and partial.stat().st_size == row["size_bytes"]:
            try:
                completed = set(json.loads(journal_path.read_text("utf-8"))["completed"])
            except (OSError, ValueError, KeyError, TypeError):
                completed = set()
        else:
            with partial.open("wb") as handle:
                handle.truncate(row["size_bytes"])
            os.chmod(partial, 0o600)
        fd = os.open(partial, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        try:
            for index in sorted(completed):
                segment = row["segments"][index] if 0 <= index < len(row["segments"]) else None
                if segment is None or _sha(os.pread(fd, segment["length"], segment["offset"])) != segment["sha256"]:
                    completed.discard(index)
                else:
                    reused += segment["length"]
            for index, segment in enumerate(row["segments"]):
                if index in completed:
                    continue
                if stop():
                    raise SliceError("slice_fetch_cancelled")
                if "inline_base64" in segment:
                    data = base64.b64decode(segment["inline_base64"])
                    if _sha(data) != segment["sha256"]:
                        raise SliceError("slice_segment_digest_mismatch")
                else:
                    data = _fetch_segment(transport, plan, segment)
                    requests += data[1]
                    downloaded += data[2]
                    data = data[0]
                if os.pwrite(fd, data, segment["offset"]) != len(data):
                    raise SliceError("slice_write_failed")
                os.fsync(fd)
                completed.add(index)
                _atomic_json(journal_path, {"file": row["path"], "completed": sorted(completed)})
        finally:
            os.close(fd)
        if _file_sha(partial) != row["content_digest"]:
            shutil.rmtree(work, ignore_errors=True)
            raise SliceError("slice_file_digest_mismatch")
        final = root / row["path"]
        _private_dir(final.parent)
        os.replace(partial, final)
        shutil.rmtree(work, ignore_errors=True)
    work_root = root / _WORK
    if work_root.is_dir() and not any(work_root.iterdir()):
        work_root.rmdir()
    return {"protocol": REPORT_PROTOCOL, "state": "slice_verified_not_loaded",
            "node_id": plan["node_id"], "node_plan_digest": plan["node_plan_digest"],
            "plan_digest": plan["plan_digest"], "destination_root": root,
            "files": [{k: row[k] for k in ("path", "size_bytes", "content_digest")}
                      for row in plan["files"]],
            "downloaded_bytes": downloaded, "reused_bytes": reused, "cloned_bytes": cloned,
            "range_requests": requests, "route_ready": False}


def bind_slice_prepositions(controller: Mapping[str, Any], *, node_plans: Mapping[str, Any],
                            target_slice_roots: Mapping[str, str]) -> dict[str, Any]:
    """Like ``managed_transfers.bind_hf_prepositions`` for member slice roots.

    Removes each node's stage-shard weights from its control archive and
    prepositions them from the member's verified slice root. The node plan's
    files must equal that node's compiled transfer rows exactly; targets still
    re-hash every prepositioned file in ``TargetStaging``.
    """
    try:
        result = json.loads(json.dumps(controller, allow_nan=False))
        peers = result["peers"]
        nodes = [peer["node_id"] for peer in peers]
        if (not nodes or len(nodes) != len(set(nodes))
                or any(peer.get("process_transport") != "managed_https" for peer in peers)
                or set(target_slice_roots) != set(nodes) or set(node_plans) != set(nodes)
                or "prepositioned_artifacts" in result):
            raise ValueError
        base = {row["path"]: row for row in result["transfer_manifest"]["files"]}
        document = result["node_transfer_manifests"]
        if set(document["manifests"]) != set(nodes):
            raise ValueError
        members = {}
        for node in nodes:
            node_plan = node_plans[node]
            _validated_node_plan(node_plan, node_plan.get("node_plan_digest"))
            if node_plan["node_id"] != node:
                raise ValueError
            root = PurePosixPath(target_slice_roots[node])
            if not root.is_absolute() or ".." in root.parts or len(str(root)) > 1800:
                raise ValueError
            planned = {row["path"]: (row["size_bytes"], row["content_digest"])
                       for row in node_plan["files"]}
            retained, local = [], []
            for row in document["manifests"][node]["files"]:
                if row != base[row["path"]]:
                    raise ValueError
                if row["path"].startswith("deployment/") and row["path"].endswith(".safetensors"):
                    if planned.get(row["path"]) != (row["size_bytes"], row["content_digest"]):
                        raise ValueError
                    local.append({"destination_path": row["path"],
                                  "source_path": str(root / row["path"]),
                                  "size_bytes": row["size_bytes"],
                                  "content_digest": row["content_digest"]})
                else:
                    retained.append(row)
            if {row["destination_path"] for row in local} != set(planned):
                raise ValueError
            document["manifests"][node]["files"] = retained
            members[node] = sorted(local, key=lambda row: row["destination_path"])
        result["prepositioned_artifacts"] = {
            "protocol": "mycelium.controller_prepositioned_artifacts.v1", "members": members}
        return result
    except SliceError:
        raise SliceError("slice_preposition_binding_invalid") from None
    except (ValueError, TypeError, KeyError, AttributeError):
        raise SliceError("slice_preposition_binding_invalid") from None


__all__ = ["MAX_INLINE_BYTES_PER_NODE", "MAX_SEGMENT_BYTES", "NODE_PLAN_PROTOCOL",
           "PLAN_PROTOCOL", "REPORT_PROTOCOL", "SliceError", "bind_slice_prepositions",
           "compile_slice_plan", "fetch_node_slice", "node_slice_plan", "node_weight_files"]
