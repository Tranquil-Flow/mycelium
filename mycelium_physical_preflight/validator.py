from __future__ import annotations

import ipaddress
import json
import math
import os
import posixpath
import re
import stat
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

from .generator import generate_execution_plan
from .schema import (
    ABORT_CONDITIONS,
    CLEANUP_FIELDS,
    COORDINATOR_FIELDS,
    DECODE_FIELDS,
    EVIDENCE_FIELDS,
    HOST_FIELDS,
    IDENTITY_FIELDS,
    NEGATIVE_TESTS,
    PER_STEP_EVIDENCE,
    PROTOCOL,
    ROLLBACK_FIELDS,
    ROOT_FIELDS,
    RUN_FIELDS,
    RUN_MATRIX_FIELDS,
)

_MAX_PLAN_BYTES = 1_048_576
_SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_COMMIT_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_DNS_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_SECRET_CLI_RE = re.compile(
    r"--(?:api[-_]?key|access[-_]?token|auth[-_]?token|token|password|secret|credential)"
    r"(?:=|\s)",
    re.IGNORECASE,
)
_INLINE_SECRET_RES = [
    re.compile(r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:]+:[^\s/@]+@"),
]
_CREDENTIAL_KEY_ALLOWLIST = frozenset(
    (
        "prohibit_credential_bytes_in_arguments_or_evidence",
        "require_single_token_decode",
        "remove_token_files",
        "token_file_path",
        "token_match",
    )
)
_CREDENTIAL_KEY_TERMS = (
    "apikey",
    "authorizationheader",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
)
_UNSAFE_SOURCE_NAME_RE = re.compile(
    r"(?:^|[._-])(?:api[-_]?key|cookie|credentials?|id[-_]?rsa|password|"
    r"private[-_]?key|secrets?|tokens?)(?:[._-]|$)",
    re.IGNORECASE,
)

class PreflightValidationError(ValueError):
    """Stable, redacted validation failure."""

    def __init__(self, code: str, pointer: str = "") -> None:
        self.code = code
        self.pointer = pointer or "/"
        super().__init__(f"{self.code}:{self.pointer}")


def _fail(code: str, pointer: str = "") -> NoReturn:
    raise PreflightValidationError(code, pointer)


def _json_body(value: Any) -> bytes:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        _fail("noncanonical_json")
    try:
        return rendered.encode("utf-8")
    except UnicodeEncodeError:
        _fail("invalid_unicode")


def canonical_json_bytes(value: Any) -> bytes:
    return _json_body(value) + b"\n"


def canonical_error_bytes(error: PreflightValidationError) -> bytes:
    return canonical_json_bytes(
        {"error": {"code": error.code, "pointer": error.pointer}, "ok": False}
    )


def _reject_constant(_: str) -> None:
    _fail("invalid_json")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            _fail("duplicate_json_key", "/")
        value[key] = item
    return value


def _parse_canonical_json(encoded: bytes) -> dict[str, Any]:
    if not isinstance(encoded, bytes) or not encoded or len(encoded) > _MAX_PLAN_BYTES:
        _fail("invalid_input_size")
    try:
        text = encoded.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _fail("invalid_utf8")
    try:
        value = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
        )
    except PreflightValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError, RecursionError):
        _fail("invalid_json")
    if type(value) is not dict:
        _fail("invalid_root")
    if encoded != _json_body(value):
        _fail("noncanonical_json")
    return value


def _pointer(parent: str, key: str | int) -> str:
    return f"{parent}/{key}" if parent else f"/{key}"


def _scan_for_credentials(value: Any, pointer: str = "") -> None:
    if type(value) is dict:
        for key, item in value.items():
            normalized = re.sub(r"-", "_", key.lower())
            if normalized not in _CREDENTIAL_KEY_ALLOWLIST:
                compact = re.sub(r"[^a-z0-9]", "", normalized)
                if any(term in compact for term in _CREDENTIAL_KEY_TERMS):
                    _fail("forbidden_credential_field", _pointer(pointer, key))
            _scan_for_credentials(item, _pointer(pointer, key))
    elif type(value) is list:
        for index, item in enumerate(value):
            _scan_for_credentials(item, _pointer(pointer, index))
    elif type(value) is str:
        if _SECRET_CLI_RE.search(value):
            _fail("secret_cli_argument", pointer)
        if any(pattern.search(value) for pattern in _INLINE_SECRET_RES):
            _fail("inline_credential", pointer)


