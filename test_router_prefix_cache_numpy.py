"""PV20: prefix KV cache on the two-stage NumPy Qwen fixture route (hit == cold, exactly)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mycelium_router.numpy_runtime import NumpyRuntimePort
from mycelium_router.payloads import encode_token_ids
from mycelium_router.prefix_cache import (
   PrefixCacheConfig,
   PrefixPlan,
   PrefixPlanner,
   chain_digests,
   route_scope_digest,
)
from test_router_numpy_qwen_kv import _case, _reference
from test_router_numpy_runtime import _work_item

BLOCK = 16
SYSTEM = tuple((index * 7 + 3) % 31 + 1 for index in range(48))  # 3 full blocks
USER_A = (5, 9, 2)
USER_B = (11, 4, 30, 8)
LEASE = 1_000_000_000_000.0


def _config(**overrides) -> PrefixCacheConfig:
   values = {"enabled": True, "block_tokens": BLOCK, "max_bytes": 1 << 20}
   values.update(overrides)
   return PrefixCacheConfig(**values)


def _ports(case, config: PrefixCacheConfig | None):
   return tuple(
      NumpyRuntimePort(
         assignment["node_id"],
         case.graph,
         {case.graph.stages[index].placements[0].placement_id: case.loaded[index]},
         decode_mode="stage_local_kv",
         prefix_cache=config,
      )
      for index, assignment in enumerate(case.assignments)
   )


def _placement_ids(case) -> tuple[str, ...]:
   return tuple(stage.placements[0].placement_id for stage in case.graph.stages)


def _planner(case, ports, config: PrefixCacheConfig) -> PrefixPlanner:
   return PrefixPlanner(
      config,
      route_scope=route_scope_digest(case.graph),
      probes=dict(zip(_placement_ids(case), ports)),
   )


def _item(case, stage_index, payload, *, phase, request_id, path_id, token_index, position, span, plan, emits_token=False, terminal=False):
   item = _work_item(
      case,
      stage_index,
      payload,
      phase=phase,
      token_span=span,
      request_id=request_id,
      path_id=path_id,
      token_index=token_index,
      position=position,
      emits_token=emits_token,
      terminal=terminal,
      lease_expires_at=LEASE,
   )
   starts = (phase == "PREFILL") or (phase == "PREFILL_CHUNK" and token_index == 0)
   if starts:
      item = replace(item, prefix_reuse_tokens=plan.reuse_tokens, prefix_chain=plan.chain)
   return item


def _through_stages(case, ports, make_item, payload):
   result = None
   for stage_index, port in enumerate(ports):
      result = port.execute(make_item(stage_index, payload))
      assert result.success, result.failure_reason
      if result.payload is not None:
         payload = result.payload
   return result


def _generate(case, ports, prompt, *, request_id, planner=None, steps=4, chunk=0):
   """Plan, prefill only the suffix (optionally chunked), then greedy-decode ``steps`` tokens."""

   plan = (
      planner.plan(request_id, prompt, _placement_ids(case)) if planner else PrefixPlan()
   )
   path_id = f"path:{request_id}"
   suffix = prompt[plan.reuse_tokens :]
   pieces = (
      [suffix]
      if chunk <= 0
      else [suffix[start : start + chunk] for start in range(0, len(suffix), chunk)]
   )
   position = plan.reuse_tokens
   result = None
   for index, piece in enumerate(pieces):
      final = index == len(pieces) - 1
      phase = "PREFILL" if chunk <= 0 else "PREFILL_CHUNK"
      result = _through_stages(
         case,
         ports,
         lambda stage_index, payload, piece=piece, index=index, position=position, final=final, phase=phase: _item(
            case,
            stage_index,
            payload,
            phase=phase,
            request_id=request_id,
            path_id=path_id,
            token_index=-1 if phase == "PREFILL" else index,
            position=position,
            span=len(piece),
            plan=plan,
            emits_token=final and phase == "PREFILL_CHUNK",
         ),
         encode_token_ids(tuple(piece)),
      )
      position += len(piece)
   tokens = [result.token_id]
   assert tokens[0] is not None
   for token_index in range(1, steps + 1):
      token = tokens[-1]
      result = _through_stages(
         case,
         ports,
         lambda stage_index, payload, token_index=token_index: _item(
            case,
            stage_index,
            payload,
            phase="DECODE",
            request_id=request_id,
            path_id=path_id,
            token_index=token_index,
            position=len(prompt) + token_index - 1,
            span=1,
            plan=PrefixPlan(),
            terminal=token_index == steps,
         ),
         encode_token_ids((token,)),
      )
      tokens.append(result.token_id)
   return tokens, plan


def _measure_block_bytes(case) -> int:
   """Bytes one stored block takes on the fixture (K+V of the stage's single layer)."""

   config = _config()
   ports = _ports(case, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-measure", planner=_planner(case, ports, config))
   per_block = []
   for port in ports:
      snapshot = port.kv_snapshot()["prefix_cache"]
      assert snapshot["blocks"] == 3 and snapshot["bytes"] % 3 == 0
      per_block.append(snapshot["bytes"] // 3)
   assert per_block[0] == per_block[1] > 0
   return per_block[0]


def _control(case, prompt, *, request_id="r-control", steps=4):
   """A cold run on ports with the cache OFF: the exactness oracle."""

   tokens, plan = _generate(case, _ports(case, None), prompt, request_id=request_id, steps=steps)
   assert plan.reuse_tokens == 0
   return tokens


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_second_request_with_same_system_prompt_reuses_tokens_and_matches_cold(
   tmp_path: Path, architecture: str
) -> None:
   case = _case(tmp_path, architecture)
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)

   first, plan_first = _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)
   assert plan_first.reuse_tokens == 0
   assert first == _control(case, SYSTEM + USER_A, request_id="c-a")

   warm, plan_warm = _generate(case, ports, SYSTEM + USER_B, request_id="r-b", planner=planner)
   assert plan_warm.reuse_tokens == 3 * BLOCK
   assert warm == _control(case, SYSTEM + USER_B, request_id="c-b")
   assert warm[0] == _reference(case, SYSTEM + USER_B)

   for port in ports:
      snapshot = port.kv_snapshot()
      assert snapshot["prefix_cache"]["hits"] == 1
      assert snapshot["prefix_cache"]["reused_tokens_total"] == 3 * BLOCK
      assert snapshot["prefix_cache"]["pinned_blocks"] == 0
      # only the suffix was prefilled the second time
      assert snapshot["prefill_input_token_count"] == len(SYSTEM + USER_A) + len(USER_B)
      assert snapshot["active_state_count"] == 0
   assert planner.snapshot()["reused_tokens_total"] == 3 * BLOCK


@pytest.mark.parametrize("chunk", [4, 16])
def test_chunked_suffix_prefill_matches_cold(tmp_path: Path, chunk: int) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   long_user = (3, 6, 9, 12, 15, 18, 21, 24, 27, 30, 2, 4)

   cold_first, _ = _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner, chunk=chunk)
   warm, plan = _generate(case, ports, SYSTEM + long_user, request_id="r-b", planner=planner, chunk=chunk)

   assert plan.reuse_tokens == 3 * BLOCK
   assert warm == _control(case, SYSTEM + long_user, steps=4)
   assert cold_first == _control(case, SYSTEM + USER_A, request_id="c-a", steps=4)


