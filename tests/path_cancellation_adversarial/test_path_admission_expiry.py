# SPDX-License-Identifier: AGPL-3.0-only
"""Review 7: only admitted activity keeps a live path, and no live path
outlives its absolute lifetime.

This is the OPEN security flag on d99a05e (authorization / resource-cap
defeat), part (a) and part (b):

* (a) ``receive_progressive_prefill``, ``receive_hop`` and ``enqueue_hop``
  refreshed the path and marked it in flight *before* validating the frame, so
  any configured route peer could keep a path alive with frames this relay
  rejects.  Activity is now recorded only after admission.
* (b) Idle expiry alone is still a resource-cap defeat: an authorized
  participant can hold a path (and its creator slot) forever by keeping it
  warm.  There is now an absolute per-registration lifetime ceiling, read from
  the enforced production bounds and pinned below.

Every behavioral test below is a *behavioral* regression: it fails on the
pre-fix relay because a path that should retire does not (or a frame that
should be refused is not), not merely because a new constant is missing.
The two derivation tests additionally pin the constants; they are marked as
static oracles and are not the behavioral RED.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from pathlib import Path

from mycelium_router.contracts import HopHeader, RouterConfig
from mycelium_router.fakes import (
   FakeCapacityPort,
   FakeDeviceStateProvider,
   FakeRuntimePort,
   FakeTopologyProvider,
   FakeTransportPort,
   ManualClock,
   SequenceIdSource,
)
from mycelium_router.idempotency import hop_idempotency_key
from mycelium_router.router import Router
from test_router_receive_hop import locked_path


def _middle_router(devices=None):
   graph, request, manifest = locked_path()
   placements = {p.placement_id: p for stage in graph.stages for p in stage.placements}
   nodes = [placements[hop.placement_id].node_id for hop in manifest.ordered_hops]
   router = Router(
      node_id=nodes[1],
      topology=FakeTopologyProvider(graph),
      device_states=FakeDeviceStateProvider(devices if devices is not None else {}),
      capacity=FakeCapacityPort(),
      runtime=FakeRuntimePort(),
      transport=FakeTransportPort(),
      clock=ManualClock(),
      id_source=SequenceIdSource(),
      config=RouterConfig(),
   )
   return router, graph, request, manifest, nodes


def _with_entry(graph, manifest, node_id: str):
   first = manifest.ordered_hops[0].placement_id
   stages = tuple(
      replace(stage, placements=tuple(
         replace(p, node_id=node_id) if p.placement_id == first else p for p in stage.placements
      ))
      for stage in graph.stages
   )
   return replace(graph, stages=stages)


def _hop_header(request, manifest, *, token_index: int, hop_index: int = 1) -> HopHeader:
   """A well-formed hop addressed to this (the middle) router: its source is
   the previous placement, this node is the destination."""
   hop = manifest.ordered_hops[hop_index]
   source = manifest.ordered_hops[hop_index - 1]
   return HopHeader(
      request_id=request.request_id,
      path_id=manifest.path_id,
      path_attempt=manifest.path_attempt,
      phase="DECODE",
      token_index=token_index,
      hop_index=hop_index,
      source_placement_id=source.placement_id,
      destination_placement_id=hop.placement_id,
      topology_version=manifest.topology_version,
      idempotency_key=hop_idempotency_key(
         request_id=request.request_id,
         path_id=manifest.path_id,
         path_attempt=manifest.path_attempt,
         phase="DECODE",
         token_index=token_index,
         hop_index=hop_index,
      ),
   )


def _corrupt_key(header: HopHeader) -> HopHeader:
   """A frame whose idempotency key does not match its own fields: refused by
   every relay before anything else, but it still names a live path."""
   return replace(header, idempotency_key=header.idempotency_key + "-corrupt")


def _ceiling_seconds(relay_module) -> float:
   """The absolute lifetime ceiling, with the pre-fix default so the same
   tests run *behaviorally* on the old relay (where the constant does not
   exist): there they fail because a warm path is never retired, not with an
   ImportError.  The real constant's derivation is pinned by the static
   oracle at the bottom of this file."""
   return getattr(
      relay_module,
      "_LIVE_PATH_MAX_LIFETIME_SECONDS",
      3_600.0 + relay_module._LIVE_PATH_IDLE_SECONDS,
   )


# ---- part (a): only admitted activity refreshes ----------------------

def test_a_bad_idempotency_key_never_refreshes_a_live_path() -> None:
   """A frame that is refused for its idempotency key is not traffic.  Before
   the fix, one such frame per idle horizon kept the path alive forever; the
   retirement must land at the *first* idle deadline after the last admitted
   traffic, and the later refused frames must change nothing."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rk")
   man = replace(manifest, request_id="rk")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   header = _corrupt_key(_hop_header(req, man, token_index=0))
   # Rejected frames arrive every IDLE-1 s — denser than the idle horizon.
   # The FIRST idle deadline (t=IDLE, after the second frame's ingress sweep)
   # must retire the path even though rejected frames keep arriving.
   for index in range(3):
      relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
      result = router.receive_hop(header, req.prompt_token_ids, source_node_id=nodes[0])
      assert result.disposition == "REJECTED"
      if index == 0:
         assert result.reason == "invalid_idempotency_key"
         assert man.path_id in relay._paths
      else:
         # Retired at the first idle deadline despite the rejected frames.
         # (The frame still fails the *key* check first — that check precedes
         # the path lookup — so the observable retirement is the table.)
         assert man.path_id not in relay._paths
   # Nothing is left to retire: the path is already gone.
   assert relay.expire_idle_paths() == ()


