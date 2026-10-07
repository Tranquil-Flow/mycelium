"""A7: a stage keeps the KV it releases on the terminal hop until the request's terminal release.

A covered source lost while the final decode step is outstanding leaves the
surviving stage having already executed the terminal hop.  Without retention
that stage has released its KV, the fenced rekey refuses
``kv_checkpoint_state_missing`` and the cutover is reported
``compatibility_incompatible``.  With retention (enabled only on A7-wired
nodes) the rekey continues bit-identically, and the retained KV is bounded:
released on the terminal release / cancellation / A6 fallback, on expiry, on
eviction and on close.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest

from mycelium_a7_node_agent import A7NodeAgent
from mycelium_router.contracts import KVShadowRelease
from mycelium_router.kv_checkpoint import KVCheckpointError
from mycelium_router.payloads import encode_token_ids
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _work_item

from tests.a7_acceptance.test_kv_checkpoint_transfer import _MAX, _fresh_ports

_LEASE = 1_000_000_000_000.0
_PROMPT = (1, 7, 11, 5)


class _Clock:
   def __init__(self) -> None:
      self.now = 1_000.0

   def __call__(self) -> float:
      return self.now


def _stage(port, case, stage_index, payload, *, phase, request_id, path_id, path_attempt, token_index, position, terminal=False):
   item = replace(
      _work_item(
         case,
         stage_index,
         payload,
         phase=phase,
         token_span=len(_PROMPT) if phase == "PREFILL" else 1,
         request_id=request_id,
         path_id=path_id,
         token_index=token_index,
         position=position,
         terminal=terminal,
         lease_expires_at=_LEASE,
      ),
      path_attempt=path_attempt,
   )
   return port.execute(item)


def _through(ports, case, tokens, **kwargs):
   payload = encode_token_ids(tokens)
   result = None
   for stage_index, port in enumerate(ports):
      result = _stage(port, case, stage_index, payload, **kwargs)
      assert result.success, result.failure_reason
      if result.payload is not None:
         payload = result.payload
   return result.token_id


def _until_final_step(case, request_id, path_id):
   """Prefill + one decode on both stages: two tokens committed of a three-token request."""

   first = _through(case.ports, case, _PROMPT, phase="PREFILL", request_id=request_id, path_id=path_id, path_attempt=0, token_index=-1, position=0)
   second = _through(case.ports, case, (first,), phase="DECODE", request_id=request_id, path_id=path_id, path_attempt=0, token_index=1, position=len(_PROMPT))
   return first, second


def _final_step_on_surviving_stage(case, request_id, path_id, second):
   """The final (terminal) decode executes on stage 0; stage 1 (the source) is lost before it."""

   result = _stage(
      case.ports[0], case, 0, encode_token_ids((second,)),
      phase="DECODE", request_id=request_id, path_id=path_id, path_attempt=0,
      token_index=2, position=len(_PROMPT) + 1, terminal=True,
   )
   assert result.success, result.failure_reason


def _enable(*ports, seconds=30.0, max_entries=64):
   for port in ports:
      port.enable_terminal_kv_retention(seconds=seconds, max_entries=max_entries)


def _retention(port):
   return port.kv_snapshot()["terminal_kv_retention"]


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_final_step_source_loss_rekeys_the_retained_surviving_kv_bit_identically(tmp_path: Path, architecture: str) -> None:
   case = _case(tmp_path, architecture)
   _enable(*case.ports)
   request_id, old_path = f"request:{architecture}:final", "path:old"
   first, second = _until_final_step(case, request_id, old_path)
   # The standby's shadow of the source stage: everything committed before the final step.
   shadow = case.ports[1].export_kv_checkpoint(old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)

   surviving = case.ports[0]
   snapshot = surviving.kv_snapshot()
   # Released for every other purpose exactly as before ...
   assert snapshot["active_state_count"] == 0
   assert snapshot["release_counts"] == {"normal_completion": 1}
   assert snapshot["terminal_kv_retention"]["retained_count"] == 1
   # ... and the KV is still held, so exact-path cleanup proof reports it.
   assert surviving.kv_subject_clean(request_id, old_path, 0) is False

   receipt = surviving.rekey_kv_path(
      old_path,
      new_path_id="path:successor",
      new_path_attempt=1,
      lease_expires_at=_LEASE,
      truncate_to_position=len(_PROMPT) + 1,
   )
   assert receipt["next_position"] == len(_PROMPT) + 1
   assert _retention(surviving)["retained_count"] == 0
   assert _retention(surviving)["reclaimed_by_rekey_total"] == 1
   standby = _fresh_ports(case)[1]
   standby.import_kv_checkpoint(
      shadow,
      request_id=request_id,
      path_id="path:successor",
      path_attempt=1,
      placement_id=case.graph.stages[1].placements[0].placement_id,
      lease_expires_at=_LEASE,
      expected_next_position=len(_PROMPT) + 1,
      max_bytes=_MAX,
   )
   resumed = _through(
      (surviving, standby), case, (second,), phase="DECODE", request_id=request_id,
      path_id="path:successor", path_attempt=1, token_index=2, position=len(_PROMPT) + 1, terminal=True,
   )
   assert resumed == _reference(case, _PROMPT + (first, second))


def test_without_retention_the_terminal_hop_releases_immediately(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   request_id, old_path = "request:off", "path:old"
   _first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   snapshot = case.ports[0].kv_snapshot()
   assert "terminal_kv_retention" not in snapshot
   assert case.ports[0].kv_subject_clean(request_id, old_path, 0) is True
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1)
   assert refused.value.code == "kv_checkpoint_state_missing"


def test_retained_path_accepts_no_further_work_and_export_stays_refused(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports)
   request_id, old_path = "request:fenced", "path:old"
   _first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   later = _stage(
      case.ports[0], case, 0, encode_token_ids((second,)),
      phase="DECODE", request_id=request_id, path_id=old_path, path_attempt=0,
      token_index=3, position=len(_PROMPT) + 2,
   )
   assert later.success is False and later.failure_reason == "kv_state_released"
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].export_kv_checkpoint(old_path)
   assert refused.value.code == "kv_checkpoint_state_missing"


def test_a_refused_rekey_returns_the_retained_kv_unchanged(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports)
   request_id, old_path = "request:refused", "path:old"
   first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   port = case.ports[0]
   with pytest.raises(KVCheckpointError) as refused:
      port.rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) - 1)
   assert refused.value.code == "kv_checkpoint_position_mismatch"
   assert port.kv_snapshot()["active_state_count"] == 0
   assert _retention(port)["retained_count"] == 1
   port.rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1)
   assert port.kv_snapshot()["states"]["path:successor"]["next_position"] == len(_PROMPT) + 1


@pytest.mark.parametrize("release", ["cancel", "a7_terminal_release", "close"])
def test_retained_kv_is_released_by_the_terminal_release_cancel_and_close(tmp_path: Path, release: str) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports)
   request_id, old_path = "request:release", "path:old"
   _first, second = _until_final_step(case, request_id, old_path)
   # Normal completion: both stages execute the terminal hop.
   _through(case.ports, case, (second,), phase="DECODE", request_id=request_id, path_id=old_path, path_attempt=0, token_index=2, position=len(_PROMPT) + 1, terminal=True)
   assert all(_retention(port)["retained_count"] == 1 for port in case.ports)
   assert all(port.kv_snapshot()["release_counts"] == {"normal_completion": 1} for port in case.ports)

   if release == "cancel":
      for port in case.ports:
         port.cancel(old_path)
      reason = "cancelled"  # the numpy port's cancellation release reason
   elif release == "a7_terminal_release":
      # The entry's terminal KVShadowRelease reaches each participant's agent.
      for index, port in enumerate(case.ports):
         agent = A7NodeAgent(f"node-{index}", send=lambda *_args: None)
         agent.register_port(case.graph.stages[index].placements[0].placement_id, port)
         assert agent.receive_release(
            KVShadowRelease(request_id=request_id, path_id=old_path, path_attempt=0, reason="completed"),
            source_node_id="entry",
         )
      reason = "cancelled"
   else:
      for port in case.ports:
         port.close()
      reason = "worker_shutdown"

   for port in case.ports:
      assert port.kv_subject_clean(request_id, old_path, 0) is True
      retention = port._terminal_retention
      assert len(retention) == 0
      assert dict(retention.release_counts) == {reason: 1}
      if release != "close":
         assert _retention(port)["retained_kv_bytes"] == 0
         # The normal-completion accounting is unchanged by the later release.
         assert port.kv_snapshot()["release_counts"] == {"normal_completion": 1}


def test_retained_kv_expires_and_is_bounded(tmp_path: Path) -> None:
   clock = _Clock()
   case = _case(tmp_path, "qwen2", clock=clock)
   _enable(*case.ports, seconds=5.0, max_entries=3)
   for index in range(5):
      request_id, path_id = f"request:{index}", f"path:{index}"
      _first, second = _until_final_step(case, request_id, path_id)
      _through(case.ports, case, (second,), phase="DECODE", request_id=request_id, path_id=path_id, path_attempt=0, token_index=2, position=len(_PROMPT) + 1, terminal=True)
      assert all(len(port._terminal_retention) <= 3 for port in case.ports)
   for port in case.ports:
      retention = _retention(port)
      assert retention["retained_count"] == 3
      assert retention["release_counts"] == {"evicted": 2}
      # The two oldest were evicted: their KV is gone and their subjects are clean.
      assert port.kv_subject_clean("request:0", "path:0", 0) is True
      assert port.kv_subject_clean("request:4", "path:4", 0) is False
   clock.now += 5.0
   for port in case.ports:
      retention = _retention(port)  # kv_snapshot sweeps expiry
      assert retention["retained_count"] == 0
      assert retention["retained_kv_bytes"] == 0
      assert retention["release_counts"] == {"evicted": 2, "expired": 3}
      assert all(port.kv_subject_clean(f"request:{index}", f"path:{index}", 0) for index in range(5))
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path("path:4", new_path_id="path:late", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1)
   assert refused.value.code == "kv_checkpoint_state_missing"


def test_a_cancelled_retained_path_is_never_rekeyed(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports)
   request_id, old_path = "request:cancelled", "path:old"
   _first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   case.ports[0].cancel(old_path)
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1)
   assert refused.value.code == "kv_checkpoint_state_missing"
   assert _retention(case.ports[0])["retained_count"] == 0


def test_no_retained_kv_leaks_under_concurrent_load(tmp_path: Path) -> None:
   """Many concurrent requests complete; each request's terminal release frees its KV."""

   case = _case(tmp_path, "qwen2")
   _enable(*case.ports, seconds=30.0, max_entries=8)
   requests = 24
   failures: list[str] = []
   peak = [0]
   lock = threading.Lock()

   def serve(index: int) -> None:
      request_id, path_id = f"request:load:{index}", f"path:load:{index}"
      try:
         _first, second = _until_final_step(case, request_id, path_id)
         _through(case.ports, case, (second,), phase="DECODE", request_id=request_id, path_id=path_id, path_attempt=0, token_index=2, position=len(_PROMPT) + 1, terminal=True)
         with lock:
            peak[0] = max(peak[0], *(len(port._terminal_retention) for port in case.ports))
         # The entry's terminal release (every third one lands only after a
         # later request completed, as under load).
         if index % 3:
            for port in case.ports:
               port.cancel(path_id)
      except Exception as exc:  # pragma: no cover - reported below
         failures.append(repr(exc))

   threads = [threading.Thread(target=serve, args=(index,)) for index in range(requests)]
   for thread in threads:
      thread.start()
   for thread in threads:
      thread.join(timeout=60)
   assert failures == []
   assert peak[0] <= 8
   for index in range(0, requests, 3):
      for port in case.ports:
         port.cancel(f"path:load:{index}")
   for port in case.ports:
      snapshot = port.kv_snapshot()
      assert snapshot["active_state_count"] == 0
      assert snapshot["terminal_kv_retention"]["retained_count"] == 0
      assert snapshot["terminal_kv_retention"]["retained_kv_bytes"] == 0
      assert snapshot["release_counts"] == {"normal_completion": requests}
      assert all(port.kv_subject_clean(f"request:load:{index}", f"path:load:{index}", 0) for index in range(requests))
      assert port._kv_subjects == {}


def test_retention_settings_are_validated(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   for seconds, entries in ((0.0, 4), (float("inf"), 4), (5.0, 0), (True, 4)):
      with pytest.raises(ValueError):
         case.ports[0].enable_terminal_kv_retention(seconds=seconds, max_entries=entries)