def test_warm_kv_equals_cold_kv_and_state_reports_reused_tokens(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)

   _generate(case, ports, SYSTEM + USER_B, request_id="r-b", planner=planner, steps=0)
   cold_ports = _ports(case, None)
   _generate(case, cold_ports, SYSTEM + USER_B, request_id="c-b", steps=0)

   for warm_port, cold_port in zip(ports, cold_ports):
      assert warm_port.kv_snapshot()["states"]["path:r-b"]["prefix_reused_tokens"] == 3 * BLOCK
      warm_layers = warm_port._kv_states["path:r-b"].layers
      cold_layers = cold_port._kv_states["path:c-b"].layers
      assert warm_layers.keys() == cold_layers.keys()
      for layer in warm_layers:
         for warm_array, cold_array in zip(warm_layers[layer], cold_layers[layer]):
            assert warm_array.shape == cold_array.shape
            np.testing.assert_allclose(warm_array, cold_array, rtol=0, atol=1e-6)
      warm_port.cancel("path:r-b")
      cold_port.cancel("path:c-b")


def test_changed_token_in_the_prefix_invalidates_from_its_block(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)

   changed = list(SYSTEM)
   changed[BLOCK + 3] = (changed[BLOCK + 3] % 31) + 1  # inside block 1
   prompt = tuple(changed) + USER_B
   tokens, plan = _generate(case, ports, prompt, request_id="r-c", planner=planner)

   assert plan.reuse_tokens == BLOCK  # block 0 still matches, block 1 onward does not
   assert tokens == _control(case, prompt, request_id="c-c")


