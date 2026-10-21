# SPDX-License-Identifier: AGPL-3.0-only
"""W5 churn gate: fixture tests, including the positive controls that prove it CAN fail.

The gate's whole value is that it names churn. A gate only ever seen on real, passing data has not been
shown to be able to fail at all -- so every failure mode below is driven by a synthetic tree, and the
"clean" fixture exists so a broken gate cannot pass by refusing everything.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parents[2] / "scripts" / "a13_operator" / "churn_gate.py"
RUN_IDS = ["a13-qwen-20260101T000001Z", "a13-qwen-20260101T000002Z", "a13-qwen-20260101T000003Z"]


def _write_owner(root: Path, gens: list[int], depths: list[int]) -> None:
    for run, gen, dep in zip(RUN_IDS, gens, depths):
        d = root / "local-contributor" / "managed" / run / "pump"
        d.mkdir(parents=True, exist_ok=True)
        suffix = "".join(f".r{i}" for i in range(1, dep + 1))
        (d / "state.json").write_text(json.dumps({
            "binding": {"membership_generation": gen,
                        "member_incarnation": f"remote-incarnation-abc{suffix}"}}))


def _write_target(records: Path, gens: list[int], depths: list[int]) -> None:
    for run, gen, dep in zip(RUN_IDS, gens, depths):
        d = records / run / "pump"
        d.mkdir(parents=True, exist_ok=True)
        suffix = "".join(f".r{i}" for i in range(1, dep + 1))
        (d / "state.json").write_text(json.dumps({
            "binding": {"membership_generation": gen,
                        "member_incarnation": f"remote-incarnation-def{suffix}"}}))


def _write_durable(path: Path, restart_count: int, generation: int, dep: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = "".join(f".r{i}" for i in range(1, dep + 1))
    path.write_text(json.dumps({
        "protocol": "mycelium.node.membership_state.v1",
        "node_id": "remote-mac-fixture",
        "restart_count": restart_count,
        "membership_generation": generation,
        "incarnation": f"remote-incarnation-fed{suffix}"}))


def _run(tmp_path: Path, *, owner: Path | None = None, records: Path | None = None,
         durable: Path | None = None) -> subprocess.CompletedProcess:
    argv = [sys.executable, str(GATE),
            "--owner-root", str(owner if owner is not None else tmp_path / "owner"),
            "--target-records", str(records if records is not None else tmp_path / "records"),
            "--target-durable", str(durable if durable is not None else tmp_path / "durable.json")]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120)


def _clean(tmp_path: Path):
    owner, records, durable = tmp_path / "owner", tmp_path / "records", tmp_path / "durable.json"
    _write_owner(owner, [1, 2, 3], [0, 1, 2])
    _write_target(records, [2, 3, 4], [1, 2, 3])
    _write_durable(durable, 3, 4, 3)
    return owner, records, durable


def test_clean_both_sides_passes(tmp_path):
    owner, records, durable = _clean(tmp_path)
    r = _run(tmp_path, owner=owner, records=records, durable=durable)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "CHURN_GATE PASS" in r.stdout
    assert "TARGET side -- 3 rounds" in r.stdout


def test_target_generation_jump_is_named(tmp_path):
    """Positive control: the gate must FAIL and name the round when the target skips a generation."""
    owner, records, durable = _clean(tmp_path)
    _write_target(records, [2, 4, 5], [1, 3, 4])   # +2 between rounds: churn
    r = _run(tmp_path, owner=owner, records=records, durable=durable)
    assert r.returncode == 1, r.stdout
    assert "CHURN_GATE FAIL" in r.stdout
    assert "generation moved by 2" in r.stdout
    assert RUN_IDS[1] in r.stdout


def test_owner_depth_mismatch_is_named(tmp_path):
    """Positive control on the owner side: depth must equal generation-1."""
    owner, records, durable = _clean(tmp_path)
    _write_owner(owner, [1, 2, 3], [0, 0, 2])      # round 2 carries no re-incarnation
    r = _run(tmp_path, owner=owner, records=records, durable=durable)
    assert r.returncode == 1, r.stdout
    assert "depth r0 != generation-1 r1" in r.stdout


def test_durable_restart_count_must_match_the_rounds_observed(tmp_path):
    """Positive control: the node's own restart_count is the per-round delta one level up."""
    owner, records, durable = _clean(tmp_path)
    _write_durable(durable, 9, 10, 9)
    r = _run(tmp_path, owner=owner, records=records, durable=durable)
    assert r.returncode == 1, r.stdout
    assert "restart_count 9 != 3 rounds observed" in r.stdout


def test_missing_target_records_are_reported_not_assumed_clean(tmp_path):
    """No target records must still PASS the owner side while saying the target was not checked."""
    owner, records, durable = _clean(tmp_path)
    r = _run(tmp_path, owner=owner, records=tmp_path / "absent", durable=tmp_path / "absent.json")
    assert r.returncode == 0, r.stdout
    assert "TARGET side -- NO DATA" in r.stdout
    assert "target side was NOT checked" in r.stdout
    assert "restart_count cannot be checked" in r.stdout


def test_no_owner_data_exits_7(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 7, r.stdout
    assert "NO_DATA" in r.stdout