def _object(value: Any, fields: set[str], pointer: str) -> dict[str, Any]:
    if type(value) is not dict:
        _fail("invalid_type", pointer)
    missing = fields - set(value)
    if missing:
        _fail("missing_field", _pointer(pointer, sorted(missing)[0]))
    unknown = set(value) - fields
    if unknown:
        _fail("unknown_field", _pointer(pointer, sorted(unknown)[0]))
    return value


def _string(value: Any, pointer: str, *, code: str = "invalid_string") -> str:
    if type(value) is not str or not value:
        _fail(code, pointer)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(code, pointer)
    return value


def _boolean(value: Any, pointer: str) -> bool:
    if type(value) is not bool:
        _fail("invalid_boolean", pointer)
    return value


def _positive_int(value: Any, pointer: str, code: str = "invalid_integer") -> int:
    if type(value) is not int or value <= 0:
        _fail(code, pointer)
    return value


def _positive_float(value: Any, pointer: str) -> float:
    if type(value) not in {int, float} or type(value) is bool:
        _fail("invalid_tolerance", pointer)
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0.0 or numeric > 1.0:
        _fail("invalid_tolerance", pointer)
    return numeric


def _exact_string(value: Any, expected: str, pointer: str, code: str) -> None:
    if type(value) is not str or value != expected:
        _fail(code, pointer)


def _exact_bool(value: Any, expected: bool, pointer: str, code: str) -> None:
    if type(value) is not bool or value is not expected:
        _fail(code, pointer)


def _exact_list(value: Any, expected: list[str], pointer: str, code: str) -> None:
    if type(value) is not list or value != expected:
        _fail(code, pointer)
    if any(type(item) is not str for item in value):
        _fail(code, pointer)


def _validate_schema(plan: dict[str, Any]) -> None:
    _object(plan, ROOT_FIELDS, "")
    hosts = plan["hosts"]
    if type(hosts) is not list:
        _fail("invalid_host_count", "/hosts")
    for index, host in enumerate(hosts):
        _object(host, HOST_FIELDS, f"/hosts/{index}")
    _object(plan["coordinator"], COORDINATOR_FIELDS, "/coordinator")
    _object(plan["identities"], IDENTITY_FIELDS, "/identities")
    matrix = _object(plan["run_matrix"], RUN_MATRIX_FIELDS, "/run_matrix")
    _object(matrix["cold"], RUN_FIELDS, "/run_matrix/cold")
    _object(matrix["warm"], RUN_FIELDS, "/run_matrix/warm")
    _object(plan["decode_parity"], DECODE_FIELDS, "/decode_parity")
    _object(plan["evidence"], EVIDENCE_FIELDS, "/evidence")
    _object(plan["cleanup"], CLEANUP_FIELDS, "/cleanup")
    _object(plan["rollback"], ROLLBACK_FIELDS, "/rollback")


def _validate_slug(value: Any, pointer: str, code: str) -> str:
    text = _string(value, pointer, code=code)
    if not _SLUG_RE.fullmatch(text):
        _fail(code, pointer)
    return text


def _validate_host_name(value: Any, pointer: str) -> str:
    text = _string(value, pointer, code="invalid_host_name")
    if not _HOST_RE.fullmatch(text) or ".." in text:
        _fail("invalid_host_name", pointer)
    return text


def _validate_user(value: Any, pointer: str) -> str:
    text = _string(value, pointer, code="invalid_ssh_user")
    if not _USER_RE.fullmatch(text) or text.lower() == "root":
        _fail("unsafe_ssh_user", pointer)
    return text


def _validate_digest(value: Any, pointer: str) -> str:
    text = _string(value, pointer, code="invalid_digest")
    if not _DIGEST_RE.fullmatch(text):
        _fail("invalid_digest", pointer)
    return text


def _validate_address(value: Any, pointer: str) -> str:
    text = _string(value, pointer, code="invalid_coordinator_address")
    if any(character in text for character in "@/[]"):
        _fail("invalid_coordinator_address", pointer)
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        if (
            not _DNS_RE.fullmatch(text)
            or text.lower() == "localhost"
            or text.lower().endswith(".localhost")
        ):
            _fail("invalid_coordinator_address", pointer)
    else:
        if (
            address.is_loopback
            or address.is_link_local
            or address.is_multicast
            or address.is_reserved
            or address.is_unspecified
        ):
            _fail("unsafe_coordinator_address", pointer)
    return text