def test_stage_without_the_blocks_forces_the_shorter_length_never_a_partial_hit(
   tmp_path: Path,
) -> None:
   case = _case(tmp_path, "qwen2")
   block_bytes = _measure_block_bytes(case)
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)

   ports[1].set_prefix_cache_budget(block_bytes)  # stage 1 keeps one block (the root)
   tokens, plan = _generate(case, ports, SYSTEM + USER_B, request_id="r-b", planner=planner)
   assert plan.reuse_tokens == BLOCK
   assert tokens == _control(case, SYSTEM + USER_B, request_id="c-b")

   ports[1].clear_prefix_cache("model_unload")
   tokens, plan = _generate(case, ports, SYSTEM + USER_B, request_id="r-b2", planner=planner)
   assert plan.reuse_tokens == 0
   assert tokens == _control(case, SYSTEM + USER_B, request_id="c-b2")


def test_cap_is_never_exceeded_and_a_capped_cache_is_still_exact(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   block_bytes = _measure_block_bytes(case)
   config = _config(max_bytes=2 * block_bytes)
   ports = _ports(case, config)
   planner = _planner(case, ports, config)

   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)
   for port in ports:
      cache = port.kv_snapshot()["prefix_cache"]
      assert cache["bytes"] <= cache["max_bytes"] == 2 * block_bytes
      assert cache["blocks"] == 2
      assert cache["rejected_inserts"] >= 1

   tokens, plan = _generate(case, ports, SYSTEM + USER_B, request_id="r-b", planner=planner)
   assert plan.reuse_tokens == 2 * BLOCK
   assert tokens == _control(case, SYSTEM + USER_B, request_id="c-b")
   for port in ports:
      cache = port.kv_snapshot()["prefix_cache"]
      assert cache["bytes"] <= cache["max_bytes"]


def test_shrinking_the_budget_evicts_before_anything_else_and_zero_clears(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)
   for port in ports:
      port.set_prefix_cache_budget(0)
      assert port.kv_snapshot()["prefix_cache"]["blocks"] == 0
   tokens, plan = _generate(case, ports, SYSTEM + USER_B, request_id="r-b", planner=planner)
   assert plan.reuse_tokens == 0
   assert tokens == _control(case, SYSTEM + USER_B, request_id="c-b")


