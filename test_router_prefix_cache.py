"""PV20: cross-request prefix KV cache core (digests, store, all-stage agreement)."""

from __future__ import annotations

import pytest

from mycelium_router.prefix_cache import (
   PrefixCacheConfig,
   PrefixKVCache,
   PrefixPlanner,
   agree_blocks,
   cap_bytes_from_budget,
   chain_digests,
   stage_scope_digest,
   usable_blocks,
)

ROUTE = "route-scope-a"
SCOPE = "stage-scope-0"


def _tokens(count: int, *, start: int = 0) -> tuple[int, ...]:
   return tuple(start + index for index in range(count))


def _cache(*, max_bytes: int = 1_000, block_tokens: int = 16, now=None) -> PrefixKVCache:
   clock = now or (lambda: 0.0)
   return PrefixKVCache(
      PrefixCacheConfig(enabled=True, block_tokens=block_tokens, max_bytes=max_bytes),
      clock=clock,
   )


def _fill(cache: PrefixKVCache, scope: str, chain: tuple[str, ...], *, nbytes: int = 100) -> None:
   parent = None
   for digest in chain:
      assert cache.insert(scope, digest, parent, {"digest": digest}, nbytes)
      parent = digest


# ---- digest chaining and block alignment -------------------------------------


def test_chain_has_only_full_blocks_and_is_deterministic() -> None:
   tokens = _tokens(16 * 3 + 5)
   chain = chain_digests(ROUTE, tokens, 16)
   assert len(chain) == 3
   assert chain == chain_digests(ROUTE, tokens, 16)
   assert all(d.startswith("sha256:") and len(d) == 71 for d in chain)
   assert len(set(chain)) == 3


def test_chain_ignores_the_partial_tail() -> None:
   base = _tokens(32)
   assert chain_digests(ROUTE, base + (9, 9, 9), 16) == chain_digests(ROUTE, base, 16)


def test_changed_token_invalidates_from_its_block_onward() -> None:
   tokens = _tokens(16 * 4)
   changed = list(tokens)
   changed[16 * 2 + 3] = 9_999  # inside block 2
   a = chain_digests(ROUTE, tokens, 16)
   b = chain_digests(ROUTE, tuple(changed), 16)
   assert a[:2] == b[:2]
   assert a[2] != b[2] and a[3] != b[3]


def test_chain_is_chained_not_per_block() -> None:
   same_block = _tokens(16)
   a = chain_digests(ROUTE, _tokens(16, start=500) + same_block, 16)
   b = chain_digests(ROUTE, _tokens(16, start=900) + same_block, 16)
   assert a[1] != b[1]


def test_chain_is_scoped_by_route_and_block_size() -> None:
   tokens = _tokens(64)
   assert chain_digests("route-a", tokens, 16) != chain_digests("route-b", tokens, 16)
   assert chain_digests(ROUTE, tokens, 16)[0] != chain_digests(ROUTE, tokens, 32)[0]


@pytest.mark.parametrize("bad", [0, 8, 24, 128, -16, True])
def test_block_size_is_restricted(bad) -> None:
   with pytest.raises(ValueError):
      PrefixCacheConfig(enabled=True, block_tokens=bad, max_bytes=1)
   with pytest.raises(ValueError):
      chain_digests(ROUTE, _tokens(64), bad)


@pytest.mark.parametrize(
   ("prompt_len", "block", "expected"),
   [(0, 16, 0), (16, 16, 0), (17, 16, 1), (32, 16, 1), (33, 16, 2), (400, 32, 12), (401, 32, 12)],
)
def test_usable_blocks_always_leaves_a_token_to_compute(prompt_len, block, expected) -> None:
   assert usable_blocks(prompt_len, block) == expected


# ---- all-stage agreement --------------------------------------------------------


def test_agreement_is_the_minimum_over_all_stages_capped_by_usable() -> None:
   assert agree_blocks([5, 3, 4], usable=10) == 3
   assert agree_blocks([5, 5], usable=2) == 2
   assert agree_blocks([0, 7], usable=10) == 0
   assert agree_blocks([], usable=10) == 0


