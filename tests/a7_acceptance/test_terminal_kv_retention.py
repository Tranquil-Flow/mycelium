# SPDX-License-Identifier: AGPL-3.0-only
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
from mycelium_router.contracts import KVShadowRelease, KVSuccessorCutover, RouterConfig
from mycelium_router.kv_checkpoint import KVCheckpointError
from mycelium_router.payloads import encode_token_ids
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _work_item

from tests.a7_acceptance.test_kv_checkpoint_transfer import _MAX, _fresh_ports

_LEASE = 1_000_000_000_000.0
_PROMPT = (1, 7, 11, 5)
#: A stage runtime's default KV lease renewal window (the base reservation lease).
_RENEWAL_SECONDS = RouterConfig().reservation_lease_seconds


class _Clock:
   def __init__(self) -> None:
      self.now = 1_000.0

   def __call__(self) -> float:
      return self.now


def _stage(port, case, stage_index, payload, *, phase, request_id, path_id, path_attempt, token_index, position, terminal=False, lease=_LEASE):
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
         lease_expires_at=lease,
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


def _auth(case, request_id, stage_index=0):
   """What an authenticated A7 cutover from the path's entry passes for a surviving stage."""

   return {
      "request_id": request_id,
      "placement_id": case.graph.stages[stage_index].placements[0].placement_id,
      "allow_terminal_retained": True,
   }


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
      **_auth(case, request_id),
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
      case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, **_auth(case, request_id))
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
      port.rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) - 1, **_auth(case, request_id))
   assert refused.value.code == "kv_checkpoint_position_mismatch"
   assert port.kv_snapshot()["active_state_count"] == 0
   assert _retention(port)["retained_count"] == 1
   port.rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, **_auth(case, request_id))
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
      case.ports[0].rekey_kv_path("path:4", new_path_id="path:late", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, **_auth(case, "request:4"))
   assert refused.value.code == "kv_checkpoint_state_missing"


def test_a_cancelled_retained_path_is_never_rekeyed(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports)
   request_id, old_path = "request:cancelled", "path:old"
   _first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   case.ports[0].cancel(old_path)
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, **_auth(case, request_id))
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


# -- authority and lifecycle of the retained KV (security hardening) -------------------

_ENTRY = "node-entry"
_STAGE0 = 0


class _Receipts:
   def __init__(self) -> None:
      self.sent: list = []

   def __call__(self, destination, message, payload) -> None:
      self.sent.append(message)


def _surviving_agent(case, *, port=None):
   receipts = _Receipts()
   agent = A7NodeAgent("node-surviving", send=receipts)
   agent.register_port(case.graph.stages[_STAGE0].placements[0].placement_id, port or case.ports[0])
   return agent, receipts


def _cutover(case, request_id, old_path, *, successor="path:successor", placement_index=_STAGE0):
   return KVSuccessorCutover(
      request_id=request_id,
      old_path_id=old_path,
      old_path_attempt=0,
      successor_path_id=successor,
      successor_path_attempt=1,
      fencing_generation=2,
      placement_id=case.graph.stages[placement_index].placements[0].placement_id,
      role="surviving_rekey",
      expected_next_position=len(_PROMPT) + 1,
      lease_expires_at=_LEASE,
   )


def _retained_case(tmp_path, request_id="request:sec", old_path="path:old", clock=None):
   case = _case(tmp_path, "qwen2", clock=clock) if clock is not None else _case(tmp_path, "qwen2")
   _enable(*case.ports)
   _first, second = _until_final_step(case, request_id, old_path)
   _final_step_on_surviving_stage(case, request_id, old_path, second)
   assert _retention(case.ports[0])["retained_count"] == 1
   return case


@pytest.mark.parametrize(
   "kwargs",
   [
      pytest.param({}, id="unauthenticated_default"),
      pytest.param({"request_id": "request:other", "allow_terminal_retained": True}, id="other_request"),
      pytest.param({"placement_id": "placement:other", "allow_terminal_retained": True}, id="other_placement"),
      pytest.param({"allow_terminal_retained": False}, id="not_authorised"),
   ],
)
def test_retained_kv_is_taken_only_by_an_authorised_rekey_for_the_same_request_and_placement(tmp_path: Path, kwargs) -> None:
   case = _retained_case(tmp_path)
   rekey = {**_auth(case, "request:sec"), **kwargs} if kwargs else {}
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path("path:old", new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, **rekey)
   assert refused.value.code == "kv_checkpoint_state_missing"
   # A refused take leaves the retained entry exactly where it was.
   assert _retention(case.ports[0])["retained_count"] == 1
   assert case.ports[0].kv_snapshot()["active_state_count"] == 0