def test_unload_and_close_clear_the_cache(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   planner = _planner(case, ports, config)
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a", planner=planner)

   assert ports[0].clear_prefix_cache("model_unload") == 3
   assert ports[0].kv_snapshot()["prefix_cache"]["blocks"] == 0
   assert ports[1].kv_snapshot()["prefix_cache"]["blocks"] == 3
   ports[1].close()
   assert ports[1].kv_snapshot()["prefix_cache"]["blocks"] == 0
   assert ports[1].kv_snapshot()["prefix_cache"]["cleared_blocks"] == 3


def test_a_different_model_or_slice_never_hits(tmp_path: Path) -> None:
   qwen2_dir = tmp_path / "q2"
   qwen3_dir = tmp_path / "q3"
   qwen2_dir.mkdir()
   qwen3_dir.mkdir()
   case2 = _case(qwen2_dir, "qwen2")
   case3 = _case(qwen3_dir, "qwen3")
   config = _config()
   ports2 = _ports(case2, config)
   ports3 = _ports(case3, config)

   assert route_scope_digest(case2.graph) != route_scope_digest(case3.graph)
   scopes2 = {port._prefix._scopes[pid] for port, pid in zip(ports2, _placement_ids(case2))}
   scopes3 = {port._prefix._scopes[pid] for port, pid in zip(ports3, _placement_ids(case3))}
   assert len(scopes2) == 2 and not scopes2 & scopes3

   planner2 = _planner(case2, ports2, config)
   _generate(case2, ports2, SYSTEM + USER_A, request_id="r-a", planner=planner2)
   # The other model's stages hold nothing for the same prompt, even asked directly.
   chain = chain_digests(route_scope_digest(case2.graph), SYSTEM + USER_A, BLOCK)
   for port, pid in zip(ports3, _placement_ids(case3)):
      assert port.prefix_pin(pid, chain, "probe") == 0
   # A forged plan that names qwen2's chain fails closed on the qwen3 stage (no guessed reuse).
   forged = PrefixPlan(reuse_tokens=3 * BLOCK, chain=chain[:3])
   item = _item(
      case3, 0, encode_token_ids(USER_B), phase="PREFILL", request_id="forged", path_id="path:forged",
      token_index=-1, position=3 * BLOCK, span=len(USER_B), plan=forged,
   )
   result = ports3[0].execute(item)
   assert not result.success and result.failure_reason == "prefix_cache_miss"
   assert ports3[0].kv_snapshot()["active_state_count"] == 0


def test_off_by_default_is_inert_and_refuses_a_reuse_claim(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   ports = _ports(case, None)
   assert all(not port.prefix_cache_enabled for port in ports)
   assert all("prefix_cache" not in port.kv_snapshot() for port in ports)
   assert ports[0].prefix_pin(_placement_ids(case)[0], ("sha256:" + "0" * 64,), "x") == 0

   claim = PrefixPlan(reuse_tokens=BLOCK, chain=chain_digests("scope", SYSTEM, BLOCK))
   item = _item(
      case, 0, encode_token_ids(USER_A), phase="PREFILL", request_id="r", path_id="path:r",
      token_index=-1, position=BLOCK, span=len(USER_A), plan=claim,
   )
   result = ports[0].execute(item)
   assert not result.success and result.failure_reason == "prefix_cache_disabled"


def test_position_must_equal_the_claimed_reuse(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   config = _config()
   ports = _ports(case, config)
   claim = PrefixPlan(reuse_tokens=BLOCK, chain=chain_digests("scope", SYSTEM, BLOCK))
   item = _item(
      case, 0, encode_token_ids(USER_A), phase="PREFILL", request_id="r", path_id="path:r",
      token_index=-1, position=0, span=len(USER_A), plan=claim,
   )
   result = ports[0].execute(item)
   assert not result.success and result.failure_reason == "kv_position_mismatch"


def test_unplanned_requests_on_an_enabled_port_do_not_store(tmp_path: Path) -> None:
   case = _case(tmp_path, "qwen2")
   ports = _ports(case, _config())
   _generate(case, ports, SYSTEM + USER_A, request_id="r-a")  # no planner -> no chain
   assert all(port.kv_snapshot()["prefix_cache"]["blocks"] == 0 for port in ports)
