# SPDX-License-Identifier: AGPL-3.0-only
"""Offline admission and summary semantics of the decode benchmark (no MLX)."""
from pathlib import Path

import pytest

from scripts import bench_stage_decode as bench


@pytest.mark.parametrize("marker", ["DEVICE.busy", "CERT.busy"])
def test_busy_guard_refuses_before_importing_or_loading_model(tmp_path, monkeypatch, marker):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "mycelium-demo-run").mkdir()
    (tmp_path / "mycelium-demo-run" / marker).touch()
    with pytest.raises(RuntimeError, match="benchmark_blocked"):
        bench.run(None)


def test_snapshot_requires_exact_cached_revision_and_both_files(tmp_path):
    snapshot = tmp_path / bench.REVISION
    snapshot.mkdir()
    (snapshot / "config.json").touch()
    with pytest.raises(RuntimeError, match="pinned_model_not_cached"):
        bench.snapshot_path(str(snapshot))
    (snapshot / "model.safetensors").touch()
    assert bench.snapshot_path(str(snapshot)) == snapshot
    wrong_revision = tmp_path / "other"
    snapshot.rename(wrong_revision)
    with pytest.raises(RuntimeError, match="pinned_model_not_cached"):
        bench.snapshot_path(str(wrong_revision))


def test_percentiles_are_per_token_nearest_rank_not_window_means():
    assert bench.stats([10., 1., 9., 2., 8., 3., 7., 4., 6., 5.]) == {
        "count": 10, "min_ms": 1., "median_ms": 5.5, "p90_ms": 9., "max_ms": 10.,
    }
