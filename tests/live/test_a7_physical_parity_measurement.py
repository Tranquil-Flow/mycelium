# SPDX-License-Identifier: AGPL-3.0-only
"""The cross-host parity measurement across three real local placement workers.

Each role (stage-0 entry, stage-1 source, stage-1 STANDBY) runs
``python -m mycelium_a7_parity_worker`` as its own process from its own staging
root, loading its own verified stage pack, exactly as it would on its host; on
the fleet the same argv is prefixed with ssh.  The measured outputs and KV
identities seal the closed parity document and the standby qualification.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from mycelium_a7_parity_measurement import CONFIG_PROTOCOL, ParityRefused, measure_parity
from mycelium_a7_standby_qualification import (
    standby_qualification_is_current,
    validate_physical_parity_challenge,
)
from tests.live import test_a7_product_path as harness

ROOT = Path(__file__).resolve().parents[2]
CANDIDATE = "c" * 40


def _worker_argv(root: Path, *, node_id: str, placement_id: str, assignment: str, stage_pack: str) -> list[str]:
    return [
        sys.executable,
        "-B",
        "-m",
        "mycelium_a7_parity_worker",
        "--root",
        str(root),
        "--graph-file",
        "a7-parity-graph.json",
        "--node-id",
        node_id,
        "--placement-id",
        placement_id,
        "--assignment-file",
        assignment,
        "--stage-pack-file",
        stage_pack,
        "--manifest-file",
        "model-manifest.json",
        "--load-generation",
        "7",
    ]


@pytest.fixture
def staged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    state = harness._build_r7_staged_route(tmp_path, monkeypatch)
    monkeypatch.chdir(ROOT)
    roots = {node: state.root / node / "artifacts" for node in (harness._NODE_A, harness._NODE_B)}
    for root in roots.values():
        harness._write(root / "a7-parity-graph.json", json.loads(json.dumps(asdict(state.graph))))
    try:
        yield state, roots
    finally:
        state.close()


def _config(state: Any, roots: dict[str, Path], tmp_path: Path, *, source_node: str = harness._NODE_B) -> dict[str, Any]:
    graph = state.graph
    entry = graph.stages[0].placements[0]
    source = graph.stages[1].placements[0]
    return {
        "protocol": CONFIG_PROTOCOL,
        "source_node_id": source.node_id,
        "standby_node_id": harness._NODE_A,
        "prompt_token_ids": list(harness._PROMPT),
        "challenge_index": 1,
        "workers": {
            "entry": _worker_argv(roots[harness._NODE_A], node_id=harness._NODE_A, placement_id=entry.placement_id, assignment=f"{harness._NODE_A}-assignment.json", stage_pack=f"{harness._NODE_A}-stage-pack.json"),
            "source": _worker_argv(roots[harness._NODE_B], node_id=source_node, placement_id=source.placement_id, assignment=f"{harness._NODE_B}-assignment.json", stage_pack=f"{harness._NODE_B}-stage-pack.json"),
            "standby": _worker_argv(roots[harness._NODE_A], node_id=harness._NODE_A, placement_id=harness.STANDBY_ID, assignment="a7/standby-assignment.json", stage_pack="a7/standby-stage-pack.json"),
        },
        "max_checkpoint_bytes": 1 << 24,
        "worker_timeout_s": 120,
        "qualification": {"out": str(tmp_path / "qualification.json"), "reserved_kv_bytes": 1 << 20, "ttl_ms": 3_600_000},
    }


def test_three_placement_workers_seal_a_matching_parity_and_qualification(staged: Any, tmp_path: Path) -> None:
    state, roots = staged
    measured = measure_parity(_config(state, roots, tmp_path), candidate_commit=CANDIDATE)

    document = validate_physical_parity_challenge(measured["document"])
    assert document["source_output_digest"] == document["standby_output_digest"]
    assert document["source_identity_digest"] == document["standby_identity_digest"]
    assert document["candidate_commit"] == CANDIDATE
    qualification = measured["qualification"]
    assert qualification["compatible"] is True
    assert qualification["parity_challenge"]["passed"] is True
    # The qualification's challenge carries the very digests the sealed document holds.
    assert qualification["parity_challenge"]["source_output_digest"] == document["source_output_digest"]
    observation = measured["observation"]
    assert observation["checkpoint_bytes"] > 0
    # The standby host's own measured available memory (vm_stat / MemAvailable).
    assert type(observation["standby_available_bytes"]) is int
    assert observation["standby_available_bytes"] >= 1 << 20
    assert qualification["available_bytes"] == observation["standby_available_bytes"]
    assert qualification["qualified"] is True
    assert standby_qualification_is_current(qualification, now_unix_ms=qualification["issued_at_unix_ms"])


def test_a_worker_that_cannot_bind_its_placement_seals_nothing(staged: Any, tmp_path: Path) -> None:
    state, roots = staged
    with pytest.raises(ParityRefused) as refused:
        measure_parity(_config(state, roots, tmp_path, source_node="node-z"), candidate_commit=CANDIDATE)
    assert refused.value.code == "a7_parity_source_not_ready:parity_worker_placement_not_local"


def test_script_seals_the_record_and_qualification_with_the_grant(staged: Any, tmp_path: Path) -> None:
    state, roots = staged
    sys.path.insert(0, str(ROOT / "scripts"))
    import run_a7_kv_successor_gate as gate

    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(gate._canonical(gate.build_plan(CANDIDATE)) + b"\n")
    config_path = tmp_path / "parity-config.json"
    config_path.write_text(json.dumps(_config(state, roots, tmp_path)), encoding="utf-8")
    records = tmp_path / "records"
    env = {key: value for key, value in os.environ.items() if not key.startswith("MYCELIUM_A7_")}
    env.update(MYCELIUM_A7_FLEET_GRANT=gate.EXACT_FLEET_GRANT, MYCELIUM_A7_PARITY_CONFIG=str(config_path), PYTHONDONTWRITEBYTECODE="1")
    command = [sys.executable, "-B", str(ROOT / "scripts" / "a7_physical_parity.py"), "--plan", str(plan_path), "--records", str(records), "--case", gate.POSITIVE]
    completed = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert completed.returncode == 0, completed.stderr
    target = records / "parity" / f"{harness._NODE_B}-{harness._NODE_A}-{gate.POSITIVE}.json"
    document = validate_physical_parity_challenge(json.loads(target.read_text(encoding="utf-8")))
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert json.loads(completed.stdout)["evidence_digest"] == document["evidence_digest"]
    assert (tmp_path / "qualification.json").is_file()
    # A second run never overwrites the sealed record.
    again = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300, check=False)
    assert again.returncode == 2
