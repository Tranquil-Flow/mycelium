#!/usr/bin/env python3
"""Audit fixed readiness claims and Observatory read-only source boundaries."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL = "mycelium.claim_boundary_audit.v1"
ROUTE_READY_AUTHORITY = "mycelium_qualification/qualifier.py"
MAX_SOURCE_BYTES = 4 * 1024 * 1024
CLAIM_BOUNDARY = {
    "authenticated_transport_evaluated": False,
    "dynamic_dispatch_evaluated": False,
    "physical_qualification_evaluated": False,
    "runtime_semantics_evaluated": False,
    "scope": "tracked production source literal claims and Observatory write surfaces only",
    "semantic_qualification_evaluated": False,
}

_MUTATING_HTTP_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_UI_METHOD_PATTERN = re.compile(
    r"\bmethod\s*[:=]\s*(['\"])(POST|PUT|PATCH|DELETE)\1",
    re.IGNORECASE,
)
_UI_CLIENT_CALL_PATTERN = re.compile(
    r"\b(?:axios|client|http|api|request)\.(post|put|patch|delete)\s*\(",
    re.IGNORECASE,
)
_UI_SUFFIXES = frozenset({".js", ".jsx", ".ts", ".tsx"})


def canonical_json(value: Any) -> str:
    """Serialize deterministic JSON with exactly one trailing newline."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _finding(code: str, path: str, **fields: object) -> dict[str, object]:
    return {"code": code, "path": path, **fields}


def _result(
    findings: list[dict[str, object]],
    *,
    tracked_files: int,
    scanned_source_files: int,
    scanned_source_bytes: int,
    allowed_route_ready_literals: int,
) -> dict[str, object]:
    findings.sort(
        key=lambda item: (
            str(item.get("path", "")),
            item.get("line", 0) if isinstance(item.get("line", 0), int) else 0,
            str(item.get("code", "")),
            str(item.get("subject", "")),
        )
    )
    return {
        "claim_boundary": dict(CLAIM_BOUNDARY),
        "findings": findings,
        "ok": not findings,
        "protocol": PROTOCOL,
        "release_ready": False,
        "route_ready": False,
        "scan": {
            "allowed_route_ready_literals": allowed_route_ready_literals,
            "scanned_source_bytes": scanned_source_bytes,
            "scanned_source_files": scanned_source_files,
            "tracked_files": tracked_files,
        },
    }


def _git(repo_root: Path, *args: str) -> bytes:
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    if completed.returncode != 0:
        raise OSError(f"git command failed with exit {completed.returncode}")
    return completed.stdout


def _canonical_path(raw: bytes) -> str:
    path = raw.decode("utf-8", errors="strict")
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise ValueError("non-canonical tracked path")
    return path


def _tracked_entries(repo_root: Path) -> list[tuple[str, str, str]]:
    payload = _git(repo_root, "ls-files", "-z", "--stage")
    entries: list[tuple[str, str, str]] = []
    for record in payload.split(b"\0"):
        if not record:
            continue
        metadata, separator, raw_path = record.partition(b"\t")
        if not separator:
            raise ValueError("malformed git index entry")
        fields = metadata.decode("ascii", errors="strict").split()
        if len(fields) != 3:
            raise ValueError("malformed git index metadata")
        mode, _object_id, stage = fields
        entries.append((_canonical_path(raw_path), mode, stage))
    return entries


def _is_test_path(relative_path: str) -> bool:
    path = PurePosixPath(relative_path)
    name = path.name.lower()
    return (
        path.parts[0] == "tests"
        or "test" in path.parts[:-1]
        or "__tests__" in path.parts
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    )


def _source_kind(relative_path: str) -> str | None:
    if _is_test_path(relative_path):
        return None
    path = PurePosixPath(relative_path)
    if path.suffix == ".py":
        return "python"
    if (
        path.parts[:3] == ("ui", "web", "src")
        and path.suffix.lower() in _UI_SUFFIXES
    ):
        return "observatory_ui"
    return None