def test_a_non_participant_peer_never_refreshes_a_live_path() -> None:
   """Even a correctly keyed, otherwise well-formed hop is refused when the
   sender is not the hop's source placement.  Before the fix it refreshed the
   path on its way to the rejection."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rn")
   man = replace(manifest, request_id="rn")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   header = _hop_header(req, man, token_index=0)
   for index in range(3):
      relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
      result = router.receive_hop(header, req.prompt_token_ids, source_node_id="node-stranger")
      assert result.disposition == "REJECTED"
      if index == 0:
         assert result.reason == "source_node_mismatch"
         assert man.path_id in relay._paths
      else:
         # The first idle deadline retires the path; later frames from the
         # non-participant find no live path at all.
         assert result.reason == "unknown_path"
         assert man.path_id not in relay._paths
   assert relay.expire_idle_paths() == ()


def test_a_refused_hop_never_holds_a_path_busy() -> None:
   """A stale-attempt frame is refused, and a refused frame must not even
   transiently block expiry: it never becomes in-flight work, so it cannot
   keep a path warm between sweeps."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rb")
   man = replace(manifest, request_id="rb")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   stale = _hop_header(req, man, token_index=0)
   stale = replace(
      stale,
      path_attempt=man.path_attempt + 1,
      idempotency_key=hop_idempotency_key(
         request_id=stale.request_id,
         path_id=stale.path_id,
         path_attempt=man.path_attempt + 1,
         phase=stale.phase,
         token_index=stale.token_index,
         hop_index=stale.hop_index,
      ),
   )
   for index in range(3):
      relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
      result = router.receive_hop(stale, req.prompt_token_ids, source_node_id=nodes[0])
      assert result.disposition == "REJECTED"
      if index == 0:
         assert result.reason == "stale_path_attempt"
         assert man.path_id in relay._paths
      else:
         assert result.reason == "unknown_path"
         assert man.path_id not in relay._paths
   assert relay.expire_idle_paths() == ()


