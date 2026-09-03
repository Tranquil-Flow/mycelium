from __future__ import annotations

import inspect
from pathlib import Path
import subprocess
import sys

from scripts import run_a5_product_gate as product_gate


def test_physical_gate_clis_bootstrap_repo_when_run_directly(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    scripts = (
        "run_a5_product_gate.py",
        "run_a5_benchmark_gate.py",
        "run_a5_negative_illegal_gate.py",
        "run_a5_replica_loss_gate.py",
    )

    for script in scripts:
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / script), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        assert result.returncode == 0, (script, result.stderr)


def test_positive_overlap_uses_authoritative_admission_ownership() -> None:
    source = inspect.getsource(product_gate.run_gate)

    assert 'runtime["queue"]["active_request_ids"]' in source
    assert 'item["active_reservations"]' in source
    assert '"overlap_placement_reservation_unproven"' in source
    assert '"stage_local_kv_ownership_unproven"' not in source


def test_positive_report_labels_overlap_proof_owner() -> None:
    source = inspect.getsource(product_gate.run_gate)

    assert '"proof_owner_protocol": overlap["protocol"]' in source
    assert '"placement_reservation_deltas": overlap_reservation_deltas' in source


def test_positive_gate_requires_overlap_work_before_cleanup() -> None:
    counters = {
        "frames_sent": 4,
        "frames_received": 4,
        "applied_operation_count": 2,
        "active_kv_state_count": 1,
    }
    before = {"node-0": {key: 0 for key in counters}}

    assert product_gate._inflight_work_observed(
        before,
        {"node-0": counters},
        {"node-0"},
    )
    assert not product_gate._inflight_work_observed(
        before,
        {"node-0": {**counters, "frames_received": 0}},
        {"node-0"},
    )
    assert not product_gate._inflight_work_observed(before, {}, {"node-0"})


def test_positive_gate_uses_overlap_counters_for_ephemeral_sidecars() -> None:
    source = inspect.getsource(product_gate.run_gate)

    assert '"frames_sent": overlap_peers[node_id]["frames_sent"]' in source
    assert '"frames_received": overlap_peers[node_id]["frames_received"]' in source
    assert '"peer_counters_regressed"' not in source
