"""Cross-request prefix KV cache core (PV20).

Pure bookkeeping, independent of any tensor library: chained block digests of a
token prefix, a bounded per-port store of opaque per-block KV payloads, and the
entry-side planner that makes every stage agree on one reusable prefix length.
See ``docs/design/PREFIX-CACHE.md``.

Privacy: only salted digests and the caller's opaque KV payloads are held.  No
token id or text is stored, and ``snapshot()`` exposes counters only.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import math
import struct
import threading
import time
from typing import Any, Protocol

PREFIX_CACHE_PROTOCOL = "mycelium.prefix_cache.v1"
SUPPORTED_BLOCK_TOKENS = (16, 32, 64)
DEFAULT_BLOCK_TOKENS = 32
DEFAULT_PIN_TTL_SECONDS = 30.0
DEFAULT_BUDGET_FRACTION = 0.10
_DIGEST_PREFIX = "sha256:"
_MAX_TOKEN_ID = 0xFFFFFFFF


@dataclass(frozen=True)
class PrefixCacheConfig:
   """``enabled`` defaults to off; a disabled cache and planner are inert."""

   enabled: bool = False
   block_tokens: int = DEFAULT_BLOCK_TOKENS
   max_bytes: int = 0
   pin_ttl_seconds: float = DEFAULT_PIN_TTL_SECONDS

   def __post_init__(self) -> None:
      if type(self.enabled) is not bool:
         raise ValueError("invalid_prefix_cache_enabled")
      _validate_block_tokens(self.block_tokens)
      if type(self.max_bytes) is not int or self.max_bytes < 0:
         raise ValueError("invalid_prefix_cache_max_bytes")
      if (
         isinstance(self.pin_ttl_seconds, bool)
         or not isinstance(self.pin_ttl_seconds, (int, float))
         or not math.isfinite(float(self.pin_ttl_seconds))
         or not self.pin_ttl_seconds > 0
      ):
         raise ValueError("invalid_prefix_cache_pin_ttl")


def _validate_block_tokens(block_tokens: object) -> int:
   if type(block_tokens) is not int or block_tokens not in SUPPORTED_BLOCK_TOKENS:
      raise ValueError("invalid_prefix_block_tokens")
   return block_tokens


def cap_bytes_from_budget(budget_bytes: int, fraction: float = DEFAULT_BUDGET_FRACTION) -> int:
   """The cache cap as a bounded fraction of the sharing memory budget (0 when no budget)."""

   if (
      isinstance(fraction, bool)
      or not isinstance(fraction, (int, float))
      or not 0 < float(fraction) <= 1
   ):
      raise ValueError("invalid_prefix_budget_fraction")
   if type(budget_bytes) is not int or budget_bytes <= 0:
      return 0
   return int(budget_bytes * float(fraction))


def usable_blocks(prompt_length: int, block_tokens: int) -> int:
   """Blocks a request may reuse: at least one token is always computed (first-token logits)."""

   _validate_block_tokens(block_tokens)
   return max(0, (int(prompt_length) - 1) // block_tokens)


def _canonical(document: Mapping[str, Any]) -> bytes:
   return json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(*parts: bytes) -> str:
   hasher = hashlib.sha256()
   for part in parts:
      hasher.update(struct.pack(">I", len(part)))
      hasher.update(part)
   return _DIGEST_PREFIX + hasher.hexdigest()


def _pack_tokens(tokens: Sequence[int]) -> bytes:
   for token in tokens:
      if type(token) is not int or token < 0 or token > _MAX_TOKEN_ID:
         raise ValueError("invalid_prefix_token_id")
   return struct.pack(f">{len(tokens)}I", *tokens)


def chain_digests(route_scope: str, tokens: Sequence[int], block_tokens: int) -> tuple[str, ...]:
   """Chained digest of every FULL block of ``tokens`` (the partial tail is ignored).

   ``d_0`` binds the route scope and block size; ``d_k`` binds ``d_{k-1}``, so a
   changed token changes its block's digest and every later one.
   """

   _validate_block_tokens(block_tokens)
   if not isinstance(route_scope, str) or not route_scope:
      raise ValueError("invalid_prefix_route_scope")
   chain: list[str] = []
   previous = b""
   for start in range(0, (len(tokens) // block_tokens) * block_tokens, block_tokens):
      block = _pack_tokens(tokens[start : start + block_tokens])
      if not chain:
         digest = _digest(
            PREFIX_CACHE_PROTOCOL.encode(),
            b"root",
            route_scope.encode("utf-8"),
            struct.pack(">I", block_tokens),
            block,
         )
      else:
         digest = _digest(PREFIX_CACHE_PROTOCOL.encode(), b"link", previous, block)
      chain.append(digest)
      previous = digest.encode()
   return tuple(chain)


def route_scope_digest(graph: Any) -> str:
   """Digest of the exact route identity (model, deployment, split, codec, runtime)."""

   stages = []
   for stage in graph.stages:
      stages.append(
         {
            "stage_id": stage.stage_id,
            "start": stage.layer_range.start_layer,
            "end": stage.layer_range.end_layer_exclusive,
            "roles": list(stage.component_roles),
            "signatures": sorted(
               {(p.stage_signature, p.runtime_backend) for p in stage.placements}
            ),
         }
      )
   document = {
      "deployment_id": graph.deployment_id,
      "deployment_epoch": graph.deployment_epoch,
      "model_id": graph.model_id,
      "resolved_commit": graph.resolved_commit,
      "manifest_digest": graph.manifest_digest,
      "hidden_size": graph.hidden_size,
      "activation_bytes": graph.activation_bytes,
      "entry_stage_id": graph.entry_stage_id,
      "final_stage_id": graph.final_stage_id,
      "stages": stages,
   }
   return _digest(PREFIX_CACHE_PROTOCOL.encode(), b"route", _canonical(document))


def stage_scope_digest(
   route_scope: str,
   *,
   stage_signature: str,
   assignment_id: str,
   load_proof_digest: str,
   runtime_backend: str,
   layer_range: tuple[int, int],
   runtime_identity: Mapping[str, Any],
) -> str:
   """Scope of one placement's store: only that exact slice/weights/runtime can hit."""

   document = {
      "route": route_scope,
      "stage_signature": stage_signature,
      "assignment_id": assignment_id,
      "load_proof_digest": load_proof_digest,
      "runtime_backend": runtime_backend,
      "layer_range": list(layer_range),
      "runtime_identity": {str(k): v for k, v in sorted(runtime_identity.items())},
   }
   return _digest(PREFIX_CACHE_PROTOCOL.encode(), b"stage", _canonical(document))