def test_a_refused_enqueued_hop_never_refreshes_a_live_path() -> None:
   """The queuing ingress has the same hole: before the fix ``enqueue_hop``
   touched the path (and held it busy) before validating, so rejected frames
   kept the path alive through the queue entry point too."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="re")
   man = replace(manifest, request_id="re")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   header = _corrupt_key(_hop_header(req, man, token_index=0))
   for index in range(3):
      relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
      result = router.enqueue_hop(header, req.prompt_token_ids, source_node_id=nodes[0])
      assert result.disposition == "REJECTED"
      if index == 0:
         assert result.reason == "invalid_idempotency_key"
         assert man.path_id in relay._paths
      else:
         assert man.path_id not in relay._paths
   assert relay.expire_idle_paths() == ()


def test_a_refused_progressive_prefill_never_keeps_a_path() -> None:
   """The progressive-prefill ingress has the same hole: before the fix its
   pre-validation in-flight marking refreshed a *provisional* path, so a
   corrupt frame per idle horizon kept a prefill path alive forever.  Here a
   genuinely admitted prefill advances the build one hop (a FORWARDED
   extension keeping the provisional path live), and then only corrupt-key
   frames arrive: the provisional path must still retire at the first idle
   deadline."""
   from mycelium_router import relay as relay_module
   from mycelium_router.contracts import ProgressivePrefillContext, PathBuildState
   from test_router_policy import state_table

   router, graph, request, manifest, nodes = _middle_router(devices=state_table())
   relay = router.relay
   # The real entry is the first hop's node (no graph remap: the builder needs
   # the real device table to extend the route).
   entry = nodes[0]
   path_id = "prefill-path"
   # The builder has already placed the first (entry) hop and the middle hop; this middle relay
   # is the build's current last hop, so an admitted prefill here extends the
   # build and keeps the provisional path alive.
   build = PathBuildState(
      request=request,
      graph=graph,
      path_id=path_id,
      path_attempt=0,
      ordered_hops=manifest.ordered_hops[:2],
   )
   hop_index = len(build.ordered_hops) - 1
   header = HopHeader(
      request_id=request.request_id,
      path_id=path_id,
      path_attempt=0,
      phase="PREFILL",
      token_index=-1,
      hop_index=hop_index,
      source_placement_id=build.ordered_hops[hop_index - 1].placement_id if hop_index else "",
      destination_placement_id=build.ordered_hops[hop_index].placement_id,
      topology_version=graph.topology_version,
      idempotency_key=hop_idempotency_key(
         request_id=request.request_id,
         path_id=path_id,
         path_attempt=0,
         phase="PREFILL",
         token_index=-1,
         hop_index=hop_index,
      ),
   )
   context = ProgressivePrefillContext(
      graph=graph, request=request, build=build, payload=b"payload"
   )
   # One admitted prefill establishes/refreshes the provisional path at t=0.
   first = router.receive_progressive_prefill(
      header, context, source_node_id=entry, entry_node_id=entry
   )
   assert first.disposition == "FORWARDED", first
   assert path_id in relay._provisional_paths
   assert path_id in relay._path_activity
   # Corrupt-key frames, one per idle horizon: refused, and (after the fix)
   # never traffic.  Retirement lands at the FIRST idle deadline; later
   # corrupt frames still fail the key check first, so the observable is the
   # table state, not the rejection reason.
   bad = _corrupt_key(header)
   for index in range(3):
      relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS - 1)
      result = router.receive_progressive_prefill(
         bad, context, source_node_id=entry, entry_node_id=entry
      )
      assert (result.disposition, result.reason) == ("REJECTED", "invalid_idempotency_key")
      if index == 0:
         assert path_id in relay._provisional_paths
      else:
         assert path_id not in relay._provisional_paths
   assert relay.expire_idle_paths() == ()
   assert path_id not in relay._provisional_paths
   assert path_id not in relay._path_busy


def test_an_enqueued_and_an_admitted_hop_still_refresh_the_path() -> None:
   """The fix must not regress the useful half: an admitted hop still counts
   as activity, through both the synchronous and the queuing ingress.  The
   whole run stays *under* the absolute ceiling (it is not a ceiling test);
   the ceiling itself is pinned separately."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="ra")
   man = replace(manifest, request_id="ra")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   step = relay_module._LIVE_PATH_IDLE_SECONDS - 1
   # 4 hops * 1199 s = 4796 s < 4800 s: every hop is admitted and each one
   # refreshes the idle horizon while the path is still under the ceiling.
   assert 4 * step < _ceiling_seconds(relay_module)
   for token in range(4):
      relay.clock.advance(step)
      result = router.receive_hop(_hop_header(req, man, token_index=token), req.prompt_token_ids)
      assert result.disposition != "REJECTED", result
      assert relay.expire_idle_paths() == ()
   relay.clock.advance(1.0)
   queued = router.enqueue_hop(_hop_header(req, man, token_index=4), req.prompt_token_ids)
   assert queued.disposition != "REJECTED", queued
   assert man.path_id in relay._paths


