# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded acquisition of the exact A13 macOS selected model.

This module downloads and verifies a pinned public Hugging Face repository into
one private local snapshot, then invokes the existing source-only native manifest
compiler. It never quantizes, loads tensors, starts a worker, or marks a route
ready. Loopback HTTP and custom manifests/transports are test-only constructor
seams; the production composition is Web-PKI HTTPS and the fixed public commit.
"""
from __future__ import annotations

from mycelium_http_identity import with_user_agent
from collections.abc import Callable, Mapping
import copy
from dataclasses import asdict
import errno
import hashlib
import http.client
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import ssl
import stat
import threading
import time
from typing import Any, NoReturn
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit
import uuid

try:
    import fcntl
except ImportError:  # win32: LockFileEx through mycelium_host_platform
    fcntl = None  # type: ignore[assignment]

import mycelium_host_platform
from mycelium_platform_neutral.offline import (
    OFFLINE_ERROR_CODE,
    OfflineModelSourceError,
    offline_enabled,
    offline_message,
)

from .serving import (
    CANDIDATE_MODEL,
    ModelContract,
    PreparedSource,
    ServingPreparationError,
    prepare_local_source,
)


MANIFEST_PROTOCOL = "mycelium.a13_selected_model_repository_manifest.v1"
_STATUS_FIELDS = frozenset(
    {"state", "downloaded_bytes", "total_bytes", "error_code", "model"}
)
_CONSENT_FIELDS = frozenset(
    {"download_acknowledged", "max_download_bytes", "storage_limit_bytes"}
)
_ACTIVE_STATES = frozenset(
    {
        "starting",
        "resolving_metadata",
        "checking_capacity",
        "downloading",
        "verifying",
        "compiling",
    }
)
_STATUS_STATES = _ACTIVE_STATES | frozenset(
    {"idle", "prepared", "failed", "cancelled", "closed"}
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}/[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_CONTENT_RANGE_RE = re.compile(r"^bytes ([0-9]{1,19})-([0-9]{1,19})/([0-9]{1,19}|\*)$")
_MAX_RANGE_BYTES = 64 * 1024 * 1024
_MAX_METADATA_BYTES = 4 * 1024 * 1024
_MAX_REPOSITORY_FILES = 256
_MAX_PATH_BYTES = 1024
_MAX_PATH_PARTS = 16
_MAX_FILE_BYTES = (1 << 63) - 1
_STREAM_CHUNK_BYTES = 64 * 1024
_PARTIAL_MAX_AGE_SECONDS = 7 * 24 * 60 * 60
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_SOURCE_RETRIES = 2
_HF_ORIGIN = "https://huggingface.co"


class AcquisitionError(RuntimeError):
    """Stable acquisition failure whose message is a non-sensitive code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class OfflineAcquisitionError(AcquisitionError, OfflineModelSourceError):
    """Hugging Face was refused because offline mode is on (``.code`` is stable)."""

    def __init__(self, what: str = "the model files") -> None:
        RuntimeError.__init__(self, offline_message(what))
        self.code = OFFLINE_ERROR_CODE


def _raise(code: str, cause: BaseException | None = None) -> NoReturn:
    error = AcquisitionError(code)
    if cause is None:
        raise error
    raise error from cause


def _canonical(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


# --------------------------------------------------------------- platform
#
# POSIX keeps the original checks: owned by this euid, no group/other mode bits.
# win32 (owner parity ruling): every directory this module creates or trusts is an
# owner-only *protected* DACL directory checked through mycelium_host_platform
# (``_secure_directory``/``_private_root``); a file is trusted through the
# directory that contains it, so here it only has to be a single-link, non-reparse
# regular file.  Small state (status.json) is written with an explicit owner-only
# file DACL and read back through the owner-only check.

_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _win32() -> bool:
    return mycelium_host_platform.is_win32()


def _is_link(metadata: os.stat_result) -> bool:
    """A symlink, or on Windows any reparse point (junction, mount point)."""

    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & _REPARSE_POINT
    )


def _owned(metadata: os.stat_result, *, mask: int) -> bool:
    if _win32():
        return not _is_link(metadata)
    return metadata.st_uid == os.geteuid() and not stat.S_IMODE(metadata.st_mode) & mask