def agree_blocks(held: Sequence[int], *, usable: int) -> int:
   """All-stage agreement: the shortest leading run any stage holds, capped by ``usable``."""

   if not held:
      return 0
   return max(0, min(min(int(h) for h in held), int(usable)))


@dataclass
class _Block:
   payload: Any
   nbytes: int
   parent: str | None
   children: int = 0
   pins: int = 0


@dataclass
class _Pin:
   digests: tuple[str, ...]
   expires_at: float


class PrefixKVCache:
   """Bounded LRU store of per-block KV payloads, shared by the placements of one port."""

   def __init__(
      self,
      config: PrefixCacheConfig,
      *,
      clock: Callable[[], float] = time.monotonic,
   ) -> None:
      self.config = config
      self._clock = clock
      self._lock = threading.RLock()
      self._max_bytes = config.max_bytes if config.enabled else 0
      self._blocks: OrderedDict[tuple[str, str], _Block] = OrderedDict()
      self._pins: dict[tuple[str, str], _Pin] = {}
      self._bytes = 0
      self._hits = 0
      self._misses = 0
      self._reused_tokens_total = 0
      self._inserted_blocks = 0
      self._evicted_blocks = 0
      self._rejected_inserts = 0
      self._cleared_blocks = 0
      self._pins_expired = 0

   @property
   def enabled(self) -> bool:
      return self.config.enabled

   @property
   def bytes(self) -> int:
      return self._bytes

   @property
   def max_bytes(self) -> int:
      return self._max_bytes

   # -- reads -----------------------------------------------------------------

   def held_blocks(self, scope: str, chain: Sequence[str]) -> int:
      """Length of the leading run of ``chain`` this scope holds (no side effects)."""

      if not self.config.enabled:
         return 0
      with self._lock:
         return self._leading_run(scope, chain)

   def _leading_run(self, scope: str, chain: Sequence[str]) -> int:
      held = 0
      for digest in chain:
         if (scope, digest) not in self._blocks:
            break
         held += 1
      return held

   def touch(self, scope: str, digest: str) -> bool:
      """Refresh recency of an existing block; ``False`` if absent."""

      if not self.config.enabled:
         return False
      with self._lock:
         key = (scope, digest)
         if key not in self._blocks:
            return False
         self._blocks.move_to_end(key)
         return True

   # -- pinning ---------------------------------------------------------------

   def pin(self, scope: str, chain: Sequence[str], owner: str) -> int:
      """Pin the leading run against eviction for ``owner``; returns its length."""

      if not self.config.enabled:
         return 0
      with self._lock:
         self._expire_pins()
         self._drop_pin((scope, owner))
         held = self._leading_run(scope, chain)
         if held == 0:
            return 0
         digests = tuple(chain[:held])
         for digest in digests:
            self._blocks[(scope, digest)].pins += 1
         self._pins[(scope, owner)] = _Pin(
            digests=digests,
            expires_at=float(self._clock()) + self.config.pin_ttl_seconds,
         )
         return held

   def settle(self, scope: str, owner: str, keep_blocks: int) -> None:
      """Keep only the first ``keep_blocks`` blocks of ``owner``'s pin."""

      if not self.config.enabled:
         return
      with self._lock:
         pin = self._pins.get((scope, owner))
         if pin is None:
            return
         keep = max(0, min(int(keep_blocks), len(pin.digests)))
         for digest in pin.digests[keep:]:
            block = self._blocks.get((scope, digest))
            if block is not None and block.pins > 0:
               block.pins -= 1
         if keep == 0:
            del self._pins[(scope, owner)]
         else:
            pin.digests = pin.digests[:keep]

   def _drop_pin(self, key: tuple[str, str]) -> None:
      pin = self._pins.pop(key, None)
      if pin is None:
         return
      scope = key[0]
      for digest in pin.digests:
         block = self._blocks.get((scope, digest))
         if block is not None and block.pins > 0:
            block.pins -= 1

   def _expire_pins(self) -> None:
      now = float(self._clock())
      for key in [k for k, pin in self._pins.items() if now >= pin.expires_at]:
         self._drop_pin(key)
         self._pins_expired += 1

   def take(
      self, scope: str, chain: Sequence[str], blocks: int, owner: str
   ) -> list[Any] | None:
      """Consume ``owner``'s pin and return the first ``blocks`` payloads, or ``None`` (miss)."""

      if not self.config.enabled:
         return None
      with self._lock:
         self._expire_pins()
         pin = self._pins.get((scope, owner))
         wanted = tuple(chain[:blocks])
         ok = (
            blocks >= 1
            and pin is not None
            and len(wanted) == blocks
            and pin.digests[:blocks] == wanted
            and all((scope, digest) in self._blocks for digest in wanted)
         )
         self._drop_pin((scope, owner))
         if not ok:
            self._misses += 1
            return None
         payloads = []
         for digest in wanted:
            self._blocks.move_to_end((scope, digest))
            payloads.append(self._blocks[(scope, digest)].payload)
         self._hits += 1
         self._reused_tokens_total += blocks * self.config.block_tokens
         return payloads

   # -- writes ----------------------------------------------------------------

   def insert(
      self, scope: str, digest: str, parent: str | None, payload: Any, nbytes: int
   ) -> bool:
      """Store one block; ``False`` if it cannot fit under the cap without evicting pinned data."""

      if not self.config.enabled:
         return False
      if type(nbytes) is not int or nbytes < 0:
         raise ValueError("invalid_prefix_block_bytes")
      with self._lock:
         key = (scope, digest)
         existing = self._blocks.get(key)
         if existing is not None:
            self._blocks.move_to_end(key)
            return True
         if parent is not None and (scope, parent) not in self._blocks:
            return False
         self._expire_pins()
         if nbytes > self._max_bytes:
            self._rejected_inserts += 1
            return False
         protected = {(scope, parent)} if parent is not None else set()
         victims = self._plan_evictions(
            need=self._bytes + nbytes - self._max_bytes,
            protected=protected,
            force=False,
         )
         if victims is None:
            self._rejected_inserts += 1
            return False
         for victim in victims:
            self._evict(victim)
         self._blocks[key] = _Block(payload=payload, nbytes=nbytes, parent=parent)
         if parent is not None:
            self._blocks[(scope, parent)].children += 1
         self._bytes += nbytes
         self._inserted_blocks += 1
         return True

   def _plan_evictions(
      self,
      *,
      need: int,
      protected: set[tuple[str, str]],
      force: bool,
   ) -> list[tuple[str, str]] | None:
      """Oldest childless (then newly childless) blocks until ``need`` bytes are freed."""

      if need <= 0:
         return []
      children = {key: block.children for key, block in self._blocks.items()}
      chosen: list[tuple[str, str]] = []
      chosen_set: set[tuple[str, str]] = set()
      freed = 0
      while freed < need:
         victim = None
         for key, block in self._blocks.items():
            if key in chosen_set or key in protected or children[key] != 0:
               continue
            if block.pins > 0 and not force:
               continue
            victim = key
            break
         if victim is None:
            return None
         chosen.append(victim)
         chosen_set.add(victim)
         freed += self._blocks[victim].nbytes
         parent = self._blocks[victim].parent
         if parent is not None and (victim[0], parent) in children:
            children[(victim[0], parent)] -= 1
      return chosen

   def _evict(self, key: tuple[str, str]) -> None:
      block = self._blocks.pop(key)
      self._bytes -= block.nbytes
      self._evicted_blocks += 1
      if block.parent is not None:
         parent = self._blocks.get((key[0], block.parent))
         if parent is not None and parent.children > 0:
            parent.children -= 1

   def set_max_bytes(self, max_bytes: int) -> None:
      """Pressure / budget change: shrink immediately (pinned blocks included; their take misses)."""

      if type(max_bytes) is not int or max_bytes < 0:
         raise ValueError("invalid_prefix_cache_max_bytes")
      with self._lock:
         self._max_bytes = max_bytes if self.config.enabled else 0
         if self._bytes <= self._max_bytes:
            return
         victims = self._plan_evictions(
            need=self._bytes - self._max_bytes, protected=set(), force=True
         )
         for victim in victims or ():
            self._evict(victim)

   def clear(self, reason: str = "cleared") -> int:
      """Drop every block and pin (model unload, route teardown, node close)."""

      with self._lock:
         dropped = len(self._blocks)
         self._cleared_blocks += dropped
         self._blocks.clear()
         self._pins.clear()
         self._bytes = 0
         return dropped

   # -- observability ---------------------------------------------------------

   def snapshot(self) -> dict[str, Any]:
      """Counters only: no digest, scope or token material."""

      with self._lock:
         return {
            "enabled": self.config.enabled,
            "block_tokens": self.config.block_tokens,
            "max_bytes": self._max_bytes,
            "bytes": self._bytes,
            "blocks": len(self._blocks),
            "pinned_blocks": sum(1 for b in self._blocks.values() if b.pins > 0),
            "hits": self._hits,
            "misses": self._misses,
            "reused_tokens_total": self._reused_tokens_total,
            "inserted_blocks": self._inserted_blocks,
            "evicted_blocks": self._evicted_blocks,
            "rejected_inserts": self._rejected_inserts,
            "cleared_blocks": self._cleared_blocks,
            "pins_expired": self._pins_expired,
         }


