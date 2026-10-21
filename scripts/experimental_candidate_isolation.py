#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only candidate isolation preflight, never an authority or promotion gate.

Input is a private operator JSON document; output contains codes, not path contents.
An operator-supplied remote inventory is a bounded declaration, not a live probe.
No roots, keys, plans, sockets, services, or credentials are created or accessed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import time
from typing import Callable

R8_CANDIDATE = "961409e34fe2bec0c80c81e8d6c4ee6fe6199919"
R8_RUN = "w6-m1-exp-r8-961409e"
SCRUB = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
         "GIT_ALTERNATE_OBJECT_DIRECTORIES", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")
LOCAL_PROTECTED = ("source", "seed", "product", "controller", "worker_stage", "public_proxy")
LOCAL_CANDIDATE = ("source", "seed", "product", "controller", "worker_stage", "state")
FUTURE_GATES = ("physical_inventory_and_signed_qualification", "full_regression",
                "consumer_proof", "new_attempt_authorization", "separate_promotion_review")


def _absolute(raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw:
        return None
    path = Path(raw)
    if any(part in (".", "..") for part in raw.split("/")) or str(path) != raw.rstrip("/"):
        return None
    return path


def _remote_path(raw: object) -> PurePosixPath | None:
    if not isinstance(raw, str) or not raw.startswith("/") or "\x00" in raw:
        return None
    path = PurePosixPath(raw)
    if any(part in (".", "..") for part in raw.split("/")) or str(path) != raw.rstrip("/"):
        return None
    return path


def _overlaps(a, b) -> bool:
    return a == b or a in b.parents or b in a.parents


def _source_probe(root: Path) -> tuple[str, bool]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["GIT_OPTIONAL_LOCKS"] = "0"  # status must not refresh/write the index
    def git(*args: str) -> str:
        result = subprocess.run(("git", "-c", "core.fsmonitor=false", "-C", str(root), *args), env=env,
                                capture_output=True, text=True, timeout=5, check=True)
        return result.stdout.strip()
    top = git("rev-parse", "--show-toplevel")
    if Path(top).resolve() != root.resolve():
        raise ValueError("source_not_checkout_root")
    return git("rev-parse", "HEAD"), bool(git("status", "--porcelain", "-uall"))


def _listener_probe(port: int) -> str:
    try:
        result = subprocess.run(("lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"),
                                capture_output=True, timeout=3, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode == 0:
        return "occupied"
    if result.returncode == 1 and not result.stdout and not result.stderr:
        return "free"
    return "unknown"


def check(document: object, *, now_unix_ms: int | None = None,
          source_probe: Callable[[Path], tuple[str, bool]] = _source_probe,
          listener_probe: Callable[[int], str] = _listener_probe) -> dict:
    """Check snapshot separation only; all errors return BLOCKED without side effects."""
    reasons: set[str] = set()
    if now_unix_ms is None:
        now_unix_ms = int(time.time() * 1000)
    def obj(value):
        return value if isinstance(value, dict) else {}
    doc = obj(document)
    protected, candidate, remote = (obj(doc.get(name)) for name in
                                     ("protected", "candidate", "remote"))
    if doc.get("schema") != "candidate_isolation_preflight.v1":
        reasons.add("malformed_identity")
    old_id, new_id = protected.get("candidate_id"), candidate.get("candidate_id")
    old_run, new_run = protected.get("run_id"), candidate.get("run_id")
    commit = candidate.get("source_commit")
    if (not isinstance(old_id, str) or not re.fullmatch(r"[0-9a-f]{40}", old_id)
            or not isinstance(new_id, str) or not re.fullmatch(r"[0-9a-f]{40}", new_id)
            or not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit)
            or not isinstance(old_run, str) or not old_run.strip()
            or not isinstance(new_run, str) or not new_run.strip()):
        reasons.add("malformed_identity")
    if old_id != R8_CANDIDATE or old_run != R8_RUN:
        reasons.add("frozen_identity_not_verified")
    if (new_id in (old_id, R8_CANDIDATE) or commit in (old_id, R8_CANDIDATE)
            or new_run in (old_run, R8_RUN) or (isinstance(new_run, str) and "r8-961409e" in new_run)):
        reasons.add("consumed_identity")
    if new_id != commit:
        reasons.add("source_dirty_or_mismatch")
    # This is before signing and before an attempt. Even a differently named
    # authorization/attempt reference is not admission to reuse old authority.
    if (candidate.get("authorization_status") != "not_signed"
            or candidate.get("attempt_status") != "not_started"
            or candidate.get("authorization_ref") is not None
            or candidate.get("attempt_ref") is not None):
        reasons.add("prior_authority_or_attempt")
    binding = obj(candidate.get("binding"))
    issued, expires = binding.get("issued_at_unix_ms"), binding.get("expires_at_unix_ms")
    if (binding.get("candidate_id") != new_id or binding.get("source_commit") != commit
            or binding.get("status") != "unconsumed" or type(issued) is not int
            or type(expires) is not int or not (0 < issued <= now_unix_ms < expires)
            or expires - issued > 24 * 60 * 60 * 1000):
        reasons.add("invalid_binding")

    old_roots, new_roots = obj(protected.get("roots")), obj(candidate.get("roots"))
    old_paths = {key: _absolute(old_roots.get(key)) for key in LOCAL_PROTECTED}
    new_paths = {key: _absolute(new_roots.get(key)) for key in LOCAL_CANDIDATE}
    if any(value is None for value in (*old_paths.values(), *new_paths.values())):
        reasons.add("invalid_root")
    else:
        old_checked = {key: path for key, path in old_paths.items() if path is not None}
        new_checked = {key: path for key, path in new_paths.items() if path is not None}
        # Missing protected roots cannot establish an inventory; never create them.
        if any(not path.exists() for path in old_checked.values()):
            reasons.add("protected_root_not_verified")
        for path in new_checked.values():
            if path != new_checked["source"] and (path.exists() or path.is_symlink()):
                reasons.add("candidate_root_exists")
        for path in new_checked.values():
            for protected_path in old_checked.values():
                if (_overlaps(path, protected_path)
                        or _overlaps(path.resolve(strict=False), protected_path.resolve(strict=False))):
                    reasons.add("root_overlap")
        for a_key, a_path in new_checked.items():
            for b_key, b_path in new_checked.items():
                if a_key < b_key and (_overlaps(a_path, b_path) or
                                      _overlaps(a_path.resolve(strict=False), b_path.resolve(strict=False))):
                    reasons.add("candidate_root_overlap")
        source = new_checked["source"]
        if not source.is_dir():
            reasons.add("source_not_verified")
        elif not ("consumed_identity" in reasons):
            try:
                actual_commit, dirty = source_probe(source)
                if actual_commit != commit or dirty:
                    reasons.add("source_dirty_or_mismatch")
            except (OSError, ValueError, subprocess.SubprocessError):
                reasons.add("source_not_verified")

    host = remote.get("host_id")
    old_remote, new_remote = _remote_path(remote.get("protected_root")), _remote_path(remote.get("candidate_root"))
    inventory = obj(remote.get("inventory"))
    observed = obj(inventory.get("roots"))
    captured = inventory.get("captured_at_unix_ms")
    if (not isinstance(host, str) or not host.strip() or old_remote is None or new_remote is None
            or inventory.get("host_id") != host or inventory.get("method") != "read_only_inventory"
            or type(captured) is not int or not (0 < now_unix_ms - captured <= 5 * 60 * 1000)
            or inventory.get("protected_root_exists") is not True
            or inventory.get("candidate_root_exists") is not False
            or old_remote is not None and str(old_remote) not in observed
            or new_remote is not None and str(new_remote) not in observed):
        reasons.add("remote_not_verified")
    if old_remote is not None and new_remote is not None:
        if _overlaps(old_remote, new_remote):
            reasons.add("root_overlap")
        old_real = _remote_path(observed.get(str(old_remote)))
        new_real = _remote_path(observed.get(str(new_remote)))
        if old_real is None or new_real is None:
            reasons.add("remote_not_verified")
        elif _overlaps(old_real, new_real):
            reasons.add("root_overlap")

    protected_ports, candidate_ports = protected.get("ports"), candidate.get("ports")
    def valid_ports(value):
        return isinstance(value, list) and bool(value) and all(
            type(p) is int and 0 < p < 65536 for p in value) and len(value) == len(set(value))
    if (not valid_ports(protected_ports) or not valid_ports(candidate_ports)
            or not {14351, 4350, 4353}.issubset(protected_ports or [])):
        reasons.add("invalid_ports")
    else:
        if set(protected_ports) & set(candidate_ports):
            reasons.add("protected_port")
        for port in candidate_ports:
            if port in protected_ports:
                continue  # do not even inspect a protected listener
            try:
                finding = listener_probe(port)
            except (OSError, ValueError, subprocess.SubprocessError):
                finding = "unknown"
            if finding == "occupied":
                reasons.add("listener_occupied")
            elif finding != "free":
                reasons.add("listener_not_verified")
    remote_old_ports = remote.get("protected_ports")
    remote_new_ports = remote.get("candidate_ports")
    if not valid_ports(remote_old_ports) or not valid_ports(remote_new_ports):
        reasons.add("remote_not_verified")
    else:
        if set(remote_old_ports or []) & set(remote_new_ports or []):
            reasons.add("protected_port")
        listeners = obj(inventory.get("candidate_listeners"))
        if any(listeners.get(str(port)) != "free" for port in (remote_new_ports or [])):
            reasons.add("remote_not_verified")
    return {"verdict": "BLOCKED" if reasons else "preflight_separation_only",
            "reasons": sorted(reasons), "live_access": False, "authorized": False,
            "synthetic": doc.get("synthetic") is True,
            "separation_scope": "operator-declared roots and read-only local snapshot only",
            "remote_inventory_is_operator_supplied_not_live_verified": True,
            "future_gates": list(FUTURE_GATES)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="private operator JSON path")
    args = parser.parse_args()
    try:
        document = json.loads(args.input.read_text(encoding="utf-8"))
        result = check(document)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
        result = {"verdict": "BLOCKED", "reasons": ["input_not_verified"],
                  "live_access": False, "authorized": False, "future_gates": list(FUTURE_GATES)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["verdict"] == "preflight_separation_only" and not result["synthetic"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
