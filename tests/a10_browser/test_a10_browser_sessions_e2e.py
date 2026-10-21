# SPDX-License-Identifier: AGPL-3.0-only
"""A10 gate 1 browser harness, end to end on a loopback local-conformance origin.

Drives the real harness (``ui/web/scripts/a10-browser-sessions.mjs``) with a real
headless Chromium, through the gate's own ``BrowserSessionsPhase``, against
``conformance_origin.ConformanceOrigin``: the BUILT production SPA and the real
product gateway over a simulated request gateway.  This proves the harness and
its gate wiring work without devices; it can never produce gate evidence (the
origin says ``simulated: true`` and the phase records
``local_conformance_is_not_gate_evidence``).

Needs ``ui/web/dist`` (``npm run build``), ``ui/web/node_modules`` and the pinned
Playwright Chromium; the module skips with the missing prerequisite named.
One browser at a time, headless.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
   sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conformance_origin import ConformanceOrigin  # noqa: E402
from scripts import run_a10_physical_gate as gate  # noqa: E402

NODE = shutil.which("node") or str(Path.home() / ".nvm/versions/node/v22.22.2/bin/node")
_MISSING = [
   name
   for name, present in (
      ("ui/web/dist", (ROOT / "ui/web/dist/index.html").is_file()),
      ("ui/web/node_modules", (ROOT / "ui/web/node_modules/@playwright/test").is_dir()),
      ("node", Path(NODE).is_file()),
   )
   if not present
]
pytestmark = pytest.mark.skipif(bool(_MISSING), reason=f"prerequisite missing: {', '.join(_MISSING)}")
#: One engine per run (default chromium); A10_BROWSER_E2E_ENGINE=firefox|webkit for the others.
ENGINE = os.environ.get("A10_BROWSER_E2E_ENGINE", "chromium")


def _live_status(origin: str):
   def read():
      with urllib.request.urlopen(f"{origin}/__mycelium/live-status", timeout=10) as response:
         return json.load(response)

   return read


@pytest.fixture(scope="module")
def origin():
   with ConformanceOrigin() as started:
      yield started


def test_two_clean_sessions_submit_the_frozen_workload_through_the_ui(origin, tmp_path):
   phase = gate.BrowserSessionsPhase(
      base_url=origin.origin, out=tmp_path, live_status=_live_status(origin.origin),
      engine=ENGINE, node=NODE, mode="local_conformance", timeout_s=600,
   )
   preflight = phase.preflight()
   assert preflight["engine_executable_present"] is True
   assert preflight["origin_contact_attempted"] is False

   evidence = phase()

   assert evidence["blockers"] == ["local_conformance_is_not_gate_evidence"], evidence
   assert evidence["checks_passed"] is True
   assert evidence["passed"] is False
   assert evidence["runtime"]["executed_on_route"] == 4
   assert evidence["runtime"]["in_true_runtime_batch"] >= 2
   assert evidence["runtime"]["browser_pair_in_one_batch"] is True
   assert [item["round"] for item in evidence["browser"]["requests"]] == [
      "concurrent", "concurrent", "late_arrival", "late_arrival",
   ]
   assert all(item["terminal"] == "completed" for item in evidence["browser"]["requests"])
   assert isinstance(evidence["browser"]["concurrent_acceptance_skew_ms"], int)
   assert evidence["browser"]["leader_tokens_before_late_submit"] >= 1
   report_path = tmp_path / "browser-sessions" / ENGINE / "browser-report.json"
   report = json.loads(report_path.read_text("utf-8"))
   assert stat.S_IMODE(report_path.stat().st_mode) == 0o600
   assert report["checks"] == list(gate.REQUIRED_BROWSER_CHECKS)
   assert report["live_evidence"] is False and report["mode"] == "local_conformance"
   # The decoder admitted exactly the four frozen requests the UI submitted.
   assert origin.decoder.counter >= 4
   text = json.dumps(report) + json.dumps(evidence)
   for round_ in gate.browser_workload()["rounds"]:
      for item in round_["submissions"]:
         assert item["prompt"] not in text
   assert "a10-local-" not in text  # raw request IDs never leave the harness


def test_live_mode_refuses_a_simulated_origin_before_any_browser_session(origin, tmp_path):
   phase = gate.BrowserSessionsPhase(
      base_url=origin.origin, out=tmp_path, live_status=_live_status(origin.origin),
      engine=ENGINE, node=NODE, mode="live", timeout_s=120,
   )
   phase.preflight()
   admitted = origin.decoder.counter
   evidence = phase()
   assert evidence["passed"] is False
   assert "browser_report_not_passed" in evidence["blockers"]
   assert "harness:physical_route_required" in evidence["blockers"]
   assert origin.decoder.counter == admitted
   report = json.loads((tmp_path / "browser-sessions" / ENGINE / "browser-report.json").read_text("utf-8"))
   assert report["checks"] == [] and report["requests"] == []