def _read_regular_file(repo_root: Path, relative_path: str) -> tuple[bytes | None, str | None]:
    current = repo_root
    parts = PurePosixPath(relative_path).parts
    metadata = repo_root.lstat()
    for index, part in enumerate(parts):
        current = current / part
        try:
            metadata = current.lstat()
        except OSError:
            return None, "tracked_file_missing"
        if stat.S_ISLNK(metadata.st_mode):
            return None, (
                "tracked_symlink" if index == len(parts) - 1 else "tracked_symlink_component"
            )
        if index < len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            return None, "tracked_path_component_not_directory"
    if not stat.S_ISREG(metadata.st_mode):
        return None, "tracked_not_regular_file"
    if metadata.st_size > MAX_SOURCE_BYTES:
        return None, "tracked_source_too_large"
    try:
        with current.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return None, "tracked_file_changed_during_read"
            if opened.st_size > MAX_SOURCE_BYTES:
                return None, "tracked_source_too_large"
            content = handle.read(MAX_SOURCE_BYTES + 1)
            final = os.fstat(handle.fileno())
    except OSError:
        return None, "tracked_file_read_error"
    if len(content) > MAX_SOURCE_BYTES:
        return None, "tracked_source_too_large"
    if (
        (final.st_dev, final.st_ino) != (metadata.st_dev, metadata.st_ino)
        or final.st_size != metadata.st_size
        or final.st_mtime_ns != metadata.st_mtime_ns
    ):
        return None, "tracked_file_changed_during_read"
    return content, None


def _claim_target(target: ast.expr) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        key = target.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            return key.value
    return None


def _literal_claims(tree: ast.AST) -> list[tuple[str, int]]:
    claims: set[tuple[str, int, int]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and isinstance(key.value, str)
                    and key.value in {"route_ready", "release_ready"}
                    and isinstance(value, ast.Constant)
                    and value.value is True
                ):
                    claims.add((key.value, value.lineno, value.col_offset))
        elif isinstance(node, ast.Call):
            for keyword in node.keywords:
                if (
                    keyword.arg in {"route_ready", "release_ready"}
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                ):
                    claims.add((keyword.arg, keyword.value.lineno, keyword.value.col_offset))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, ast.Constant) or value.value is not True:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                claim = _claim_target(target)
                if claim in {"route_ready", "release_ready"}:
                    claims.add((claim, node.lineno, node.col_offset))
        elif isinstance(node, ast.NamedExpr):
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                claim = _claim_target(node.target)
                if claim in {"route_ready", "release_ready"}:
                    claims.add((claim, node.lineno, node.col_offset))
    return [(claim, line) for claim, line, _column in sorted(claims, key=lambda item: (item[1], item[2], item[0]))]


def _claim_findings(
    relative_path: str,
    tree: ast.AST,
) -> tuple[list[dict[str, object]], int]:
    findings: list[dict[str, object]] = []
    allowed = 0
    for claim, line in _literal_claims(tree):
        if claim == "route_ready" and relative_path == ROUTE_READY_AUTHORITY:
            allowed += 1
        elif claim == "route_ready":
            findings.append(
                _finding("route_ready_true_outside_authority", relative_path, line=line)
            )
        else:
            findings.append(_finding("release_ready_true_literal", relative_path, line=line))
    return findings, allowed


