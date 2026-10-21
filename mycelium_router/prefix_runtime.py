"""Runtime-side glue for the PV20 prefix KV cache, shared by the NumPy and MLX ports.

A port owns one ``PrefixRuntimeSupport``.  It stores per-block K/V slices of the
prompt prefix as soon as a request's KV covers them, answers the entry planner's
``pin``/``settle`` probes, and seeds a new request's KV from the stored blocks.
Array operations are injected so this module never imports a tensor library.
See ``docs/design/PREFIX-CACHE.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from mycelium_router.prefix_cache import PrefixCacheConfig, PrefixKVCache

Layers = Mapping[int, tuple[Any, Any]]


class PrefixSeedError(Exception):
   """Fail-closed seed refusal; ``code`` is the stable reject code."""

   def __init__(self, code: str) -> None:
      self.code = code
      super().__init__(code)


class PrefixRuntimeSupport:
   def __init__(
      self,
      config: PrefixCacheConfig,
      *,
      clock: Callable[[], float],
      slice_block: Callable[[Any, int, int], Any],
      concat_blocks: Callable[[Sequence[Any]], Any],
      array_nbytes: Callable[[Any], int],
   ) -> None:
      self.config = config
      self.cache = PrefixKVCache(config, clock=clock)
      self._slice_block = slice_block
      self._concat_blocks = concat_blocks
      self._array_nbytes = array_nbytes
      self._scopes: dict[str, str] = {}

   @property
   def enabled(self) -> bool:
      return self.config.enabled

   def register_scope(self, placement_id: str, scope: str) -> None:
      self._scopes[placement_id] = scope

   # -- planner probes -------------------------------------------------------

   def pin(self, placement_id: str, chain: Sequence[str], owner: str) -> int:
      scope = self._scopes.get(placement_id)
      if scope is None:
         return 0
      return self.cache.pin(scope, chain, owner)

   def settle(self, placement_id: str, owner: str, keep_blocks: int) -> None:
      scope = self._scopes.get(placement_id)
      if scope is not None:
         self.cache.settle(scope, owner, keep_blocks)

   # -- seeding ----------------------------------------------------------------

   def seed(
      self,
      placement_id: str,
      request_id: str,
      reuse_tokens: int,
      chain: Sequence[str],
      position: int,
   ) -> dict[int, tuple[Any, Any]]:
      """Rebuild this stage's KV for the first ``reuse_tokens`` tokens, or raise ``PrefixSeedError``."""

      block = self.config.block_tokens
      if not self.config.enabled:
         raise PrefixSeedError("prefix_cache_disabled")
      scope = self._scopes.get(placement_id)
      if (
         scope is None
         or reuse_tokens <= 0
         or reuse_tokens % block
         or position != reuse_tokens
         or len(chain) < reuse_tokens // block
      ):
         raise PrefixSeedError("prefix_plan_invalid")
      blocks = reuse_tokens // block
      payloads = self.cache.take(scope, chain, blocks, request_id)
      if payloads is None:
         raise PrefixSeedError("prefix_cache_miss")
      layer_ids = sorted(payloads[0])
      if any(sorted(payload) != layer_ids for payload in payloads):
         raise PrefixSeedError("prefix_cache_miss")
      return {
         layer: (
            self._concat_blocks([payload[layer][0] for payload in payloads]),
            self._concat_blocks([payload[layer][1] for payload in payloads]),
         )
         for layer in layer_ids
      }

   # -- storing ----------------------------------------------------------------

   def store_new_blocks(
      self,
      placement_id: str,
      chain: Sequence[str],
      stored_blocks: int,
      layers: Layers,
      covered_tokens: int,
   ) -> int:
      """Store the full prompt blocks ``layers`` now covers; returns the new stored-block count.

      Stops at the first block that does not fit (children need their parent), and
      then reports the chain as finished so it is not retried every step.
      """

      if not self.config.enabled or not chain:
         return len(chain)
      scope = self._scopes.get(placement_id)
      if scope is None:
         return len(chain)
      block = self.config.block_tokens
      available = min(len(chain), covered_tokens // block)
      index = stored_blocks
      while index < available:
         if self.cache.touch(scope, chain[index]):
            index += 1
            continue
         start, end = index * block, (index + 1) * block
         payload: dict[int, tuple[Any, Any]] = {}
         nbytes = 0
         for layer, (key, value) in layers.items():
            key_block = self._slice_block(key, start, end)
            value_block = self._slice_block(value, start, end)
            payload[layer] = (key_block, value_block)
            nbytes += self._array_nbytes(key_block) + self._array_nbytes(value_block)
         parent = chain[index - 1] if index else None
         if not self.cache.insert(scope, chain[index], parent, payload, nbytes):
            return len(chain)
         index += 1
      return index

   # -- lifecycle ----------------------------------------------------------------

   def clear(self, reason: str) -> int:
      return self.cache.clear(reason)

   def set_budget(self, max_bytes: int) -> None:
      self.cache.set_max_bytes(max_bytes)

   def snapshot(self) -> dict[str, Any]:
      return self.cache.snapshot()