def test_an_active_rekey_naming_another_request_is_refused(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _until_final_step(case, "request:live", "path:live")
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path("path:live", new_path_id="path:successor", new_path_attempt=1, lease_expires_at=_LEASE, truncate_to_position=len(_PROMPT) + 1, request_id="request:other")
   assert refused.value.code == "kv_checkpoint_subject_mismatch"
   assert set(case.ports[0].kv_snapshot()["states"]) == {"path:live"}


@pytest.mark.parametrize(
   ("path_entry", "sender", "reason"),
   [
      pytest.param("node-entry", "node-intruder", "a7_cutover_sender_not_entry", id="sender_is_not_the_path_entry"),
      pytest.param(None, "node-entry", "kv_checkpoint_state_missing", id="path_entry_unknown"),
   ],
)
def test_a_cutover_not_from_the_paths_entry_never_revives_retained_kv(tmp_path: Path, path_entry, sender, reason) -> None:
   case = _retained_case(tmp_path)
   agent, receipts = _surviving_agent(case)
   assert agent.receive_cutover(_cutover(case, "request:sec", "path:old"), source_node_id=sender, path_entry_node_id=path_entry)
   receipt = receipts.sent[-1]
   assert receipt.installed is False and receipt.reason == reason
   assert case.ports[0].kv_snapshot()["active_state_count"] == 0
   assert _retention(case.ports[0])["retained_count"] == 1


def test_the_paths_entry_cutover_takes_the_retained_kv(tmp_path: Path) -> None:
   case = _retained_case(tmp_path)
   agent, receipts = _surviving_agent(case)
   agent.receive_cutover(_cutover(case, "request:sec", "path:old"), source_node_id=_ENTRY, path_entry_node_id=_ENTRY)
   assert receipts.sent[-1].installed is True, receipts.sent[-1].reason
   assert set(case.ports[0].kv_snapshot()["states"]) == {"path:successor"}


def test_no_cutover_after_the_requests_terminal_release(tmp_path: Path) -> None:
   case = _retained_case(tmp_path)
   agent, receipts = _surviving_agent(case)
   assert agent.receive_release(KVShadowRelease(request_id="request:sec", path_id="path:old", path_attempt=0, reason="completed"), source_node_id=_ENTRY)
   agent.receive_cutover(_cutover(case, "request:sec", "path:old"), source_node_id=_ENTRY, path_entry_node_id=_ENTRY)
   assert receipts.sent[-1].installed is False
   assert receipts.sent[-1].reason == "a7_cutover_request_released"
   snapshot = case.ports[0].kv_snapshot()
   assert snapshot["active_state_count"] == 0 and snapshot["terminal_kv_retention"]["retained_count"] == 0


def test_a_terminal_release_naming_the_old_path_frees_a_successor_a_stale_cutover_installed(tmp_path: Path) -> None:
   case = _retained_case(tmp_path)
   agent, receipts = _surviving_agent(case)
   agent.receive_cutover(_cutover(case, "request:sec", "path:old"), source_node_id=_ENTRY, path_entry_node_id=_ENTRY)
   assert receipts.sent[-1].installed is True
   # A post-cutover "fenced" release retires only the old attempt ...
   agent.receive_release(KVShadowRelease(request_id="request:sec", path_id="path:old", path_attempt=0, reason="fenced"), source_node_id=_ENTRY)
   assert set(case.ports[0].kv_snapshot()["states"]) == {"path:successor"}
   # ... while the terminal release frees the successor even when it names the old path.
   agent.receive_release(KVShadowRelease(request_id="request:sec", path_id="path:old", path_attempt=0, reason="completed"), source_node_id=_ENTRY)
   assert case.ports[0].kv_snapshot()["active_state_count"] == 0
   assert case.ports[0].kv_subject_clean("request:sec", "path:successor", 1) is True


def test_the_retained_entrys_own_lease_is_enforced_at_take_time(tmp_path: Path) -> None:
   clock = _Clock()
   case = _case(tmp_path, "qwen2", clock=clock)
   # The KV's own deadline is its issued lease renewed by its last applied
   # continuation (``mycelium_router.leases``): here the terminal decode step,
   # so ``started + _RENEWAL_SECONDS``.  Retention is longer,
   # so only the entry's own deadline can refuse the take below.
   _enable(*case.ports, seconds=2 * _RENEWAL_SECONDS)
   request_id, old_path = "request:lease", "path:old"
   started = clock.now
   lease = clock.now + 5.0
   prompt_payload = encode_token_ids(_PROMPT)
   payload = prompt_payload
   tokens = []
   for stage_index, port in enumerate(case.ports):
      result = _stage(port, case, stage_index, payload, phase="PREFILL", request_id=request_id, path_id=old_path, path_attempt=0, token_index=-1, position=0, lease=lease)
      assert result.success, result.failure_reason
      payload = result.payload if result.payload is not None else payload
      tokens.append(result.token_id)
   result = _stage(case.ports[0], case, 0, encode_token_ids((tokens[-1],)), phase="DECODE", request_id=request_id, path_id=old_path, path_attempt=0, token_index=1, position=len(_PROMPT), terminal=True, lease=lease)
   assert result.success, result.failure_reason
   assert _retention(case.ports[0])["retained_count"] == 1
   clock.now = started + _RENEWAL_SECONDS  # within retention, past the KV's own deadline
   with pytest.raises(KVCheckpointError) as refused:
      case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=clock.now + 60.0, truncate_to_position=len(_PROMPT), **_auth(case, request_id))
   assert refused.value.code == "kv_checkpoint_state_missing"
   assert len(case.ports[0]._terminal_retention) == 0
   assert case.ports[0].kv_subject_clean(request_id, old_path, 0) is True