def _backend_findings(relative_path: str, tree: ast.AST) -> list[dict[str, object]]:
    if not relative_path.startswith("mycelium_gateway/"):
        return []
    surfaces: set[tuple[int, str]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            method = node.value.upper()
            if method in _MUTATING_HTTP_METHODS:
                surfaces.add((node.lineno, method))
        elif isinstance(node, ast.Attribute):
            method = node.attr.upper()
            if method in _MUTATING_HTTP_METHODS:
                surfaces.add((node.lineno, node.attr))
    return [
        _finding(
            "observatory_backend_write_surface",
            relative_path,
            line=line,
            subject=subject,
        )
        for line, subject in sorted(surfaces)
    ]


def _ui_findings(relative_path: str, content: bytes) -> list[dict[str, object]]:
    try:
        source = content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return [_finding("source_decode_error", relative_path)]
    surfaces: set[tuple[int, str]] = set()
    for pattern in (_UI_METHOD_PATTERN, _UI_CLIENT_CALL_PATTERN):
        for match in pattern.finditer(source):
            method = match.group(2) if pattern is _UI_METHOD_PATTERN else match.group(1)
            line = source.count("\n", 0, match.start()) + 1
            surfaces.add((line, method.upper()))
    return [
        _finding(
            "observatory_ui_write_surface",
            relative_path,
            line=line,
            subject=subject,
        )
        for line, subject in sorted(surfaces)
    ]


def audit_repository(repo_root: str | Path) -> dict[str, object]:
    """Audit tracked production source without modifying repository state."""
    empty = {
        "tracked_files": 0,
        "scanned_source_files": 0,
        "scanned_source_bytes": 0,
        "allowed_route_ready_literals": 0,
    }
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError:
        return _result([_finding("repository_root_unavailable", ".")], **empty)
    if not root.is_dir():
        return _result([_finding("repository_root_not_directory", ".")], **empty)
    try:
        git_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve(
            strict=True
        )
        if git_root != root:
            raise ValueError("path is not repository root")
        entries = _tracked_entries(root)
    except (OSError, UnicodeDecodeError, ValueError):
        return _result([_finding("git_inventory_failed", ".")], **empty)

    findings: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    scanned_source_files = 0
    scanned_source_bytes = 0
    allowed_route_ready_literals = 0

    for relative_path, mode, stage in entries:
        if relative_path in seen_paths:
            findings.append(_finding("duplicate_index_path", relative_path))
            continue
        seen_paths.add(relative_path)
        kind = _source_kind(relative_path)
        if kind is None:
            continue
        if stage != "0":
            findings.append(_finding("unmerged_index_entry", relative_path))
            continue
        if mode == "120000":
            findings.append(_finding("tracked_symlink", relative_path))
            continue
        if mode not in {"100644", "100755"}:
            findings.append(_finding("tracked_non_file", relative_path, subject=mode))
            continue

        content, error = _read_regular_file(root, relative_path)
        if error is not None:
            findings.append(_finding(error, relative_path))
            continue
        assert content is not None
        scanned_source_files += 1
        scanned_source_bytes += len(content)

        if kind == "observatory_ui":
            findings.extend(_ui_findings(relative_path, content))
            continue
        try:
            tree = ast.parse(content, filename=relative_path)
        except (SyntaxError, UnicodeDecodeError):
            findings.append(_finding("python_parse_error", relative_path))
            continue
        claim_findings, allowed = _claim_findings(relative_path, tree)
        findings.extend(claim_findings)
        allowed_route_ready_literals += allowed
        findings.extend(_backend_findings(relative_path, tree))

    return _result(
        findings,
        tracked_files=len(seen_paths),
        scanned_source_files=scanned_source_files,
        scanned_source_bytes=scanned_source_bytes,
        allowed_route_ready_literals=allowed_route_ready_literals,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = audit_repository(args.repo_root)
    if args.json:
        print(canonical_json(result), end="")
    elif result["ok"]:
        scan = result["scan"]
        assert isinstance(scan, dict)
        print(f"claim boundary audit OK: {scan['scanned_source_files']} source files")
    else:
        findings = result["findings"]
        assert isinstance(findings, list)
        for finding in findings:
            assert isinstance(finding, dict)
            location = str(finding["path"])
            if "line" in finding:
                location += f":{finding['line']}"
            print(f"{finding['code']}: {location}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
