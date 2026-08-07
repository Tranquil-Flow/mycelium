"""Shared operator-plan builders for the runner config/state unit suite.

Every document here is pure JSON. No fixture may contain a callable, a private
key, or a credential value: the runner config contract forbids all three.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

PLAN_PROTOCOL = "mycelium.physical_runner_operator_plan.v1"

PUBLIC_KEY_RECORD = {
    "algorithm": "ed25519",
    "encoding": "base64",
    "verification_key": "MCowBQYDK2VwAyEAGb9ECWmEzf6FQbrBZ9w7lshQhqowtrbLDFw4rXAxZuE=",
    "verification_key_digest": "sha256:" + "b" * 64,
}


def operator_plan_payload(workspace: Path, **overrides: Any) -> dict[str, Any]:
    """Return a structurally valid operator plan rooted in ``workspace``."""
    payload: dict[str, Any] = {
        "protocol": PLAN_PROTOCOL,
        "plan_id": "two-mac-g4",
        "run_id": "run-0001",
        "now_unix_ms": 1_800_000_000_000,
        "paths": {
            "evidence_output_dir": str(workspace / "evidence" / "run-0001"),
            "lock_path": str(workspace / "run" / "runner.lock"),
            "state_path": str(workspace / "run" / "runner-state.json"),
            "log_path": str(workspace / "run" / "runner.log"),
        },
        "controller": {
            "mode": "physical",
            "now": 1_800_000_000.0,
            "source_root": str(workspace / "src"),
            "peers": [
                {
                    "node_id": "node-a",
                    "ssh_target": "operator@host-a",
                    "host_id": "host-a",
                    "boot_id": "boot-a",
                    "staging_root": "/opt/mycelium/stage-a",
                },
                {
                    "node_id": "node-b",
                    "ssh_target": "operator@host-b",
                    "host_id": "host-b",
                    "boot_id": "boot-b",
                    "staging_root": "/opt/mycelium/stage-b",
                },
            ],
            "transfer_manifest": {
                "protocol": "mycelium.controller_transfer_manifest.v1",
                "files": [{"path": "physical_inference_node.py"}],
            },
            "membership_snapshot": {
                "protocol": "mycelium.controller_membership_snapshot.v1",
                "deployment_id": "deployment-g4",
            },
            "run_plan": {
                "protocol": "mycelium.controller_run_plan.v1",
                "run_id": "run-0001",
                "deployment_id": "deployment-g4",
                "entry_node_id": "node-a",
                "nodes": [
                    {
                        "node_id": "node-a",
                        "endpoint_secret_file": "/opt/mycelium/identities/node-a/endpoint",
                    }
                ],
            },
        },
        "verification_keys": {
            "gossip": [dict(PUBLIC_KEY_RECORD)],
            "load_proof": [dict(PUBLIC_KEY_RECORD)],
        },
    }
    payload.update(overrides)
    return payload


def write_operator_plan(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path