def test_a_request_that_progressed_past_its_issued_lease_still_retains_its_terminal_kv(
   tmp_path: Path,
) -> None:
   """A slow but progressing request keeps its KV through the terminal step.

   Its issued lease passed while decode steps kept being applied (each renews
   the KV's own deadline), so the terminal hop's KV is retained for the A7
   rekey instead of being discarded as already expired.
   """

   clock = _Clock()
   case = _case(tmp_path, "qwen2", clock=clock)
   _enable(*case.ports, seconds=30.0)
   request_id, old_path = "request:slow", "path:old"
   lease = clock.now + 5.0
   payload = encode_token_ids(_PROMPT)
   tokens = []
   for stage_index, port in enumerate(case.ports):
      result = _stage(port, case, stage_index, payload, phase="PREFILL", request_id=request_id, path_id=old_path, path_attempt=0, token_index=-1, position=0, lease=lease)
      assert result.success, result.failure_reason
      payload = result.payload if result.payload is not None else payload
      tokens.append(result.token_id)
   clock.now += 4.0  # one slow decode round, still inside the issued lease
   result = _stage(case.ports[0], case, 0, encode_token_ids((tokens[-1],)), phase="DECODE", request_id=request_id, path_id=old_path, path_attempt=0, token_index=1, position=len(_PROMPT), lease=lease)
   assert result.success, result.failure_reason
   clock.now += 4.0  # the terminal step arrives after the issued lease
   result = _stage(case.ports[0], case, 0, encode_token_ids((3,)), phase="DECODE", request_id=request_id, path_id=old_path, path_attempt=0, token_index=2, position=len(_PROMPT) + 1, terminal=True, lease=lease)
   assert result.success, result.failure_reason
   assert clock.now > lease
   assert _retention(case.ports[0])["retained_count"] == 1
   clock.now += 1.0
   receipt = case.ports[0].rekey_kv_path(old_path, new_path_id="path:successor", new_path_attempt=1, lease_expires_at=clock.now + 60.0, truncate_to_position=len(_PROMPT) + 1, **_auth(case, request_id))
   assert receipt
   assert set(case.ports[0].kv_snapshot()["states"]) == {"path:successor"}


def test_retained_kv_is_never_read_by_another_path_and_its_subject_is_scoped(tmp_path: Path) -> None:
   case = _retained_case(tmp_path)
   port = case.ports[0]
   with pytest.raises(KVCheckpointError):
      port.export_kv_checkpoint("path:old")
   # A frame for another path (even of the same request) never sees it.
   other = _stage(port, case, 0, encode_token_ids((3,)), phase="DECODE", request_id="request:sec", path_id="path:other", path_attempt=1, token_index=2, position=len(_PROMPT) + 1)
   assert other.success is False
   assert port.kv_snapshot()["active_state_count"] == 0
   # Only the retained path's own subject is reported held.
   assert port.kv_subject_clean("request:sec", "path:old", 0) is False
   assert port.kv_subject_clean("request:sec", "path:other", 1) is True
   assert port.kv_subject_clean("request:x", "path:x", 0) is True
   assert set(port._kv_subjects) == {"path:old"}


def test_release_racing_a_cutover_never_leaves_kv_behind(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   _enable(*case.ports, max_entries=64)
   port = case.ports[0]
   outcomes = {"installed": 0, "refused": 0}
   for index in range(30):
      request_id, old_path = f"request:race:{index}", f"path:race:{index}"
      _first, second = _until_final_step(case, request_id, old_path)
      _final_step_on_surviving_stage(case, request_id, old_path, second)
      agent, receipts = _surviving_agent(case)
      start = threading.Barrier(2)

      def release() -> None:
         start.wait()
         agent.receive_release(KVShadowRelease(request_id=request_id, path_id=old_path, path_attempt=0, reason="completed"), source_node_id=_ENTRY)

      def cutover() -> None:
         start.wait()
         agent.receive_cutover(_cutover(case, request_id, old_path, successor=f"path:race:{index}:s"), source_node_id=_ENTRY, path_entry_node_id=_ENTRY)

      threads = [threading.Thread(target=release), threading.Thread(target=cutover)]
      for thread in threads:
         thread.start()
      for thread in threads:
         thread.join(timeout=30)
      outcomes["installed" if receipts.sent[-1].installed else "refused"] += 1
      # Cancellation may finish on a deferred worker; wait for it (bounded).
      with port._cancellation_condition:
         port._cancellation_condition.wait_for(lambda: not port._deferred_cancellation_cleanup, timeout=5)
      snapshot = port.kv_snapshot()
      assert snapshot["active_state_count"] == 0, (index, snapshot["states"])
      assert snapshot["terminal_kv_retention"]["retained_count"] == 0
      assert port.kv_subject_clean(request_id, old_path, 0) is True
      assert port.kv_subject_clean(request_id, f"path:race:{index}:s", 1) is True
      # The other stage completed normally; its own release is the entry's too.
      case.ports[1].cancel(old_path)
   assert port._kv_subjects == {}