def _path_is_within(child: Path | PurePosixPath, parent: Path | PurePosixPath) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _paths_overlap(first: Path | PurePosixPath, second: Path | PurePosixPath) -> bool:
    return _path_is_within(first, second) or _path_is_within(second, first)


def _reject_symlink_components(path: PurePosixPath, pointer: str) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            break
        except OSError:
            _fail("path_metadata_unavailable", pointer)
        if stat.S_ISLNK(metadata.st_mode):
            _fail("symlink_path_component", pointer)


def _absolute_path(value: Any, pointer: str) -> PurePosixPath:
    text = _string(value, pointer, code="invalid_path")
    if not text.startswith("/") or text.startswith("//"):
        _fail("path_not_absolute", pointer)
    if posixpath.normpath(text) != text or str(PurePosixPath(text)) != text:
        _fail("path_not_canonical", pointer)
    path = PurePosixPath(text)
    if len(path.parts) < 4:
        _fail("unsafe_staging_path", pointer)
    _reject_symlink_components(path, pointer)
    return path


def _reject_source_tree_overlap(
    path: PurePosixPath, pointer: str, source_tree_root: Path
) -> None:
    lexical_root = PurePosixPath(str(source_tree_root.absolute()))
    resolved_root = PurePosixPath(str(source_tree_root.resolve(strict=True)))
    if _paths_overlap(path, lexical_root):
        _fail("source_tree_path", pointer)
    resolved_path = PurePosixPath(str(Path(path).resolve(strict=False)))
    if _paths_overlap(resolved_path, resolved_root):
        _fail("source_tree_path", pointer)


def _validate_paths(plan: dict[str, Any], source_tree_root: Path) -> None:
    plan_id = plan["plan_id"]
    staging: list[PurePosixPath] = []
    tokens: list[PurePosixPath] = []
    copybacks: list[PurePosixPath] = []

    for index, host in enumerate(plan["hosts"]):
        base = f"/hosts/{index}"
        stage = _absolute_path(host["staging_root"], base + "/staging_root")
        _reject_source_tree_overlap(stage, base + "/staging_root", source_tree_root)
        token = _absolute_path(host["token_file_path"], base + "/token_file_path")
        _reject_source_tree_overlap(token, base + "/token_file_path", source_tree_root)
        copyback = _absolute_path(
            host["evidence_copyback_destination"],
            base + "/evidence_copyback_destination",
        )
        _reject_source_tree_overlap(
            copyback, base + "/evidence_copyback_destination", source_tree_root
        )
        staging.append(stage)
        tokens.append(token)
        copybacks.append(copyback)

    if _paths_overlap(staging[0], staging[1]):
        _fail("overlapping_staging_roots", "/hosts")

    for index, (host, stage, token, copyback) in enumerate(
        zip(plan["hosts"], staging, tokens, copybacks)
    ):
        base = f"/hosts/{index}"
        expected_stage_tail = (
            "mycelium-physical-qualification",
            plan_id,
            host["host_name"],
        )
        if tuple(stage.parts[-3:]) != expected_stage_tail:
            _fail("unsafe_staging_path", base + "/staging_root")
        if tuple(stage.parts[1:3]) != ("Users", host["ssh_user"]):
            _fail("staging_user_mismatch", base + "/staging_root")
        if not _path_is_within(token, stage) or token == stage:
            _fail("token_file_outside_staging", base + "/token_file_path")
        relative_token = token.relative_to(stage)
        if len(relative_token.parts) < 2 or relative_token.parts[0] != ".credentials":
            _fail("unsafe_token_file_path", base + "/token_file_path")
        if any(_paths_overlap(copyback, candidate) for candidate in staging):
            _fail("copyback_staging_overlap", base + "/evidence_copyback_destination")
        expected_copyback_tail = (
            "mycelium-physical-qualification-evidence",
            plan_id,
            host["host_name"],
        )
        if tuple(copyback.parts[-3:]) != expected_copyback_tail:
            _fail("unsafe_copyback_path", base + "/evidence_copyback_destination")

    if _paths_overlap(copybacks[0], copybacks[1]):
        _fail("overlapping_copyback_destinations", "/hosts")


