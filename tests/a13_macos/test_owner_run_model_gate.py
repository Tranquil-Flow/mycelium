# SPDX-License-Identifier: AGPL-3.0-only
"""R23(6c)/R24(5): Start demo without a prepared model refuses at once, unless a download is really in progress."""
from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_demo_chain import make, wait

RUN_JS = Path(__file__).resolve().parents[2] / "mycelium_a13_macos" / "web" / "run.js"


def acquisition(app, state_box):
    source = app.acquisition.prepared_source
    app.acquisition = SimpleNamespace(status=lambda: {"state": state_box["state"]}, prepared_source=source)


def test_start_demo_without_prepared_model_refuses_immediately(tmp_path):
    app, ops, members, run = make(tmp_path, prepared=False)
    run.chain.budgets["model"] = 60.0  # the old wait would hold the run here for a minute
    started = time.monotonic()
    run.start()
    status = wait(run, lambda s: s["state"] == "failed", timeout=5)
    assert time.monotonic() - started < 5
    assert (status["failed_step"], status["error_code"]) == ("model", "owner_model_not_prepared")


def test_refusal_names_model_preparation_step_with_fix_action(tmp_path):
    app, ops, members, run = make(tmp_path, prepared=False)
    run.start()
    status = wait(run, lambda s: s["state"] == "failed", timeout=5)
    assert "Inference" in status["error_message"] and "download" in status["error_message"].lower()
    assert status["error_fix"] == {"action": "prepare_model", "target": "acquisition"}


@pytest.mark.parametrize("state", ["idle", "not_started", "failed", "cancelled", "closed"])
def test_inactive_acquisition_refuses_immediately(tmp_path, state):
    app, ops, members, run = make(tmp_path, prepared=False)
    acquisition(app, {"state": state})
    run.chain.budgets["model"] = 60.0
    run.start()
    status = wait(run, lambda s: s["state"] == "failed", timeout=5)
    assert status["error_code"] == "owner_model_not_prepared"


@pytest.mark.parametrize("state", ["starting", "resolving_metadata", "checking_capacity", "downloading", "verifying",
                                   "compiling"])
def test_start_demo_waits_when_acquisition_active_then_proceeds(tmp_path, state):
    app, ops, members, run = make(tmp_path, prepared=False)
    box = {"state": state}
    acquisition(app, box)
    run.chain.budgets["model"] = 20.0
    run.start()
    time.sleep(0.3)
    assert run.status()["state"] == "running"  # still waiting for the download, not refused
    box["state"] = "prepared"
    status = wait(run, lambda s: s["state"] in {"serving", "failed"}, timeout=10)
    assert status["state"] == "serving", status


def test_active_wait_is_still_bounded_by_the_model_budget(tmp_path):
    app, ops, members, run = make(tmp_path, prepared=False)
    acquisition(app, {"state": "downloading"})
    run.chain.budgets["model"] = 0.3
    run.start()
    status = wait(run, lambda s: s["state"] == "failed", timeout=5)
    assert status["error_code"] == "owner_model_not_prepared"


def test_download_that_fails_midway_ends_the_wait_at_once(tmp_path):
    app, ops, members, run = make(tmp_path, prepared=False)
    box = {"state": "downloading"}
    acquisition(app, box)
    run.chain.budgets["model"] = 60.0
    run.start()
    time.sleep(0.2)
    box["state"] = "failed"
    started = time.monotonic()
    status = wait(run, lambda s: s["state"] == "failed", timeout=5)
    assert time.monotonic() - started < 3 and status["error_code"] == "owner_model_not_prepared"


def test_start_demo_with_prepared_model_proceeds_unchanged(tmp_path):
    app, ops, members, run = make(tmp_path)
    run.start()
    assert wait(run, lambda s: s["state"] in {"serving", "failed"})["state"] == "serving"


def test_run_js_message_matches_code_and_points_at_the_model_step():
    script = RUN_JS.read_text()
    assert "owner_model_not_prepared" in script and "Inference" in script
    assert "error_fix" in script and "prepare_model" in script and "acquisition" in script