class _Probe:
   def __init__(self, held_by_placement: dict[str, int]) -> None:
      self.held = dict(held_by_placement)
      self.pinned: dict[tuple[str, str], int] = {}
      self.settled: list[tuple[str, str, int]] = []

   def prefix_pin(self, placement_id, chain, owner):
      blocks = min(self.held.get(placement_id, 0), len(chain))
      self.pinned[(placement_id, owner)] = blocks
      return blocks

   def prefix_settle(self, placement_id, owner, keep_blocks):
      self.settled.append((placement_id, owner, keep_blocks))


def _planner(probes, *, block_tokens=16) -> PrefixPlanner:
   return PrefixPlanner(
      PrefixCacheConfig(enabled=True, block_tokens=block_tokens, max_bytes=1),
      route_scope=ROUTE,
      probes=probes,
   )


def test_stage_lacking_blocks_forces_the_shorter_length_and_trims_pins() -> None:
   probe = _Probe({"p0": 4, "p1": 2, "p2": 4})
   planner = _planner({"p0": probe, "p1": probe, "p2": probe})
   tokens = _tokens(16 * 4 + 1)

   plan = planner.plan("req-1", tokens, ("p0", "p1", "p2"))

   assert plan.reuse_tokens == 32
   assert plan.chain == chain_digests(ROUTE, tokens, 16)[:4]
   assert sorted(probe.settled) == [("p0", "req-1", 2), ("p1", "req-1", 2), ("p2", "req-1", 2)]


def test_no_partial_stage_hit_when_one_stage_is_cold() -> None:
   probe = _Probe({"p0": 4, "p1": 0})
   planner = _planner({"p0": probe, "p1": probe})
   plan = planner.plan("req-1", _tokens(16 * 4 + 1), ("p0", "p1"))
   assert plan.reuse_tokens == 0
   # a cold run still carries the chain so every stage can store what it computes
   assert plan.chain == chain_digests(ROUTE, _tokens(16 * 4 + 1), 16)
   assert all(keep == 0 for _p, _o, keep in probe.settled)


def test_unknown_probe_means_cold_run_without_pinning() -> None:
   probe = _Probe({"p0": 4})
   planner = _planner({"p0": probe})  # p1 has no probe
   plan = planner.plan("req-1", _tokens(16 * 4 + 1), ("p0", "p1"))
   assert plan.reuse_tokens == 0
   assert plan.chain == ()  # a plain cold request: nothing new on the wire
   assert probe.pinned == {}


def test_disabled_planner_never_probes() -> None:
   probe = _Probe({"p0": 4})
   planner = PrefixPlanner(
      PrefixCacheConfig(enabled=False, block_tokens=16, max_bytes=0),
      route_scope=ROUTE,
      probes={"p0": probe},
   )
   assert planner.plan("req-1", _tokens(80), ("p0",)).reuse_tokens == 0
   assert probe.pinned == {}


def test_planner_counts_hits_and_reused_tokens() -> None:
   probe = _Probe({"p0": 2})
   planner = _planner({"p0": probe})
   planner.plan("req-1", _tokens(16 * 2 + 1), ("p0",))
   planner.plan("req-2", _tokens(10), ("p0",))
   snapshot = planner.snapshot()
   assert snapshot["plans_total"] == 2
   assert snapshot["hits_total"] == 1
   assert snapshot["reused_tokens_total"] == 32


# ---- store: scoping, lookup, eviction, pins, clear ------------------------------


def test_scope_isolation_different_stage_never_hits() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(48), 16)
   _fill(cache, "stage-A", chain)
   assert cache.held_blocks("stage-A", chain) == 3
   assert cache.held_blocks("stage-B", chain) == 0


