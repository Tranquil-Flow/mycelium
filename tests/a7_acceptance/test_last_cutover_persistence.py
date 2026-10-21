# SPDX-License-Identifier: AGPL-3.0-only
"""A7: the entry's last cutover record survives a route cold restart.

With no A6 authority the entry aborts the covered request synchronously and
the route cold-restarts, retiring the entry process and its in-memory cutover
record before the evidence door is read.  The record is therefore kept on disk
beside the entry's fence ledger, and a restarted entry (a new controller over
the same fence directory) serves it for exactly that request and node, and
for nothing else.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from mycelium_a7_entry_controller import CUTOVER_RECORD_PROTOCOL, LAST_CUTOVER_SOURCE, A7EntryController
from mycelium_a7_fence_store import (
   LAST_CUTOVER_DIRECTORY,
   LAST_CUTOVER_FILE,
   LAST_CUTOVER_MAX_BYTES,
   DurableFenceStore,
)
from tests.a7_acceptance.test_entry_cutover import _Harness

_REQUEST = "request:a7-abort-restart"


def _restarted(harness: _Harness, tmp_path: Path, *, node_id: str = "node-a") -> A7EntryController:
   """A new entry process: fresh controller and fence store over the same directory."""

   old = harness.controller
   return A7EntryController(
      node_id=node_id,
      agent=harness.agent_a,
      send=harness.net.sender("node-a"),
      fence_store=DurableFenceStore(tmp_path / "fences"),
      coverage=list(old._coverage.values()),
      a6_available=False,
   )


def _aborted(tmp_path: Path) -> tuple[_Harness, dict]:
   harness = _Harness(tmp_path, qualification_ttl_ms=10, a6_available=False)
   harness.run(_REQUEST, kill_after=2)
   assert harness.entry.request_status(_REQUEST) == "FAILED"
   live = harness.controller.snapshot(_REQUEST)
   assert live is not None and live["result"]["outcome"] == "aborted"
   return harness, live


def test_restarted_entry_serves_the_persisted_record_for_the_same_request_only(tmp_path: Path) -> None:
   harness, live = _aborted(tmp_path)
   restarted = _restarted(harness, tmp_path)
   del harness.controller  # the old entry process is retired with its memory

   served = restarted.snapshot(_REQUEST)
   assert served is not None
   assert served["restored_from"] == LAST_CUTOVER_SOURCE
   assert {key: value for key, value in served.items() if key != "restored_from"} == live
   assert served["result"]["protocol"] == CUTOVER_RECORD_PROTOCOL
   assert served["result"]["outcome"] == "aborted"
   assert served["result"]["terminal_released"] is True
   # Fail-closed: never served for another request, never invented.
   assert restarted.snapshot("request:another") is None
   # The record is not a fence record and does not hold the fence ledger.
   assert restarted.fence_record_count() == 0


def test_persisted_record_is_owner_only_bounded_and_request_digest_bound(tmp_path: Path) -> None:
   harness, _live = _aborted(tmp_path)
   path = tmp_path / "fences" / LAST_CUTOVER_DIRECTORY / LAST_CUTOVER_FILE
   info = os.lstat(path)
   assert info.st_mode & 0o777 == 0o600
   assert 0 < info.st_size <= LAST_CUTOVER_MAX_BYTES
   raw = path.read_text("utf-8")
   assert _REQUEST not in raw  # only the request digest is written
   assert not list(path.parent.glob("*.tmp"))


def test_persisted_record_is_refused_for_another_node_or_when_tampered(tmp_path: Path) -> None:
   harness, _live = _aborted(tmp_path)
   assert _restarted(harness, tmp_path, node_id="node-other").snapshot(_REQUEST) is None

   path = tmp_path / "fences" / LAST_CUTOVER_DIRECTORY / LAST_CUTOVER_FILE
   record = json.loads(path.read_bytes())
   record["snapshot"]["result"]["outcome"] = "fenced_kv_successor"
   path.write_bytes(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
   assert _restarted(harness, tmp_path).snapshot(_REQUEST) is None

   path.unlink()
   path.write_bytes(b"{}")
   os.chmod(path, 0o644)
   assert _restarted(harness, tmp_path).snapshot(_REQUEST) is None


def test_a_later_cutover_replaces_the_record(tmp_path: Path) -> None:
   harness, _live = _aborted(tmp_path)
   harness.source_port.killed = False
   harness.run("request:a7-abort-later", kill_after=2)
   assert harness.controller.snapshot("request:a7-abort-later")["result"]["outcome"] == "aborted"
   restarted = _restarted(harness, tmp_path)
   assert restarted.snapshot("request:a7-abort-later")["result"]["outcome"] == "aborted"
   assert restarted.snapshot(_REQUEST) is None


def test_a_request_without_a_cutover_decision_writes_no_record(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   harness.run("request:a7-clean-restart", kill_after=None)
   assert not (tmp_path / "fences" / LAST_CUTOVER_DIRECTORY / LAST_CUTOVER_FILE).exists()
   assert _restarted(harness, tmp_path).snapshot("request:a7-clean-restart") is None


def test_a_successful_cutover_keeps_its_terminal_record(tmp_path: Path) -> None:
   harness = _Harness(tmp_path)
   harness.run("request:a7-cutover-restart", kill_after=3)
   assert harness.entry.request_status("request:a7-cutover-restart") == "COMPLETED"
   live = harness.controller.snapshot("request:a7-cutover-restart")
   # The terminal release writes synchronously; an earlier background write
   # of the cutover decision can never replace it.
   for thread in [item for item in threading.enumerate() if item.name == "a7-last-cutover"]:
      thread.join(timeout=5)
   served = _restarted(harness, tmp_path).snapshot("request:a7-cutover-restart")
   assert served is not None and served["restored_from"] == LAST_CUTOVER_SOURCE
   # As observed when written (before the terminal release dropped the watermark).
   assert served["acknowledged"] == 3 and live["acknowledged"] == -1
   assert {key: value for key, value in served.items() if key not in {"restored_from", "acknowledged"}} == {
      key: value for key, value in live.items() if key != "acknowledged"
   }
   assert served["result"]["outcome"] == "fenced_kv_successor"
   assert served["result"]["terminal_released"] is True