def _validate_hosts(plan: dict[str, Any]) -> None:
    hosts = plan["hosts"]
    if len(hosts) != 2:
        _fail("invalid_host_count", "/hosts")
    if [host.get("role") for host in hosts] != ["coordinator", "peer"]:
        _fail("invalid_host_roles", "/hosts")

    names: list[str] = []
    endpoint_ids: list[str] = []
    assignment_ids: list[str] = []
    for index, host in enumerate(hosts):
        base = f"/hosts/{index}"
        names.append(_validate_host_name(host["host_name"], base + "/host_name"))
        _validate_user(host["ssh_user"], base + "/ssh_user")
        endpoint_ids.append(
            _validate_slug(host["endpoint_id"], base + "/endpoint_id", "invalid_endpoint_id")
        )
        _positive_int(
            host["expected_generation"],
            base + "/expected_generation",
            "invalid_expected_generation",
        )
        assignment_ids.append(
            _validate_slug(
                host["assignment_id"], base + "/assignment_id", "invalid_assignment_id"
            )
        )
        _validate_digest(host["assignment_digest"], base + "/assignment_digest")
    if len(set(names)) != 2:
        _fail("duplicate_host_name", "/hosts")
    if len(set(endpoint_ids)) != 2:
        _fail("duplicate_endpoint_id", "/hosts")
    if len(set(assignment_ids)) != 2:
        _fail("duplicate_assignment_id", "/hosts")


def _validate_coordinator(plan: dict[str, Any]) -> None:
    coordinator = plan["coordinator"]
    _validate_address(coordinator["address"], "/coordinator/address")
    port = _positive_int(coordinator["port"], "/coordinator/port", "invalid_coordinator_port")
    if port < 1024 or port > 65535:
        _fail("invalid_coordinator_port", "/coordinator/port")
    if coordinator["host_name"] != plan["hosts"][0]["host_name"]:
        _fail("coordinator_host_mismatch", "/coordinator/host_name")


def _validate_identities(plan: dict[str, Any]) -> None:
    identities = plan["identities"]
    for key in ("deployment_id", "route_id"):
        _validate_slug(identities[key], f"/identities/{key}", f"invalid_{key}")
    for key in ("deployment_epoch", "topology_generation"):
        _positive_int(identities[key], f"/identities/{key}", f"invalid_{key}")
    model_id = _string(identities["model_id"], "/identities/model_id", code="invalid_model_id")
    if not _MODEL_RE.fullmatch(model_id):
        _fail("invalid_model_id", "/identities/model_id")
    commit = _string(
        identities["resolved_commit"],
        "/identities/resolved_commit",
        code="invalid_resolved_commit",
    )
    if not _COMMIT_RE.fullmatch(commit):
        _fail("invalid_resolved_commit", "/identities/resolved_commit")
    for key in (
        "assignment_bundle_digest",
        "execution_graph_digest",
        "model_manifest_digest",
        "route_plan_digest",
    ):
        _validate_digest(identities[key], f"/identities/{key}")


def _same_identity(first: os.stat_result, second: os.stat_result) -> bool:
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _source_file_open_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    return flags