class StageProbe(Protocol):
   """What the planner needs from a stage (runtime port, or a remote proxy)."""

   def prefix_pin(self, placement_id: str, chain: Sequence[str], owner: str) -> int: ...

   def prefix_settle(self, placement_id: str, owner: str, keep_blocks: int) -> None: ...


@dataclass(frozen=True)
class PrefixPlan:
   """``chain`` is every full prompt block (stages store from it); ``reuse_tokens`` the agreed skip."""

   reuse_tokens: int = 0
   chain: tuple[str, ...] = ()


class PrefixPlanner:
   """Entry-side planner: pins on every stage of the path, reuses the shortest run."""

   def __init__(
      self,
      config: PrefixCacheConfig,
      *,
      route_scope: str,
      probes: Mapping[str, StageProbe],
   ) -> None:
      self.config = config
      self.route_scope = route_scope
      self._probes = dict(probes)
      self._lock = threading.Lock()
      self._plans = 0
      self._hits = 0
      self._reused_tokens_total = 0

   def plan(
      self,
      request_id: str,
      prompt_token_ids: Sequence[int],
      placement_ids: Sequence[str],
   ) -> PrefixPlan:
      if not self.config.enabled:
         return PrefixPlan()
      block = self.config.block_tokens
      chain = chain_digests(self.route_scope, prompt_token_ids, block)
      usable = usable_blocks(len(prompt_token_ids), block)
      with self._lock:
         self._plans += 1
      probes = [self._probes.get(placement_id) for placement_id in placement_ids]
      if not placement_ids or any(probe is None for probe in probes):
         # A stage we cannot ask (e.g. a member without the feature) means a plain
         # cold request: no chain on the wire either.
         return PrefixPlan()
      if usable == 0:
         return PrefixPlan(reuse_tokens=0, chain=chain)
      held: list[int] = []
      for placement_id, probe in zip(placement_ids, probes):
         try:
            held.append(max(0, int(probe.prefix_pin(placement_id, chain[:usable], request_id))))
         except Exception:
            held.append(0)
      agreed = agree_blocks(held, usable=usable)
      self._settle(request_id, placement_ids, probes, agreed)
      if agreed:
         with self._lock:
            self._hits += 1
            self._reused_tokens_total += agreed * block
      return PrefixPlan(reuse_tokens=agreed * block, chain=chain)

   def release(self, request_id: str, placement_ids: Sequence[str]) -> None:
      """Drop any pins of a request that will not start (admission failed)."""

      self._settle(
         request_id, placement_ids, [self._probes.get(p) for p in placement_ids], 0
      )

   @staticmethod
   def _settle(request_id, placement_ids, probes, keep_blocks: int) -> None:
      for placement_id, probe in zip(placement_ids, probes):
         if probe is None:
            continue
         try:
            probe.prefix_settle(placement_id, request_id, keep_blocks)
         except Exception:
            pass

   def snapshot(self) -> dict[str, Any]:
      with self._lock:
         return {
            "enabled": self.config.enabled,
            "block_tokens": self.config.block_tokens,
            "plans_total": self._plans,
            "hits_total": self._hits,
            "reused_tokens_total": self._reused_tokens_total,
         }