# ---- part (b): the absolute lifetime ceiling -------------------------

def test_a_path_warm_forever_is_still_retired_at_the_ceiling() -> None:
   """The resource-cap defeat: a creator that keeps one path warm with valid
   hops below the idle horizon holds its live-path and creator slot forever.
   Past the ceiling the very next frame — valid, keyed, from the right peer —
   is refused, because ingress expiry retires the path first."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rc")
   man = replace(manifest, request_id="rc")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   step = relay_module._LIVE_PATH_IDLE_SECONDS - 1
   # Warm the path right up to (but not past) the ceiling, all admitted.
   elapsed = 0.0
   token = 0
   while elapsed + step <= _ceiling_seconds(relay_module):
      relay.clock.advance(step)
      elapsed += step
      token += 1
      result = router.receive_hop(_hop_header(req, man, token_index=token), req.prompt_token_ids)
      assert result.disposition != "REJECTED", result
   assert elapsed <= _ceiling_seconds(relay_module)
   # Past the ceiling the same perfectly valid hop is refused: ingress expiry
   # retired the path before the frame was processed.
   relay.clock.advance(_ceiling_seconds(relay_module) - elapsed + 1.0)
   result = router.receive_hop(_hop_header(req, man, token_index=token + 1), req.prompt_token_ids)
   assert (result.disposition, result.reason) == ("REJECTED", "unknown_path")
   # And with it went the resources: no live path, and the exact attempt is
   # bound to its owner (a replay of the same attempt cannot re-register).
   assert man.path_id not in relay._paths
   assert not relay.register_path(req, man, graph, entry_node_id=entry)
   # A NEW attempt is a new registration and gets its own fresh ceiling.
   assert relay.register_path(
      req, replace(man, path_attempt=man.path_attempt + 1), graph, entry_node_id=entry
   )


def test_queued_work_cannot_outlive_the_ceiling() -> None:
   """Work that never drains is not a reason to keep a path: the ceiling is
   not deferred by queued hops, unlike the idle horizon."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rq")
   man = replace(manifest, request_id="rq")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   # A hop queued and never drained: idle expiry alone would keep this path
   # alive indefinitely (see test_queued_work_still_defers_the_idle_horizon).
   queued = router.enqueue_hop(_hop_header(req, man, token_index=0), req.prompt_token_ids)
   assert queued.disposition == "QUEUED", queued
   relay.clock.advance(_ceiling_seconds(relay_module))
   assert relay.expire_idle_paths() == (man.path_id,)
   assert man.path_id not in relay._paths