def test_stage_scope_digest_binds_model_slice_and_codec_identity() -> None:
   base = dict(
      stage_signature="sig-a",
      assignment_id="assign-1",
      load_proof_digest="sha256:" + "1" * 64,
      runtime_backend="numpy",
      layer_range=(0, 12),
      runtime_identity={"dtype": "float16", "quantization": "none", "architecture": "qwen2"},
   )
   reference = stage_scope_digest(ROUTE, **base)
   assert reference == stage_scope_digest(ROUTE, **base)
   assert reference != stage_scope_digest("other-route", **base)
   for key, value in (
      ("stage_signature", "sig-b"),
      ("assignment_id", "assign-2"),
      ("load_proof_digest", "sha256:" + "2" * 64),
      ("runtime_backend", "mlx"),
      ("layer_range", (0, 13)),
      ("runtime_identity", {"dtype": "float32", "quantization": "none", "architecture": "qwen2"}),
   ):
      assert stage_scope_digest(ROUTE, **{**base, key: value}) != reference


def test_held_blocks_is_the_leading_run_only() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(16 * 4), 16)
   _fill(cache, SCOPE, chain)
   gapped = (chain[0], "sha256:" + "f" * 64, chain[2])
   assert cache.held_blocks(SCOPE, gapped) == 1


def test_insert_requires_the_parent_to_be_present() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(32), 16)
   assert cache.insert(SCOPE, chain[1], chain[0], {"x": 1}, 10) is False
   assert cache.insert(SCOPE, chain[0], None, {"x": 1}, 10) is True
   assert cache.insert(SCOPE, chain[1], chain[0], {"x": 2}, 10) is True


def test_bytes_never_exceed_the_cap_and_lru_leaf_is_evicted_first() -> None:
   cache = _cache(max_bytes=300)
   a = chain_digests(ROUTE, _tokens(48), 16)
   b = chain_digests(ROUTE, _tokens(48, start=1000), 16)
   _fill(cache, SCOPE, a)  # 300 bytes: full
   assert cache.bytes == 300
   assert cache.insert(SCOPE, b[0], None, {"b": 0}, 100) is True
   assert cache.bytes <= 300
   # the oldest childless block (a's last) went; a's earlier blocks stay usable
   assert cache.held_blocks(SCOPE, a) == 2
   assert cache.held_blocks(SCOPE, b) == 1
   snap = cache.snapshot()
   assert snap["evicted_blocks"] == 1
   assert snap["bytes"] <= snap["max_bytes"]


def test_take_refreshes_recency_so_the_other_chain_is_evicted() -> None:
   cache = _cache(max_bytes=400)
   a = chain_digests(ROUTE, _tokens(32), 16)
   b = chain_digests(ROUTE, _tokens(32, start=1000), 16)
   _fill(cache, SCOPE, a)
   _fill(cache, SCOPE, b)
   assert cache.pin(SCOPE, a, "r") == 2
   assert len(cache.take(SCOPE, a, 2, "r")) == 2  # a is now most recent
   c = chain_digests(ROUTE, _tokens(16, start=2000), 16)
   assert cache.insert(SCOPE, c[0], None, {}, 100) is True
   assert cache.held_blocks(SCOPE, a) == 2
   assert cache.held_blocks(SCOPE, b) == 1


def test_oversized_block_is_rejected_without_evicting_everything() -> None:
   cache = _cache(max_bytes=250)
   chain = chain_digests(ROUTE, _tokens(32), 16)
   _fill(cache, SCOPE, chain)
   assert cache.insert(SCOPE, "sha256:" + "a" * 64, None, {}, 251) is False
   assert cache.held_blocks(SCOPE, chain) == 2
   assert cache.snapshot()["rejected_inserts"] == 1


def test_pinned_blocks_are_never_evicted_and_insert_is_refused_instead() -> None:
   cache = _cache(max_bytes=200)
   chain = chain_digests(ROUTE, _tokens(32), 16)
   _fill(cache, SCOPE, chain)
   assert cache.pin(SCOPE, chain, "req") == 2
   assert cache.insert(SCOPE, "sha256:" + "b" * 64, None, {}, 100) is False
   assert cache.bytes == 200
   cache.settle(SCOPE, "req", 0)
   assert cache.insert(SCOPE, "sha256:" + "b" * 64, None, {}, 100) is True
   assert cache.bytes <= 200