def _fsync_directory(path: Path) -> None:
    if _win32():
        return  # no directory handle fsync on Windows; MoveFileEx metadata is journaled
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path: Path, value: object) -> None:
    if _win32():
        try:
            with mycelium_host_platform.private_directory(path.parent, create=False) as lease:
                lease.atomic_replace(path.name, _canonical(value))
        except mycelium_host_platform.HostPlatformError as exc:
            _raise("artifact_storage_failure", exc)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(_canonical(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    except OSError as exc:
        _raise("artifact_storage_failure", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _private_root(value: str | Path) -> Path:
    try:
        candidate = Path(value).expanduser()
    except TypeError as exc:
        _raise("acquisition_state_root_unsafe", exc)
    if not candidate.is_absolute() or candidate.is_symlink():
        _raise("acquisition_state_root_unsafe")
    if _win32():
        try:
            with mycelium_host_platform.private_directory(candidate, create=True):
                pass
            resolved = candidate.resolve(strict=True)
        except (mycelium_host_platform.HostPlatformError, OSError) as exc:
            _raise("acquisition_state_root_unsafe", exc)
        if resolved != candidate:
            _raise("acquisition_state_root_unsafe")
        return resolved
    try:
        candidate.mkdir(parents=True, mode=0o700, exist_ok=True)
        resolved = candidate.resolve(strict=True)
        metadata = candidate.lstat()
    except OSError as exc:
        _raise("acquisition_state_root_unsafe", exc)
    if (
        resolved != candidate
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
    ):
        _raise("acquisition_state_root_unsafe")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        _raise("acquisition_state_root_unsafe")
    return resolved


def _secure_directory(path: Path, *, create: bool = True) -> Path:
    if _win32():
        try:
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                _raise("artifact_storage_unsafe")
            with mycelium_host_platform.private_directory(path, create=create):
                pass
            resolved = path.resolve(strict=True)
        except AcquisitionError:
            raise
        except (mycelium_host_platform.HostPlatformError, OSError) as exc:
            _raise("artifact_storage_unsafe", exc)
        if resolved != path:
            _raise("artifact_storage_unsafe")
        return path
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        _raise("artifact_storage_unsafe", exc)
    if (
        resolved != path
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        _raise("artifact_storage_unsafe")
    return path


def _safe_relative_path(value: object) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\x00" in value:
        _raise("model_manifest_invalid")
    try:
        encoded_length = len(value.encode("utf-8"))
    except UnicodeError as exc:
        _raise("model_manifest_invalid", exc)
    relative = PurePosixPath(value)
    if (
        encoded_length > _MAX_PATH_BYTES
        or relative.is_absolute()
        or relative.as_posix() != value
        or len(relative.parts) > _MAX_PATH_PARTS
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        _raise("model_manifest_invalid")
    return relative


def _model_identity(model: ModelContract = CANDIDATE_MODEL) -> dict[str, Any]:
    return asdict(model)


def _validate_model(model: object) -> ModelContract:
    from .serving import REGISTERED_MODELS

    if not isinstance(model, ModelContract) or model not in REGISTERED_MODELS:
        _raise("selected_model_identity_invalid")
    return model


def _validate_consent(consent: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(consent, Mapping) or set(consent) != _CONSENT_FIELDS:
        _raise("acquisition_consent_invalid")
    maximum = consent.get("max_download_bytes")
    storage = consent.get("storage_limit_bytes")
    if (
        consent.get("download_acknowledged") is not True
        or type(maximum) is not int
        or maximum <= 0
        or type(storage) is not int
        or storage <= 0
    ):
        _raise("acquisition_consent_invalid")
    return {
        "download_acknowledged": True,
        "max_download_bytes": maximum,
        "storage_limit_bytes": storage,
    }


def _validate_status(value: object, model: ModelContract = CANDIDATE_MODEL) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _STATUS_FIELDS:
        _raise("acquisition_state_corrupt")
    state = value.get("state")
    downloaded = value.get("downloaded_bytes")
    total = value.get("total_bytes")
    error = value.get("error_code")
    if (
        state not in _STATUS_STATES
        or type(downloaded) is not int
        or downloaded < 0
        or type(total) is not int
        or total < 0
        or downloaded > total
        or (error is not None and (not isinstance(error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", error)))
        or value.get("model") != _model_identity(model)
    ):
        _raise("acquisition_state_corrupt")
    return copy.deepcopy(value)


def _validate_origin(origin: str, *, allow_test_http: bool) -> str:
    try:
        parsed = urlsplit(origin)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        _raise("transport_configuration_invalid", exc)
    if (
        not isinstance(origin, str)
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or port is None and parsed.scheme == "http"
    ):
        _raise("transport_configuration_invalid")
    if parsed.scheme == "https":
        return origin.rstrip("/")
    if (
        parsed.scheme == "http"
        and allow_test_http
        and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
    ):
        return origin.rstrip("/")
    _raise("https_required")


class _SchemeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, *, allow_test_http: bool) -> None:
        self._allow_test_http = allow_test_http
        super().__init__()

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        parsed = urlsplit(newurl)
        if parsed.scheme == "https":
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        if (
            self._allow_test_http
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            return super().redirect_request(req, fp, code, msg, headers, newurl)
        _raise("https_redirect_required")


class _HTTPRepositoryTransport:
    """Web-PKI HTTPS transport for one pinned public Hugging Face repository.

    ``allow_test_http`` exists only for loopback fixture tests. Production callers
    receive the system default trust store and cannot select plaintext transport.
    """

    def __init__(
        self,
        *,
        api_origin: str = _HF_ORIGIN,
        download_origin: str = _HF_ORIGIN,
        ssl_context: ssl.SSLContext | None = None,
        allow_test_http: bool = False,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if type(timeout_seconds) not in {int, float} or not 0 < float(timeout_seconds) <= 60:
            _raise("transport_configuration_invalid")
        self.api_origin = _validate_origin(api_origin, allow_test_http=allow_test_http)
        self.download_origin = _validate_origin(
            download_origin, allow_test_http=allow_test_http
        )
        self.allow_test_http = allow_test_http
        self.timeout_seconds = float(timeout_seconds)
        context = ssl.create_default_context() if ssl_context is None else ssl_context
        if not isinstance(context, ssl.SSLContext) or context.verify_mode == ssl.CERT_NONE:
            _raise("transport_configuration_invalid")
        self._opener = urllib.request.build_opener(
            _SchemeRedirectHandler(allow_test_http=allow_test_http),
            urllib.request.HTTPSHandler(context=context),
        )

    def _open(self, request: urllib.request.Request) -> Any:
        # Lowest boundary: offline mode never reaches the public Hugging Face
        # origin; a private mirror configured through the origins is unaffected.
        if request.full_url.startswith(_HF_ORIGIN + "/") and offline_enabled():
            raise OfflineAcquisitionError("the model files")
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            try:
                if exc.code in {401, 403}:
                    _raise("source_expired", exc)
                if exc.code == 404:
                    _raise("source_unavailable", exc)
                if exc.code == 416:
                    _raise("range_unsatisfiable", exc)
                _raise("network_error", exc)
            finally:
                exc.close()
        except (urllib.error.URLError, TimeoutError, OSError, ssl.SSLError) as exc:
            _raise("network_error", exc)
        final = urlsplit(response.geturl())
        if final.scheme != "https" and not (
            self.allow_test_http
            and final.scheme == "http"
            and final.hostname in {"127.0.0.1", "::1", "localhost"}
        ):
            response.close()
            _raise("https_redirect_required")
        return response

    def resolve(self, model: ModelContract) -> dict[str, Any]:
        _validate_model(model)
        path = (
            "/api/models/"
            + quote(model.model_id, safe="/")
            + "/revision/"
            + quote(model.resolved_commit, safe="")
            + "?blobs=true"
        )
        request = urllib.request.Request(
            self.api_origin + path,
            method="GET",
            headers=with_user_agent({
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            }),
        )
        with self._open(request) as response:
            content_encoding = response.headers.get("Content-Encoding")
            declared = response.headers.get("Content-Length")
            if content_encoding not in {None, "identity"}:
                _raise("model_metadata_invalid")
            if declared is not None:
                try:
                    length = int(declared)
                except ValueError as exc:
                    _raise("model_metadata_invalid", exc)
                if length < 0 or length > _MAX_METADATA_BYTES:
                    _raise("model_metadata_invalid")
            try:
                body = response.read(_MAX_METADATA_BYTES + 1)
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                _raise("network_error", exc)
        if len(body) > _MAX_METADATA_BYTES:
            _raise("model_metadata_invalid")
        try:
            document = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            _raise("model_metadata_invalid", exc)
        if (
            not isinstance(document, dict)
            or document.get("private") is not False
            or document.get("sha") != model.resolved_commit
            or document.get("id") != model.model_id
            or document.get("modelId") != model.model_id
            or not isinstance(document.get("siblings"), list)
        ):
            _raise("model_metadata_identity_mismatch")
        files: list[dict[str, Any]] = []
        for sibling in document["siblings"]:
            if not isinstance(sibling, Mapping):
                _raise("model_metadata_invalid")
            relative = _safe_relative_path(sibling.get("rfilename"))
            size = sibling.get("size")
            if type(size) is not int or not 0 < size <= _MAX_FILE_BYTES:
                _raise("model_metadata_invalid")
            lfs = sibling.get("lfs")
            if lfs is not None:
                if (
                    not isinstance(lfs, Mapping)
                    or lfs.get("size") != size
                    or not isinstance(lfs.get("sha256"), str)
                    or _SHA256_RE.fullmatch(lfs["sha256"]) is None
                ):
                    _raise("model_metadata_invalid")
                digest = {"algorithm": "sha256", "value": lfs["sha256"]}
            else:
                blob_id = sibling.get("blobId")
                if not isinstance(blob_id, str) or _GIT_SHA1_RE.fullmatch(blob_id) is None:
                    _raise("model_metadata_invalid")
                digest = {"algorithm": "git-sha1", "value": blob_id}
            files.append(
                {"path": relative.as_posix(), "size_bytes": size, "digest": digest}
            )
        manifest = {
            "protocol": MANIFEST_PROTOCOL,
            "model_id": model.model_id,
            "resolved_commit": model.resolved_commit,
            "files": files,
            "total_bytes": sum(item["size_bytes"] for item in files),
            "expires_at_unix_ms": None,
        }
        return _validate_manifest(manifest, model=model)

    def open_file(
        self,
        model: ModelContract,
        record: Mapping[str, Any],
        *,
        offset: int,
    ) -> Any:
        path = (
            "/"
            + quote(model.model_id, safe="/")
            + "/resolve/"
            + quote(model.resolved_commit, safe="")
            + "/"
            + quote(record["path"], safe="/")
            + "?download=true"
        )
        headers = {
            "Accept": "application/octet-stream",
            "Accept-Encoding": "identity",
        }
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(
            self.download_origin + path, method="GET", headers=with_user_agent(headers)
        )
        return self._open(request)

    def read_range(
        self,
        model_id: str,
        resolved_commit: str,
        path: str,
        offset: int,
        length: int,
    ) -> bytes:
        """Exactly ``length`` bytes at ``offset`` of one pinned-revision file.

        Slice fetch (D-1 as relaxed 2026-10-13): a member fetches only the
        byte ranges of its assigned tensors. The caller digest-verifies every
        range against the owner-compiled slice plan; this method only refuses
        anything that is not one exact identity-encoded 206 partial response.
        """
        if (
            not isinstance(model_id, str)
            or _MODEL_ID_RE.fullmatch(model_id) is None
            or not isinstance(resolved_commit, str)
            or _COMMIT_RE.fullmatch(resolved_commit) is None
            or type(offset) is not int
            or type(length) is not int
            or offset < 0
            or not 0 < length <= _MAX_RANGE_BYTES
        ):
            _raise("range_request_invalid")
        relative = _safe_relative_path(path)
        url = (
            self.download_origin
            + "/"
            + quote(model_id, safe="/")
            + "/resolve/"
            + quote(resolved_commit, safe="")
            + "/"
            + quote(relative.as_posix(), safe="/")
            + "?download=true"
        )
        end = offset + length - 1
        request = urllib.request.Request(
            url,
            method="GET",
            headers=with_user_agent({
                "Accept": "application/octet-stream",
                "Accept-Encoding": "identity",
                "Range": f"bytes={offset}-{end}",
            }),
        )
        with self._open(request) as response:
            content_range = response.headers.get("Content-Range") or ""
            matched = _CONTENT_RANGE_RE.fullmatch(content_range.strip())
            declared = response.headers.get("Content-Length")
            if (
                getattr(response, "status", None) != 206
                or response.headers.get("Content-Encoding") not in {None, "identity"}
                or matched is None
                or int(matched.group(1)) != offset
                or int(matched.group(2)) != end
                or (matched.group(3) != "*" and int(matched.group(3)) <= end)
                or (declared is not None and declared.strip() != str(length))
            ):
                _raise("range_response_invalid")
            try:
                body = response.read(length + 1)
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                _raise("network_error", exc)
        if len(body) != length:
            _raise("range_response_invalid")
        return body


def _validate_manifest(value: object, *, model: ModelContract) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value)
        != {
            "protocol",
            "model_id",
            "resolved_commit",
            "files",
            "total_bytes",
            "expires_at_unix_ms",
        }
        or value.get("protocol") != MANIFEST_PROTOCOL
        or value.get("model_id") != model.model_id
        or value.get("resolved_commit") != model.resolved_commit
        or _COMMIT_RE.fullmatch(str(value.get("resolved_commit"))) is None
        or not isinstance(value.get("files"), list)
        or not 1 <= len(value["files"]) <= _MAX_REPOSITORY_FILES
    ):
        _raise("model_manifest_invalid")
    files: list[dict[str, Any]] = []
    seen: set[str] = set()
    total = 0
    for raw in value["files"]:
        if not isinstance(raw, Mapping) or set(raw) != {"path", "size_bytes", "digest"}:
            _raise("model_manifest_invalid")
        path = _safe_relative_path(raw.get("path")).as_posix()
        size = raw.get("size_bytes")
        digest = raw.get("digest")
        if (
            path in seen
            or type(size) is not int
            or not 0 < size <= _MAX_FILE_BYTES
            or not isinstance(digest, Mapping)
            or set(digest) != {"algorithm", "value"}
        ):
            _raise("model_manifest_invalid")
        algorithm = digest.get("algorithm")
        digest_value = digest.get("value")
        valid_digest = (
            algorithm == "sha256"
            and isinstance(digest_value, str)
            and _SHA256_RE.fullmatch(digest_value) is not None
        ) or (
            algorithm == "git-sha1"
            and isinstance(digest_value, str)
            and _GIT_SHA1_RE.fullmatch(digest_value) is not None
        )
        if not valid_digest:
            _raise("model_manifest_invalid")
        seen.add(path)
        total += size
        if total > _MAX_FILE_BYTES:
            _raise("model_manifest_invalid")
        files.append(
            {
                "path": path,
                "size_bytes": size,
                "digest": {"algorithm": algorithm, "value": digest_value},
            }
        )
    if (
        "config.json" not in seen
        or not any(path.endswith(".safetensors") for path in seen)
        or type(value.get("total_bytes")) is not int
        or value["total_bytes"] != total
    ):
        _raise("model_manifest_invalid")
    expiry = value.get("expires_at_unix_ms")
    if expiry is not None and (type(expiry) is not int or expiry <= 0):
        _raise("model_manifest_invalid")
    files.sort(key=lambda item: item["path"])
    return {
        "protocol": MANIFEST_PROTOCOL,
        "model_id": model.model_id,
        "resolved_commit": model.resolved_commit,
        "files": files,
        "total_bytes": total,
        "expires_at_unix_ms": expiry,
    }


def _digest_file(path: Path, record: Mapping[str, Any]) -> bool:
    try:
        if _win32() and _is_link(path.lstat()):
            return False  # no O_NOFOLLOW on Windows: refuse the reparse point first
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
        with os.fdopen(descriptor, "rb") as handle:
            metadata = os.fstat(handle.fileno())
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not _owned(metadata, mask=0)
                or metadata.st_size != record["size_bytes"]
            ):
                return False
            algorithm = record["digest"]["algorithm"]
            if algorithm == "sha256":
                digest: Any = hashlib.sha256()
            else:
                digest = hashlib.sha1(
                    b"blob " + str(metadata.st_size).encode("ascii") + b"\0"
                )
            while block := handle.read(_STREAM_CHUNK_BYTES):
                digest.update(block)
        return digest.hexdigest() == record["digest"]["value"]
    except OSError:
        return False


def _secure_file_size(path: Path, *, maximum: int) -> int:
    try:
        metadata = path.lstat()
    except OSError as exc:
        _raise("artifact_storage_unsafe", exc)
    if (
        _is_link(metadata)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _owned(metadata, mask=0o077)
        or not 0 <= metadata.st_size <= maximum
    ):
        _raise("artifact_storage_unsafe")
    return metadata.st_size


class SelectedModelAcquisition:
    """Acquire exactly one bounded selected-model snapshot in the background."""

    def __init__(
        self,
        state_root: str | Path,
        *,
        model: ModelContract = CANDIDATE_MODEL,
        _transport: Any | None = None,
        _manifest: Mapping[str, Any] | None = None,
        _disk_free_bytes: Callable[[Path], int] | None = None,
        _clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self.model = _validate_model(model)
        self.state_root = _private_root(state_root)
        self._transport = _HTTPRepositoryTransport() if _transport is None else _transport
        # Optional byte source (peer_model_source.MultiSourceTransport): only
        # where file BYTES come from.  The manifest (the pinned digests) still
        # comes from ``_transport``/the injected manifest, every byte is still
        # verified here, and the consent budget is unchanged.
        self._byte_source: Any | None = None
        self._injected_manifest = None if _manifest is None else copy.deepcopy(_manifest)
        self._disk_free = _disk_free_bytes or (lambda path: shutil.disk_usage(path).free)
        self._clock = _clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._guard = threading.RLock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._active = False
        self._closed = False
        self._prepared: PreparedSource | None = None
        self._verified_manifest: dict[str, Any] | None = None
        self._network_bytes = 0
        self._status_path = self.state_root / "status.json"
        self._lock_fd: int | None = None
        self._win32_lock: Any | None = None
        self._acquire_lock()
        try:
            self._status = self._recover_status()
        except BaseException:
            self._release_lock()
            raise

    def _acquire_lock(self) -> None:
        if _win32():
            try:
                self._win32_lock = mycelium_host_platform.hold_exclusive_lock(
                    self.state_root, "acquisition.lock", blocking=False
                )
            except mycelium_host_platform.HostPlatformError as exc:
                if exc.code == "host_lock_busy":
                    _raise("acquisition_in_progress", exc)
                _raise("acquisition_state_root_unsafe", exc)
            return
        descriptor: int | None = None
        try:
            descriptor = os.open(
                self.state_root / "acquisition.lock",
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
            ):
                _raise("acquisition_state_root_unsafe")
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            if descriptor is not None:
                os.close(descriptor)
            _raise("acquisition_in_progress", exc)
        except AcquisitionError:
            if descriptor is not None:
                os.close(descriptor)
            raise
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            _raise("acquisition_state_root_unsafe", exc)
        assert descriptor is not None
        self._lock_fd = descriptor

    def _recover_status(self) -> dict[str, Any]:
        if _win32():
            return self._recover_status_win32()
        if not self._status_path.exists():
            return {
                "state": "idle",
                "downloaded_bytes": 0,
                "total_bytes": 0,
                "error_code": None,
                "model": _model_identity(self.model),
            }
        try:
            metadata = self._status_path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) & 0o077
                or metadata.st_size > 16 * 1024
            ):
                _raise("acquisition_state_corrupt")
            value = json.loads(self._status_path.read_text("utf-8"))
            status = _validate_status(value, self.model)
        except AcquisitionError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            _raise("acquisition_state_corrupt", exc)
        return self._settle_recovered(status)

    def _recover_status_win32(self) -> dict[str, Any]:
        try:
            with mycelium_host_platform.private_directory(self.state_root, create=False) as lease:
                raw = lease.read_file(self._status_path.name, max_bytes=16 * 1024)
        except mycelium_host_platform.HostPlatformError as exc:
            if exc.code == "host_file_missing":
                return {
                    "state": "idle",
                    "downloaded_bytes": 0,
                    "total_bytes": 0,
                    "error_code": None,
                    "model": _model_identity(self.model),
                }
            _raise("acquisition_state_corrupt", exc)
        try:
            status = _validate_status(json.loads(raw.decode("utf-8")), self.model)
        except AcquisitionError:
            raise
        except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
            _raise("acquisition_state_corrupt", exc)
        return self._settle_recovered(status)

    def _settle_recovered(self, status: dict[str, Any]) -> dict[str, Any]:
        if status["state"] in _ACTIVE_STATES:
            status["state"] = "failed"
            status["error_code"] = "interrupted_transfer"
            _atomic_json(self._status_path, status)
        elif status["state"] == "prepared":
            status["state"] = "failed"
            status["error_code"] = "prepared_source_revalidation_required"
            _atomic_json(self._status_path, status)
        return status

    def _set_status(self, **changes: Any) -> dict[str, Any]:
        with self._guard:
            status = {**self._status, **changes, "model": _model_identity(self.model)}
            status = _validate_status(status, self.model)
            self._status = status
            _atomic_json(self._status_path, status)
            return copy.deepcopy(status)

    def status(self) -> dict[str, Any]:
        with self._guard:
            return copy.deepcopy(self._status)

    def set_byte_source(self, source: Any | None) -> None:
        """Fetch file bytes from ``source`` (``None``: the pinned origin) next time.

        The source changes, never the trust: the pinned manifest still comes
        from the origin transport, and every file is digest-verified as before.
        """

        if source is not None and not callable(getattr(source, "open_file", None)):
            _raise("transport_configuration_invalid")
        with self._guard:
            if self._active:
                _raise("acquisition_in_progress")
            self._byte_source = source

    def verified_snapshot(self) -> tuple[Path, dict[str, Any]] | None:
        """The prepared snapshot root and its verified manifest, without re-hashing.

        For serving byte ranges to members (peer_model_source): members verify
        every byte against their own pinned digests, so this is a cheap view.
        """

        with self._guard:
            if (
                self._closed
                or self._prepared is None
                or self._verified_manifest is None
                or self._status["state"] != "prepared"
            ):
                return None
            return Path(self._prepared.source_root), copy.deepcopy(self._verified_manifest)

    def reverify_closed_snapshot(self, *, storage_limit_bytes: int) -> dict[str, Any]:
        """Re-hash a complete snapshot left on disk by an earlier run of the app, then prepare it.

        After a relaunch the recovered state is ``closed`` (or ``failed``/``prepared_source_revalidation_required``
        after a crash) although the verified bytes are still on disk.  This runs the ordinary acquisition path, so
        every file is hashed again exactly as a fresh download would; the grant allows **one** network byte, so
        nothing can be downloaded (a missing or partial snapshot is refused instead).  Refuses with
        ``source_not_on_disk`` unless a complete snapshot directory exists and no staged download does.
        """

        with self._guard:
            if self._status["state"] not in {"closed", "idle", "failed", "cancelled"}:
                _raise("acquisition_in_progress" if self._active else "source_already_prepared")
            _stage, snapshot = self._paths_if_present()
            if snapshot is None:
                _raise("source_not_on_disk")
        return self.start(
            {
                "download_acknowledged": True,
                "max_download_bytes": 1,
                "storage_limit_bytes": int(storage_limit_bytes),
            }
        )

    def _paths_if_present(self) -> tuple[Path | None, Path | None]:
        """The staged and final snapshot directories if they exist (nothing is created)."""

        model_root = self.state_root / ("models--" + self.model.model_id.replace("/", "--"))
        snapshots = model_root / "snapshots"
        stage = snapshots / ("." + self.model.resolved_commit + ".acquiring")
        snapshot = snapshots / self.model.resolved_commit
        try:
            staged = stage.exists() or stage.is_symlink()
            final = snapshot.is_dir() and not snapshot.is_symlink()
        except OSError:
            return None, None
        return (stage if staged else None), (snapshot if final and not staged else None)

    def _begin(self, consent: Mapping[str, Any]) -> dict[str, Any]:
        checked = _validate_consent(consent)
        with self._guard:
            if self._closed:
                _raise("acquisition_closed")
            if self._active:
                _raise("acquisition_in_progress")
            if self._prepared is not None:
                _raise("source_already_prepared")
            self._active = True
            self._cancel.clear()
            restart = getattr(self._byte_source, "reset_cancel", None)
            if restart is not None:
                restart()
            self._network_bytes = 0
            self._set_status(
                state="starting", downloaded_bytes=0, total_bytes=0, error_code=None
            )
        return checked

    def start(self, consent: Mapping[str, Any]) -> dict[str, Any]:
        """Start one bounded background acquisition after exact explicit consent."""

        checked = self._begin(consent)
        thread = threading.Thread(
            target=self._worker,
            args=(checked,),
            name="mycelium-a13-acquisition",
            daemon=True,
        )
        with self._guard:
            self._thread = thread
        try:
            thread.start()
        except RuntimeError as exc:
            with self._guard:
                self._active = False
            self._set_status(state="failed", error_code="acquisition_start_failed")
            _raise("acquisition_start_failed", exc)
        return self.status()

    def _run_blocking(self, consent: Mapping[str, Any]) -> dict[str, Any]:
        """Deterministic internal seam using the same state machine as ``start``."""

        checked = self._begin(consent)
        self._worker(checked)
        return self.status()

    def _worker(self, consent: Mapping[str, Any]) -> None:
        try:
            self._execute(consent)
        except AcquisitionError as exc:
            state = "cancelled" if exc.code == "acquisition_cancelled" else "failed"
            self._set_status(state=state, error_code=None if state == "cancelled" else exc.code)
        except BaseException:
            self._set_status(state="failed", error_code="acquisition_failed")
        finally:
            with self._guard:
                self._active = False

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            _raise("acquisition_cancelled")

    def _resolve_manifest(self) -> dict[str, Any]:
        self._set_status(state="resolving_metadata")
        self._check_cancelled()
        if self._injected_manifest is not None:
            manifest = _validate_manifest(self._injected_manifest, model=self.model)
        else:
            # Offline, the pinned file list comes from the owner (through the
            # byte source); it is validated and digest-enforced exactly as a
            # Hugging Face manifest would be.
            source = self._transport
            if offline_enabled() and callable(getattr(self._byte_source, "resolve", None)):
                source = self._byte_source
            try:
                manifest = _validate_manifest(source.resolve(self.model), model=self.model)
            except AcquisitionError:
                raise
            except BaseException as exc:
                _raise("network_error", exc)
        expiry = manifest["expires_at_unix_ms"]
        now = self._clock()
        if type(now) is not int or now < 1:
            _raise("acquisition_clock_invalid")
        if expiry is not None and now >= expiry:
            _raise("model_manifest_expired")
        return manifest

    def _paths(self) -> tuple[Path, Path]:
        model_root = _secure_directory(
            self.state_root / ("models--" + self.model.model_id.replace("/", "--"))
        )
        snapshots = _secure_directory(model_root / "snapshots")
        stage = snapshots / ("." + self.model.resolved_commit + ".acquiring")
        snapshot = snapshots / self.model.resolved_commit
        return stage, snapshot

    def _execute(self, consent: Mapping[str, Any]) -> None:
        manifest = self._resolve_manifest()
        total = manifest["total_bytes"]
        self._set_status(state="checking_capacity", total_bytes=total)
        # Storage is a property of the stored snapshot, not of this attempt.
        if total > consent["storage_limit_bytes"]:
            _raise("storage_limit_exceeded")
        self._check_cancelled()
        stage, snapshot = self._paths()
        if snapshot.exists():
            if stage.exists() or stage.is_symlink():
                _raise("artifact_storage_unsafe")
            self._verify_snapshot(snapshot, manifest)
            downloaded = total
        else:
            stage = _secure_directory(stage)
            downloaded = self._scan_stage(stage, manifest)
            self._set_status(downloaded_bytes=downloaded)
            remaining = total - downloaded
            # D-1 (2026-09-16): the download budget bounds what this attempt
            # would fetch over the network, not the repository total.  A
            # retained/LAN-seeded snapshot therefore completes with a small
            # allowance, while a seed missing model bytes refuses here — before
            # any byte is requested — instead of silently downloading them
            # (ERRATA-1).  ``downloaded`` counts only digest-verified staged
            # files, and ``_write_chunk`` still bounds cumulative network bytes
            # by the same budget during transfer.
            if remaining > consent["max_download_bytes"]:
                _raise("download_limit_exceeded")
            try:
                free = self._disk_free(self.state_root)
            except BaseException as exc:
                _raise("disk_availability_unavailable", exc)
            if type(free) is not int or free < remaining:
                _raise("insufficient_disk")
            self._set_status(state="downloading")
            for record in manifest["files"]:
                self._check_cancelled()
                final, partial = self._stage_file_paths(stage, record["path"])
                if final.exists():
                    continue
                for attempt in range(_MAX_SOURCE_RETRIES + 1):
                    try:
                        self._download_file(
                            record,
                            partial=partial,
                            final=final,
                            total=total,
                            consent=consent,
                        )
                        break
                    except AcquisitionError as exc:
                        # Bytes from a peer failed the pinned digest: the peer is
                        # quarantined and the file is fetched again from what
                        # remains (the partial is already gone).  Never retried
                        # when only the pinned origin served it.
                        reject = getattr(self._byte_source, "reject", None)
                        if (
                            exc.code != "artifact_digest_mismatch"
                            or reject is None
                            or attempt == _MAX_SOURCE_RETRIES
                            or reject(record["path"]) is not True
                        ):
                            raise
            self._set_status(state="verifying", downloaded_bytes=total)
            self._verify_snapshot(stage, manifest)
            try:
                os.replace(stage, snapshot)
                _fsync_directory(snapshot.parent)
            except OSError as exc:
                _raise("artifact_storage_failure", exc)
            self._verify_snapshot(snapshot, manifest)
            downloaded = total
        self._check_cancelled()
        self._set_status(state="compiling", downloaded_bytes=downloaded)
        try:
            prepared = prepare_local_source(snapshot, model=self.model)
        except ServingPreparationError as exc:
            _raise("native_manifest_compile_failed", exc)
        self._verify_snapshot(snapshot, manifest)
        with self._guard:
            self._prepared = prepared
            self._verified_manifest = copy.deepcopy(manifest)
        self._set_status(
            state="prepared",
            downloaded_bytes=total,
            total_bytes=total,
            error_code=None,
        )

    def _stage_file_paths(self, stage: Path, value: str) -> tuple[Path, Path]:
        relative = _safe_relative_path(value)
        parent = stage
        for part in relative.parts[:-1]:
            parent = _secure_directory(parent / part)
        final = parent / relative.name
        partial = parent / (relative.name + ".partial")
        return final, partial

    def _scan_stage(self, stage: Path, manifest: Mapping[str, Any]) -> int:
        expected_files = {record["path"] for record in manifest["files"]}
        expected_dirs: set[str] = set()
        for path in expected_files:
            parent = PurePosixPath(path).parent
            while parent.as_posix() != ".":
                expected_dirs.add(parent.as_posix())
                parent = parent.parent
        try:
            for root, directories, filenames in os.walk(stage, followlinks=False):
                root_path = Path(root)
                for directory in directories:
                    candidate = root_path / directory
                    metadata = candidate.lstat()
                    relative = candidate.relative_to(stage).as_posix()
                    if _is_link(metadata) or relative not in expected_dirs:
                        _raise("artifact_storage_unsafe")
                for filename in filenames:
                    candidate = root_path / filename
                    relative = candidate.relative_to(stage).as_posix()
                    plain = relative.removesuffix(".partial")
                    if plain not in expected_files or relative not in {plain, plain + ".partial"}:
                        _raise("artifact_storage_unsafe")
        except AcquisitionError:
            raise
        except OSError as exc:
            _raise("artifact_storage_unsafe", exc)
        downloaded = 0
        now_seconds = self._clock() / 1_000
        for record in manifest["files"]:
            final, partial = self._stage_file_paths(stage, record["path"])
            if final.exists() or final.is_symlink():
                _secure_file_size(final, maximum=record["size_bytes"])
                if not _digest_file(final, record):
                    _raise("artifact_digest_mismatch")
                if partial.exists() or partial.is_symlink():
                    _raise("artifact_storage_unsafe")
                downloaded += record["size_bytes"]
                continue
            if not (partial.exists() or partial.is_symlink()):
                continue
            size = _secure_file_size(partial, maximum=record["size_bytes"])
            try:
                age = now_seconds - partial.stat().st_mtime
            except OSError as exc:
                _raise("artifact_storage_unsafe", exc)
            if age > _PARTIAL_MAX_AGE_SECONDS:
                partial.unlink()
                _fsync_directory(partial.parent)
                continue
            if size == record["size_bytes"]:
                if not _digest_file(partial, record):
                    partial.unlink(missing_ok=True)
                    _fsync_directory(partial.parent)
                    _raise("artifact_digest_mismatch")
                os.replace(partial, final)
                _fsync_directory(final.parent)
                downloaded += size
            else:
                downloaded += size
        return downloaded

    def _download_file(
        self,
        record: Mapping[str, Any],
        *,
        partial: Path,
        final: Path,
        total: int,
        consent: Mapping[str, Any],
    ) -> None:
        offset = 0
        if partial.exists() or partial.is_symlink():
            offset = _secure_file_size(partial, maximum=record["size_bytes"])
        remaining = record["size_bytes"] - offset
        if remaining == 0:
            _raise("artifact_storage_unsafe")
        self._check_disk(remaining)
        source = self._transport if self._byte_source is None else self._byte_source
        try:
            response = source.open_file(self.model, record, offset=offset)
        except AcquisitionError:
            raise
        except BaseException as exc:
            _raise("network_error", exc)
        try:
            status = getattr(response, "status", None)
            if status not in {200, 206}:
                _raise("source_response_invalid")
            encoding = response.headers.get("Content-Encoding")
            if encoding not in {None, "identity"}:
                _raise("source_response_invalid")
            restart = offset > 0 and status == 200
            if status == 206:
                expected_range = (
                    f"bytes {offset}-{record['size_bytes'] - 1}/{record['size_bytes']}"
                )
                if response.headers.get("Content-Range") != expected_range:
                    _raise("source_response_invalid")
            elif offset == 0:
                restart = False
            if restart:
                expected = record["size_bytes"]
            else:
                expected = record["size_bytes"] - offset
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_length = int(declared)
                except ValueError as exc:
                    _raise("source_response_invalid", exc)
                if declared_length != expected:
                    _raise("source_response_invalid")
            descriptor = self._open_partial(partial)
            try:
                with os.fdopen(descriptor, "r+b") as handle:
                    descriptor = -1
                    if restart:
                        handle.truncate(0)
                        offset = 0
                        self._set_status(
                            downloaded_bytes=self._count_downloaded_from_disk(partial.parent)
                        )
                    else:
                        handle.seek(0, os.SEEK_END)
                        if handle.tell() != offset:
                            _raise("artifact_storage_unsafe")
                    written = 0
                    while written < expected:
                        self._check_cancelled()
                        request_size = min(_STREAM_CHUNK_BYTES, expected - written)
                        try:
                            chunk = response.read(request_size)
                        except http.client.IncompleteRead as exc:
                            chunk = exc.partial
                            if chunk:
                                if written + len(chunk) > expected:
                                    _raise("source_response_invalid", exc)
                                self._write_chunk(
                                    handle,
                                    chunk,
                                    total=total,
                                    consent=consent,
                                )
                                written += len(chunk)
                            _raise("source_disconnected", exc)
                        except (OSError, TimeoutError, http.client.HTTPException) as exc:
                            _raise("source_disconnected", exc)
                        if not chunk:
                            _raise("source_disconnected")
                        if written + len(chunk) > expected:
                            _raise("source_response_invalid")
                        self._write_chunk(
                            handle, chunk, total=total, consent=consent
                        )
                        written += len(chunk)
                    try:
                        extra = response.read(1)
                    except (OSError, TimeoutError, http.client.HTTPException) as exc:
                        _raise("source_disconnected", exc)
                    if extra:
                        _raise("source_response_invalid")
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
        finally:
            response.close()
        if not _digest_file(partial, record):
            try:
                partial.unlink(missing_ok=True)
                _fsync_directory(partial.parent)
            except OSError as exc:
                _raise("artifact_storage_failure", exc)
            self._set_status(downloaded_bytes=self._stage_bytes_for_status())
            _raise("artifact_digest_mismatch")
        try:
            os.replace(partial, final)
            _fsync_directory(final.parent)
        except OSError as exc:
            _raise("artifact_storage_failure", exc)

    def _open_partial(self, path: Path) -> int:
        try:
            if _win32() and (path.is_symlink() or (path.exists() and _is_link(path.lstat()))):
                _raise("artifact_storage_unsafe")
            descriptor = os.open(
                path,
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not _owned(metadata, mask=0o077)
            ):
                os.close(descriptor)
                _raise("artifact_storage_unsafe")
            return descriptor
        except AcquisitionError:
            raise
        except OSError as exc:
            _raise("artifact_storage_unsafe", exc)

    def _write_chunk(
        self,
        handle: Any,
        chunk: bytes,
        *,
        total: int,
        consent: Mapping[str, Any],
    ) -> None:
        if not isinstance(chunk, bytes) or not chunk:
            _raise("source_response_invalid")
        if self._network_bytes + len(chunk) > consent["max_download_bytes"]:
            _raise("download_limit_exceeded")
        self._check_disk(len(chunk))
        current = self.status()["downloaded_bytes"]
        if current + len(chunk) > total or current + len(chunk) > consent["storage_limit_bytes"]:
            _raise("storage_limit_exceeded")
        try:
            handle.write(chunk)
        except OSError as exc:
            if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
                _raise("insufficient_disk", exc)
            _raise("artifact_storage_failure", exc)
        self._network_bytes += len(chunk)
        self._set_status(downloaded_bytes=current + len(chunk))

    def _check_disk(self, required: int) -> None:
        try:
            free = self._disk_free(self.state_root)
        except BaseException as exc:
            _raise("disk_availability_unavailable", exc)
        if type(free) is not int or free < required:
            _raise("insufficient_disk")

    def _stage_bytes_for_status(self) -> int:
        status_total = self.status()["total_bytes"]
        stage, snapshot = self._paths()
        root = snapshot if snapshot.exists() else stage
        if not root.exists():
            return 0
        total = 0
        try:
            for path in root.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    total += path.stat().st_size
                    if total > status_total:
                        return status_total
        except OSError:
            return 0
        return total

    def _count_downloaded_from_disk(self, _near: Path) -> int:
        return self._stage_bytes_for_status()

    def _verify_snapshot(self, root: Path, manifest: Mapping[str, Any]) -> None:
        _secure_directory(root, create=False)
        expected = {record["path"]: record for record in manifest["files"]}
        observed: set[str] = set()
        try:
            for walk_root, directories, files in os.walk(root, followlinks=False):
                walk_path = Path(walk_root)
                for directory in directories:
                    metadata = (walk_path / directory).lstat()
                    if (
                        _is_link(metadata)
                        or not stat.S_ISDIR(metadata.st_mode)
                        or not _owned(metadata, mask=0o022)
                    ):
                        _raise("artifact_storage_unsafe")
                    if _win32():
                        _secure_directory(walk_path / directory, create=False)
                for filename in files:
                    path = walk_path / filename
                    relative = path.relative_to(root).as_posix()
                    if relative not in expected:
                        _raise("artifact_storage_unsafe")
                    observed.add(relative)
        except AcquisitionError:
            raise
        except OSError as exc:
            _raise("artifact_storage_unsafe", exc)
        if observed != set(expected):
            _raise("source_incomplete")
        for path, record in expected.items():
            candidate = root.joinpath(*PurePosixPath(path).parts)
            if not _digest_file(candidate, record):
                _raise("artifact_digest_mismatch")

    def prepared_source(self) -> PreparedSource:
        """Return native source-only preparation after complete verification/compile."""

        with self._guard:
            if self._closed:
                _raise("acquisition_closed")
            prepared = self._prepared
            manifest = self._verified_manifest
            if (
                prepared is None
                or manifest is None
                or self._status["state"] != "prepared"
            ):
                _raise("source_not_prepared")
        try:
            self._verify_snapshot(prepared.source_root, manifest)
        except AcquisitionError as exc:
            with self._guard:
                self._prepared = None
                self._verified_manifest = None
            self._set_status(state="failed", error_code=exc.code)
            raise
        return prepared

    def cancel(self) -> None:
        """Request bounded cancellation; safe partials remain resumable."""

        self._cancel.set()
        stop = getattr(self._byte_source, "cancel", None)
        if stop is not None:
            stop()  # a multi-source read stops between chunks, not after retries

    def close(self) -> None:
        """Cancel and join an active attempt, then release the process lock."""

        with self._guard:
            if self._closed:
                return
            self._cancel.set()
            thread = self._thread
        stop = getattr(self._byte_source, "cancel", None)
        if stop is not None:
            stop()
        if thread is not None and thread is not threading.current_thread():
            timeout = max(
                float(getattr(source, "timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
                for source in (self._transport, self._byte_source)
                if source is not None
            )
            thread.join(timeout=float(timeout) + 2.0)
            if thread.is_alive():
                _raise("acquisition_close_timeout")
        with self._guard:
            self._closed = True
            self._active = False
            try:
                self._set_status(state="closed", error_code=None)
            finally:
                self._release_lock()

    def _release_lock(self) -> None:
        held = getattr(self, "_win32_lock", None)
        if held is not None:
            self._win32_lock = None
            held.release()
        descriptor = self._lock_fd
        if descriptor is None:
            return
        self._lock_fd = None
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def _release_lock_for_test(self) -> None:
        """Simulate process death in a recovery test without touching other state."""

        self._release_lock()


__all__ = [
    "AcquisitionError",
    "MANIFEST_PROTOCOL",
    "SelectedModelAcquisition",
]