def test_a_later_attempt_under_the_same_path_id_does_not_reset_the_ceiling() -> None:
   """A6 replays and A7 successors reuse the path id.  The ceiling runs from
   the first registration, so an entry cannot mint a fresh lifetime per
   attempt under one path id.  The path is kept genuinely live by admitted
   traffic until just under the ceiling, then re-registered (as A6 does)."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="rl")
   man = replace(manifest, request_id="rl")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   # Keep the path warm with admitted hops so it is not idle-expired, but
   # stop under the ceiling.
   step = relay_module._LIVE_PATH_IDLE_SECONDS - 1
   elapsed = 0.0
   token = 0
   while elapsed + step <= _ceiling_seconds(relay_module) - 1:
      relay.clock.advance(step)
      elapsed += step
      token += 1
      result = router.receive_hop(_hop_header(req, man, token_index=token), req.prompt_token_ids)
      assert result.disposition != "REJECTED", result
   assert elapsed < _ceiling_seconds(relay_module)
   # An A6 replay of the very same attempt just under the ceiling: admitted
   # (it is traffic), but it must NOT move the first-registration timestamp.
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   newer = replace(man, path_attempt=man.path_attempt + 1)
   assert relay.register_path(req, newer, graph, entry_node_id=entry)
   assert relay.expire_idle_paths() == ()
   # Past the ceiling measured from the FIRST registration.
   relay.clock.advance(_ceiling_seconds(relay_module) - elapsed + 1.0)
   assert relay.expire_idle_paths() == (man.path_id,)
   assert man.path_id not in relay._paths


def test_queued_work_still_defers_the_idle_horizon() -> None:
   """The idle bound keeps its previous meaning (queued work is not idle); only
   the absolute ceiling overrides it.  The run stays under the ceiling, so
   this is purely an idle-horizon oracle."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   req = replace(request, request_id="ri")
   man = replace(manifest, request_id="ri")
   assert relay.register_path(req, man, graph, entry_node_id=entry)
   queued = router.enqueue_hop(_hop_header(req, man, token_index=0), req.prompt_token_ids)
   assert queued.disposition == "QUEUED", queued
   assert (
      relay_module._LIVE_PATH_IDLE_SECONDS * 2
      < _ceiling_seconds(relay_module)
   )
   relay.clock.advance(relay_module._LIVE_PATH_IDLE_SECONDS * 2)
   assert relay.expire_idle_paths() == ()
   assert man.path_id in relay._paths


def test_the_ceiling_frees_a_creator_slot() -> None:
   """The reason the ceiling exists: every creator slot is held by a warm path
   and nothing else can register.  Each holder keeps its path genuinely live
   with *admitted* hops (so idle expiry is truly defeated), the cap is proven
   held before the cutoff, and after the ceiling plus the retention horizon a
   newcomer registers again."""
   from mycelium_router import relay as relay_module

   router, graph, request, manifest, nodes = _middle_router()
   relay = router.relay
   entry = "node-e"
   graph = _with_entry(graph, manifest, entry)
   cap = relay_module._MAX_TOMBSTONE_CREATORS
   graphs = {}
   manifests = {}
   for index in range(cap):
      creator = f"node-{index}"
      creator_graph = _with_entry(graph, manifest, creator)
      creator_man = replace(manifest, request_id=f"r{index}", path_id=f"w-{creator}")
      graphs[creator] = creator_graph
      manifests[creator] = creator_man
      assert relay.register_path(
         replace(request, request_id=f"r{index}"),
         creator_man,
         creator_graph,
         entry_node_id=creator,
      )
   # All cap slots are held; the cap is real: a newcomer is refused.
   newcomer = _with_entry(graph, manifest, "node-new")
   fresh_req = replace(request, request_id="rnew")
   fresh_man = replace(manifest, request_id="rnew", path_id="w-new")
   assert not relay.register_path(fresh_req, fresh_man, newcomer, entry_node_id="node-new")
   # Keep every holder's path warm with ADMITTED hops below the idle horizon,
   # so idle expiry alone can never retire them (the actual cap defeat).
   step = relay_module._LIVE_PATH_IDLE_SECONDS - 1
   elapsed = 0.0
   token = 0
   while elapsed + step <= _ceiling_seconds(relay_module):
      relay.clock.advance(step)
      elapsed += step
      token += 1
      for creator in graphs:
         result = router.receive_hop(
            _hop_header(
               replace(request, request_id=manifests[creator].request_id),
               manifests[creator],
               token_index=token,
            ),
            request.prompt_token_ids,
            source_node_id=creator,
         )
         assert result.disposition != "REJECTED", (creator, result)
      # Idle expiry is genuinely defeated while under the ceiling.
      assert relay.expire_idle_paths() == ()
   # Past the ceiling the sweep retires every one of them.
   relay.clock.advance(_ceiling_seconds(relay_module) - elapsed + 1.0)
   retired = relay.expire_idle_paths()
   assert len(retired) == cap
   # The records still bind their owners for the retention horizon...
   assert not relay.register_path(fresh_req, fresh_man, newcomer, entry_node_id="node-new")
   relay.clock.advance(relay_module._TOMBSTONE_RETENTION_SECONDS)
   # ...and after it, the slot is free again.
   assert relay.register_path(fresh_req, fresh_man, newcomer, entry_node_id="node-new")


