#!/usr/bin/env python3
"""Deterministic, read-only release security checks for tracked source files."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

PROTOCOL = "mycelium.release_security_audit.v1"
MAX_FILE_BYTES = 16 * 1024 * 1024
CLAIM_BOUNDARY = {
    "authenticated_transport_evaluated": False,
    "dependency_vulnerabilities_evaluated": False,
    "history_evaluated": False,
    "physical_qualification_evaluated": False,
    "runtime_security_evaluated": False,
    "scope": "tracked working-tree files and Python CLI declarations only",
    "untracked_files_evaluated": False,
}

_PRIVATE_KEY_PATTERN = re.compile(
    rb"-----BEGIN (?P<label>(?:(?:RSA|EC|DSA|OPENSSH|ENCRYPTED) )?PRIVATE KEY)-----\r?\n"
    rb"(?:(?:Proc-Type|DEK-Info):[^\r\n]+\r?\n)*"
    rb"[A-Za-z0-9+/=\r\n]{40,}"
    rb"-----END (?P=label)-----"
)
_PREFIXED_TOKEN_PATTERN = re.compile(
    rb"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    rb"hf_[A-Za-z0-9]{20,}|sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
    rb"xox[baprs]-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16})"
)
_SECRET_PATTERNS = (
    (
        "private_key_material",
        _PRIVATE_KEY_PATTERN,
    ),
    (
        "prefixed_access_token",
        _PREFIXED_TOKEN_PATTERN,
    ),
)
_CREDENTIAL_BASENAMES = frozenset(
    {
        ".env",
        "credentials.json",
        "credentials.yaml",
        "credentials.yml",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
        "id_rsa",
        "secrets.json",
        "secrets.yaml",
        "secrets.yml",
    }
)
_ENV_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
_SECRET_OPTION_WORDS = frozenset(
    {"apikey", "credential", "credentials", "password", "secret", "token"}
)
_REFERENCE_OPTION_SUFFIXES = ("-env", "-fd", "-file", "-path", "-ref")


def canonical_json(value: Any) -> str:
    """Serialize deterministic JSON with one trailing newline."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _redact_output(value: str) -> str:
    encoded = value.encode("utf-8")
    has_private_key_marker = b"-----BEGIN " in encoded and b"PRIVATE KEY-----" in encoded
    if has_private_key_marker or _PREFIXED_TOKEN_PATTERN.search(encoded) is not None:
        digest = hashlib.sha256(encoded).hexdigest()
        return f"<redacted:sha256:{digest}>"
    return value


def _finding(code: str, path: str, **fields: object) -> dict[str, object]:
    safe_fields = {
        key: _redact_output(value) if isinstance(value, str) else value
        for key, value in fields.items()
    }
    return {"code": code, "path": _redact_output(path), **safe_fields}


def _result(
    findings: list[dict[str, object]],
    *,
    tracked_files: int,
    scanned_files: int,
    scanned_bytes: int,
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
            "max_file_bytes": MAX_FILE_BYTES,
            "scanned_bytes": scanned_bytes,
            "scanned_files": scanned_files,
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
    if metadata.st_size > MAX_FILE_BYTES:
        return None, "tracked_file_too_large"
    try:
        with current.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return None, "tracked_file_changed_during_read"
            content = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None, "tracked_file_read_error"
    if len(content) > MAX_FILE_BYTES:
        return None, "tracked_file_too_large"
    return content, None


def _credential_path(relative_path: str) -> bool:
    name = PurePosixPath(relative_path).name.lower()
    if name == ".env":
        return True
    if name.startswith(".env."):
        return not name.endswith(_ENV_EXAMPLE_SUFFIXES)
    return name in _CREDENTIAL_BASENAMES or name.endswith((".p12", ".pfx"))


def _secret_findings(relative_path: str, content: bytes) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for code, pattern in _SECRET_PATTERNS:
        match = pattern.search(content)
        if match is not None:
            line = content.count(b"\n", 0, match.start()) + 1
            findings.append(_finding(code, relative_path, line=line))
    return findings


def _secret_option(option: str) -> bool:
    normalized = option.partition("=")[0].lower().replace("_", "-")
    if not normalized.startswith("--") or normalized.endswith(_REFERENCE_OPTION_SUFFIXES):
        return False
    words = tuple(word for word in normalized[2:].split("-") if word)
    return bool(_SECRET_OPTION_WORDS.intersection(words)) or any(
        words[index : index + 2] == ("api", "key")
        for index in range(max(0, len(words) - 1))
    )


def _cli_findings(relative_path: str, content: bytes) -> list[dict[str, object]]:
    if not relative_path.endswith(".py"):
        return []
    try:
        tree = ast.parse(content, filename=relative_path)
    except (SyntaxError, UnicodeDecodeError):
        return [_finding("python_parse_error", relative_path)]
    findings: list[dict[str, object]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        name = function.attr if isinstance(function, ast.Attribute) else (
            function.id if isinstance(function, ast.Name) else ""
        )
        if name != "add_argument":
            continue
        for argument in node.args:
            if (
                isinstance(argument, ast.Constant)
                and isinstance(argument.value, str)
                and _secret_option(argument.value)
            ):
                findings.append(
                    _finding(
                        "secret_cli_argument",
                        relative_path,
                        line=node.lineno,
                        subject=argument.value.partition("=")[0],
                    )
                )
    return findings


def audit_repository(repo_root: str | Path) -> dict[str, object]:
    """Audit tracked working-tree files without modifying the repository."""
    findings: list[dict[str, object]] = []
    try:
        root = Path(repo_root).resolve(strict=True)
    except OSError:
        return _result(
            [_finding("repository_root_unavailable", ".")],
            tracked_files=0,
            scanned_files=0,
            scanned_bytes=0,
        )
    if not root.is_dir():
        return _result(
            [_finding("repository_root_not_directory", ".")],
            tracked_files=0,
            scanned_files=0,
            scanned_bytes=0,
        )
    try:
        git_root = Path(_git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve(
            strict=True
        )
        if git_root != root:
            raise ValueError("path is not repository root")
        entries = _tracked_entries(root)
    except (OSError, UnicodeDecodeError, ValueError):
        return _result(
            [_finding("git_inventory_failed", ".")],
            tracked_files=0,
            scanned_files=0,
            scanned_bytes=0,
        )

    seen_paths: set[str] = set()
    scanned_files = 0
    scanned_bytes = 0
    for relative_path, mode, stage in entries:
        if relative_path in seen_paths:
            findings.append(_finding("duplicate_index_path", relative_path))
            continue
        seen_paths.add(relative_path)
        if stage != "0":
            findings.append(_finding("unmerged_index_entry", relative_path))
            continue
        if mode == "120000":
            findings.append(_finding("tracked_symlink", relative_path))
            continue
        if mode not in {"100644", "100755"}:
            findings.append(_finding("tracked_non_file", relative_path, subject=mode))
            continue
        if _credential_path(relative_path):
            findings.append(_finding("tracked_credential_path", relative_path))

        content, error = _read_regular_file(root, relative_path)
        if error is not None:
            findings.append(_finding(error, relative_path))
            continue
        assert content is not None
        scanned_files += 1
        scanned_bytes += len(content)
        findings.extend(_secret_findings(relative_path, content))
        findings.extend(_cli_findings(relative_path, content))

    return _result(
        findings,
        tracked_files=len(seen_paths),
        scanned_files=scanned_files,
        scanned_bytes=scanned_bytes,
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
        print(f"release security audit OK: {scan['scanned_files']} tracked files")
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
