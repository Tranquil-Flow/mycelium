"""Browser-safe projection of source and contract governance state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable

from scripts.governance_gate import run as run_governance_gate


PROTOCOL = "mycelium.governance_readiness.v1"
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")


def _git(root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )


def governance_readiness(
    repo_root: str | Path,
    *,
    clock_unix_ms: Callable[[], int] | None = None,
) -> dict[str, Any]:
    root = Path(repo_root).resolve(strict=True)
    ledger_path = root / "contracts" / "governance-ledger.v1.json"
    manifest_path = root / "contracts" / "contract-manifest.v1.json"
    ledger_bytes = ledger_path.read_bytes()
    manifest_bytes = manifest_path.read_bytes()
    ledger = json.loads(ledger_bytes)
    manifest = json.loads(manifest_bytes)
    gate = run_governance_gate(root)
    revision = _git(root, "rev-parse", "HEAD")
    status = _git(root, "status", "--porcelain", "--untracked-files=all")
    source_commit = revision.stdout.strip()
    if revision.returncode != 0 or _COMMIT.fullmatch(source_commit) is None:
        source_commit = None
    source_worktree_clean = status.returncode == 0 and not status.stdout
    return {
        "protocol": PROTOCOL,
        "observed_at_unix_ms": int(
            (clock_unix_ms or (lambda: int(time.time() * 1_000)))()
        ),
        "source_kind": "source_control",
        "source_commit": source_commit,
        "source_worktree_clean": source_worktree_clean,
        "ledger_protocol": ledger["protocol"],
        "ledger_digest": "sha256:" + hashlib.sha256(ledger_bytes).hexdigest(),
        "contract_manifest_protocol": manifest["protocol"],
        "contract_manifest_digest": "sha256:"
        + hashlib.sha256(manifest_bytes).hexdigest(),
        "governance_gate_protocol": gate["protocol"],
        "governance_gate_ok": gate["ok"],
        "authorized_product_action_count": len(ledger["authorized_product_actions"]),
        "capability_count": len(ledger["capabilities"]),
        "milestone_count": len(ledger["milestones"]),
        "release_exclusions": list(ledger["release_exclusions"]),
        "release_ready": False,
    }


__all__ = ["PROTOCOL", "governance_readiness"]