def test_settle_keeps_only_the_agreed_prefix_pinned() -> None:
   cache = _cache(max_bytes=300)
   chain = chain_digests(ROUTE, _tokens(48), 16)
   _fill(cache, SCOPE, chain)
   assert cache.pin(SCOPE, chain, "req") == 3
   cache.settle(SCOPE, "req", 1)
   assert cache.snapshot()["pinned_blocks"] == 1


def test_pins_expire_so_abandoned_requests_cannot_hold_memory() -> None:
   now = [0.0]
   cache = _cache(max_bytes=100, now=lambda: now[0])
   chain = chain_digests(ROUTE, _tokens(16), 16)
   _fill(cache, SCOPE, chain)
   assert cache.pin(SCOPE, chain, "req") == 1
   assert cache.insert(SCOPE, "sha256:" + "c" * 64, None, {}, 100) is False
   now[0] = 1_000.0
   assert cache.insert(SCOPE, "sha256:" + "c" * 64, None, {}, 100) is True


def test_take_returns_none_when_a_pinned_block_vanished_or_never_pinned() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(32), 16)
   _fill(cache, SCOPE, chain)
   assert cache.take(SCOPE, chain, 2, "never-pinned") is None
   assert cache.snapshot()["misses"] == 1


def test_take_counts_hit_and_consumes_the_pin() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(32), 16)
   _fill(cache, SCOPE, chain)
   cache.pin(SCOPE, chain, "req")
   blocks = cache.take(SCOPE, chain, 2, "req")
   assert [b["digest"] for b in blocks] == list(chain)
   snap = cache.snapshot()
   assert snap["hits"] == 1 and snap["pinned_blocks"] == 0
   assert snap["reused_tokens_total"] == 32


def test_shrinking_the_budget_evicts_immediately_and_zero_disables_storage() -> None:
   cache = _cache(max_bytes=400)
   _fill(cache, SCOPE, chain_digests(ROUTE, _tokens(64), 16))
   cache.set_max_bytes(150)
   assert cache.bytes <= 150
   cache.set_max_bytes(0)
   assert cache.bytes == 0 and cache.snapshot()["blocks"] == 0


def test_clear_on_unload_drops_everything_and_counts_it() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(48), 16)
   _fill(cache, SCOPE, chain)
   cache.pin(SCOPE, chain, "req")
   cache.clear("model_unload")
   assert cache.bytes == 0
   assert cache.held_blocks(SCOPE, chain) == 0
   assert cache.snapshot()["cleared_blocks"] == 3
   assert cache.take(SCOPE, chain, 3, "req") is None


def test_disabled_cache_is_inert() -> None:
   cache = PrefixKVCache(PrefixCacheConfig(enabled=False, block_tokens=16, max_bytes=0))
   assert cache.insert(SCOPE, "sha256:" + "d" * 64, None, {}, 1) is False
   assert cache.held_blocks(SCOPE, ("sha256:" + "d" * 64,)) == 0
   assert cache.pin(SCOPE, ("sha256:" + "d" * 64,), "r") == 0
   assert cache.snapshot()["enabled"] is False


def test_cap_is_a_bounded_fraction_of_the_sharing_budget() -> None:
   assert cap_bytes_from_budget(10_000_000_000, 0.10) == 1_000_000_000
   assert cap_bytes_from_budget(0, 0.10) == 0
   assert cap_bytes_from_budget(-5, 0.10) == 0
   with pytest.raises(ValueError):
      cap_bytes_from_budget(10, 1.5)


def test_snapshot_holds_no_digest_or_token_material() -> None:
   cache = _cache()
   chain = chain_digests(ROUTE, _tokens(32), 16)
   _fill(cache, SCOPE, chain)
   text = repr(cache.snapshot())
   assert not any(d in text for d in chain)
   assert SCOPE not in text