# ---- derivation oracles (static; NOT the behavioral RED) -------------

def test_the_absolute_lifetime_is_the_enforced_request_deadline_plus_one_idle_horizon() -> None:
   """The ceiling is *derived*, not guessed: it is the wall-clock deadline the
   route already stamps on every physical command, plus one idle horizon (the
   longest a legitimate path may be quiet and still be inside that deadline).

   Both production literals are read by AST (not string matching), so any
   spelling of the integer (3_600_000, 3600000, ...) is handled; changing a
   literal has to be justified here.  Static oracle: it pins constants and
   call sites; the behavioral RED is the suite above."""
   from mycelium_router import relay as relay_module

   root = Path(__file__).resolve().parents[2]
   stamped = _deadline_literals(root / "mycelium_live/router_port.py", "issued_at_ms")
   assert 3_600_000 in stamped, "router_port stamps an absolute command deadline"
   legacy = _deadline_literals(root / "mycelium_live/route.py", None)
   assert 3_600_000 in legacy, "the legacy fallback uses the same deadline"
   assert relay_module._REQUEST_ABSOLUTE_DEADLINE_SECONDS == 3_600.0
   assert relay_module._LIVE_PATH_MAX_LIFETIME_SECONDS == (
      relay_module._REQUEST_ABSOLUTE_DEADLINE_SECONDS
      + relay_module._LIVE_PATH_IDLE_SECONDS
   )


def _deadline_literals(path: Path, added_to: str | None) -> set[int]:
   """Every ``x + <int literal>`` (or bare ``<int literal>``) on an
   ``absolute_deadline_ms`` value in the file, as an int."""
   tree = ast.parse(path.read_text())
   literals: set[int] = set()
   for node in ast.walk(tree):
      if (
         isinstance(node, ast.BinOp)
         and isinstance(node.op, ast.Add)
         and isinstance(node.right, ast.Constant)
         and isinstance(node.right.value, int)
      ):
         if added_to is None or _spells(node.left, added_to):
            literals.add(node.right.value)
   return literals


def _spells(node: ast.AST, name: str) -> bool:
   return (
      isinstance(node, ast.Name) and node.id == name
   ) or (
      isinstance(node, ast.Attribute) and node.attr == name
   )


def test_the_ceiling_does_not_outlive_the_command_it_bounds() -> None:
   """A recovery successor inherits the *source* command's deadline
   (``router_port.py``) rather than minting a new one, so no legitimate path
   work can outlive the deadline the ceiling is derived from.  Static
   oracle: pins the call-site contract the derivation rests on."""
   root = Path(__file__).resolve().parents[2]
   source = (root / "mycelium_live/router_port.py").read_text()
   assert (
      "absolute_deadline_ms=source_command.absolute_deadline_ms" in source
   ), "an A6/A7 successor inherits the source command's deadline"