def _verify_source_file_at(
    root_descriptor: int,
    relative_path: PurePosixPath,
    pointer: str,
) -> None:
    try:
        directory_descriptor = os.dup(root_descriptor)
    except OSError:
        _fail("source_file_not_regular", pointer)
    try:
        for component in relative_path.parts[:-1]:
            child_descriptor: int | None = None
            try:
                entry = os.stat(
                    component,
                    dir_fd=directory_descriptor,
                    follow_symlinks=False,
                )
                child_descriptor = os.open(
                    component,
                    _directory_open_flags(),
                    dir_fd=directory_descriptor,
                )
                opened = os.fstat(child_descriptor)
            except OSError:
                if child_descriptor is not None:
                    os.close(child_descriptor)
                _fail("source_file_outside_tree", pointer)
            if (
                not stat.S_ISDIR(entry.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or not _same_identity(entry, opened)
            ):
                os.close(child_descriptor)
                _fail("source_file_outside_tree", pointer)
            os.close(directory_descriptor)
            directory_descriptor = child_descriptor

        final_name = relative_path.parts[-1]
        file_descriptor: int | None = None
        try:
            entry = os.stat(
                final_name,
                dir_fd=directory_descriptor,
                follow_symlinks=False,
            )
            file_descriptor = os.open(
                final_name,
                _source_file_open_flags(),
                dir_fd=directory_descriptor,
            )
            opened = os.fstat(file_descriptor)
        except OSError:
            if file_descriptor is not None:
                os.close(file_descriptor)
            _fail("source_file_not_regular", pointer)
        try:
            if (
                not stat.S_ISREG(entry.st_mode)
                or not stat.S_ISREG(opened.st_mode)
                or not _same_identity(entry, opened)
            ):
                _fail("source_file_not_regular", pointer)
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
    finally:
        os.close(directory_descriptor)


def _validate_sources(plan: dict[str, Any], source_tree_root: Path) -> None:
    files = plan["source_files"]
    if type(files) is not list or not files:
        _fail("invalid_source_files", "/source_files")
    for index, value in enumerate(files):
        pointer = f"/source_files/{index}"
        if type(value) is not str or not _SOURCE_RE.fullmatch(value):
            _fail("unsafe_source_file", pointer)
        candidate = PurePosixPath(value)
        if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
            _fail("unsafe_source_file", pointer)
        if any(part.startswith(".") for part in candidate.parts):
            _fail("unsafe_source_file", pointer)
        if _UNSAFE_SOURCE_NAME_RE.search(value):
            _fail("unsafe_source_file", pointer)
    if len(set(files)) != len(files):
        _fail("duplicate_source_file", "/source_files")
    if files != sorted(files):
        _fail("noncanonical_source_file_order", "/source_files")

    root = source_tree_root.absolute()
    root_descriptor: int | None = None
    try:
        root_entry = root.lstat()
        root_descriptor = os.open(root, _directory_open_flags())
        root_opened = os.fstat(root_descriptor)
    except OSError:
        if root_descriptor is not None:
            os.close(root_descriptor)
        _fail("invalid_source_tree")
    try:
        if (
            not stat.S_ISDIR(root_entry.st_mode)
            or not stat.S_ISDIR(root_opened.st_mode)
            or not _same_identity(root_entry, root_opened)
        ):
            _fail("invalid_source_tree")
        for index, value in enumerate(files):
            _verify_source_file_at(
                root_descriptor,
                PurePosixPath(value),
                f"/source_files/{index}",
            )
        try:
            root_after = root.lstat()
        except OSError:
            _fail("source_tree_changed")
        if not _same_identity(root_opened, root_after):
            _fail("source_tree_changed")
    finally:
        if root_descriptor is not None:
            os.close(root_descriptor)


def _validate_matrix(plan: dict[str, Any]) -> None:
    cold = plan["run_matrix"]["cold"]
    warm = plan["run_matrix"]["warm"]
    valid = (
        cold["cache_precondition"] == "absent"
        and cold["local_files_only"] is False
        and cold["expected_network_bytes"] == "positive"
        and type(cold["local_files_only"]) is bool
        and warm["cache_precondition"] == "same_pinned_assignment"
        and warm["local_files_only"] is True
        and warm["expected_network_bytes"] == "zero"
        and type(warm["local_files_only"]) is bool
    )
    if not valid:
        _fail("invalid_run_matrix", "/run_matrix")


def _validate_decode(plan: dict[str, Any]) -> None:
    decode = plan["decode_parity"]
    if type(decode["decode_steps"]) is not int or decode["decode_steps"] != 8:
        _fail("invalid_decode_steps", "/decode_parity/decode_steps")
    valid = (
        decode["mode"] == "stage_local_kv"
        and decode["oracle"] == "independently_loaded_monolithic"
        and decode["token_match"] == "exact"
        and decode["require_single_token_decode"] is True
        and type(decode["require_single_token_decode"]) is bool
        and decode["require_no_full_prefix"] is True
        and type(decode["require_no_full_prefix"]) is bool
    )
    if not valid:
        _fail("invalid_decode_requirements", "/decode_parity")
    _positive_float(
        decode["activation_abs_tolerance"],
        "/decode_parity/activation_abs_tolerance",
    )
    _positive_float(
        decode["final_logits_abs_tolerance"],
        "/decode_parity/final_logits_abs_tolerance",
    )
    _exact_list(
        decode["per_step_evidence"],
        PER_STEP_EVIDENCE,
        "/decode_parity/per_step_evidence",
        "invalid_decode_evidence",
    )


def _validate_evidence_cleanup_rollback(plan: dict[str, Any]) -> None:
    expected_order = [host["host_name"] for host in plan["hosts"]]
    evidence = plan["evidence"]
    if evidence["copyback_order"] != expected_order:
        _fail("invalid_evidence_plan", "/evidence/copyback_order")
    for key in (
        "preserve_distinct_cold_and_warm",
        "require_immutable_manifest",
        "verify_before_cleanup",
    ):
        _exact_bool(evidence[key], True, f"/evidence/{key}", "invalid_evidence_plan")

    cleanup = plan["cleanup"]
    for key in CLEANUP_FIELDS:
        _exact_bool(cleanup[key], True, f"/cleanup/{key}", "invalid_cleanup_plan")

    rollback = plan["rollback"]
    if (
        rollback["scope"] != "run_scoped_only"
        or rollback["order"]
        != "stop_then_copy_partial_evidence_then_cleanup_if_copyback_verified"
        or rollback["preserve_remote_evidence_on_copyback_failure"] is not True
        or type(rollback["preserve_remote_evidence_on_copyback_failure"]) is not bool
        or rollback["require_reauthorization_after_abort"] is not True
        or type(rollback["require_reauthorization_after_abort"]) is not bool
    ):
        _fail("invalid_rollback_plan", "/rollback")


def _authorization_statement(plan: dict[str, Any]) -> str:
    coordinator, peer = plan["hosts"]
    address = plan["coordinator"]
    return (
        "I explicitly authorize a later Mycelium physical qualification between "
        f"{coordinator['host_name']} as SSH user {coordinator['ssh_user']} and "
        f"{peer['host_name']} as SSH user {peer['ssh_user']}; stage only the declared "
        f"source_files under {coordinator['staging_root']} and {peer['staging_root']}; "
        f"use only token-file indirection at {coordinator['token_file_path']} and "
        f"{peer['token_file_path']}; bind the coordinator to "
        f"{address['address']}:{address['port']}; copy evidence to "
        f"{coordinator['evidence_copyback_destination']} and "
        f"{peer['evidence_copyback_destination']}; then apply the declared cleanup, "
        "rollback, and abort conditions. This statement authorizes only a later "
        "operator run; this validator performs no physical qualification."
    )


def _validate_semantics(plan: dict[str, Any], source_tree_root: Path) -> None:
    _exact_string(plan["protocol"], PROTOCOL, "/protocol", "invalid_protocol")
    _validate_slug(plan["plan_id"], "/plan_id", "invalid_plan_id")
    _validate_hosts(plan)
    _validate_coordinator(plan)
    _validate_identities(plan)
    _validate_paths(plan, source_tree_root)
    _validate_sources(plan, source_tree_root)
    _validate_matrix(plan)
    _validate_decode(plan)
    _exact_list(
        plan["negative_tests"],
        NEGATIVE_TESTS,
        "/negative_tests",
        "invalid_negative_tests",
    )
    _exact_list(
        plan["abort_conditions"],
        ABORT_CONDITIONS,
        "/abort_conditions",
        "invalid_abort_conditions",
    )
    _validate_evidence_cleanup_rollback(plan)
    statement = _string(
        plan["authorization_statement"],
        "/authorization_statement",
        code="authorization_statement_mismatch",
    )
    if statement != _authorization_statement(plan):
        _fail("authorization_statement_mismatch", "/authorization_statement")


def validate_and_generate(
    encoded_plan: bytes,
    *,
    source_tree_root: Path,
) -> dict[str, Any]:
    """Validate canonical operator JSON and return an inert canonical-plan value."""
    if not isinstance(source_tree_root, Path):
        _fail("invalid_source_tree")
    try:
        if not source_tree_root.is_dir():
            _fail("invalid_source_tree")
    except OSError:
        _fail("invalid_source_tree")
    plan = _parse_canonical_json(encoded_plan)
    _scan_for_credentials(plan)
    _validate_schema(plan)
    _validate_semantics(plan, source_tree_root)
    execution_plan = generate_execution_plan(plan, encoded_plan)
    _scan_for_credentials(execution_plan)
    return execution_plan


def read_plan_file(path: Path) -> bytes:
    """Read one regular, non-symlink input file without following its final component."""
    try:
        metadata = path.lstat()
    except OSError:
        _fail("input_unavailable")
    if stat.S_ISLNK(metadata.st_mode):
        _fail("input_symlink")
    if not stat.S_ISREG(metadata.st_mode):
        _fail("input_not_regular")
    flags = _source_file_open_flags()
    try:
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                _fail("input_not_regular")
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                _fail("input_changed")
            encoded = os.read(descriptor, _MAX_PLAN_BYTES + 1)
        finally:
            os.close(descriptor)
    except PreflightValidationError:
        raise
    except OSError:
        _fail("input_unavailable")
    if len(encoded) > _MAX_PLAN_BYTES:
        _fail("invalid_input_size")
    return encoded
