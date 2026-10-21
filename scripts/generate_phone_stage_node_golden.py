# SPDX-License-Identifier: AGPL-3.0-only
"""Generate the phone stage-only node's behaviour golden scripts.

Each scenario runs the real Python ``mycelium_router.router.Router`` as the
middle placement (``node-phone``) of the locked three-hop route used by
``generate_phone_stage_wire_golden.py`` and records, step by step, what a
conforming middle node decides and sends for an ordered sequence of frames
from authenticated peers:

* ``ManifestLocked`` registration (accepted or not), dispatched the way
  ``mycelium_router/transports/iroh.py`` ``_dispatch`` does it (a manifest
  from the entry node registers with no source node, any other source must be
  the final placement's node);
* ``HopHeader`` admission and execution (disposition, reason, whether the
  stage ran) and every frame sent (forwarded hop to the next node, or the
  ``FailureReport`` to the entry node), byte for byte;
* ``PathCancellation`` acceptance, including pending tombstones for paths
  not yet registered and their effect on a later registration;
* the transport acknowledgement the Python transport gives each frame
  (``transport_ack``): ACK after dispatch, except a rejected manifest
  registration, which is not acknowledged (``_NON_DELIVERED_SCOPED_ERRORS``).

Stage failures are injected as the runtime result (``runtime_failure``); the
thermal case uses the phone's refusal reason ``thermal_ceiling_reached``.

Every scenario runs with the phone's path-table bounds (``PHONE_BOUNDS``,
patched into ``relay.py``; a scenario may shrink them to reach a bound in a
few frames), records them, and records the relay clock of every step
(``at``), so the phone replays the same horizon decisions.

Run from the repository root:
    python3 -B scripts/generate_phone_stage_node_golden.py [--check]
"""

from __future__ import annotations

from dataclasses import replace
import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mycelium_router.contracts import (  # noqa: E402
    ManifestLocked,
    PathCancellation,
    RuntimeResult,
)
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, decode_frame, encode_frame  # noqa: E402

_spec = importlib.util.spec_from_file_location("wire_golden", ROOT / "scripts" / "generate_phone_stage_wire_golden.py")
wg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wg)

OUTPUT_DIR = ROOT / "contracts" / "phone-stage-node-golden"
INDEX_SCHEMA = "mycelium.phone_stage_node.golden.v1"
ENTRY, PHONE, FINAL, STRANGER, EVIL = "node-mac-entry", wg.PHONE_NODE, "node-mac-final", "node-stranger", "node-evil"
THERMAL_REASON = "thermal_ceiling_reached"
#: stage_node.rs PHONE_BOUNDS. ``max_paths`` has no relay counterpart (the
#: relay's live paths are bounded by capacity reservations); no scenario
#: reaches it.
PHONE_BOUNDS = {"quota_per_creator": 512, "max_creators": 16, "pending_per_source": 256, "pending_total": 4096, "max_paths": 256}
_RELAY_BOUND_NAMES = {
   "quota_per_creator": "_TOMBSTONE_QUOTA_PER_CREATOR",
   "max_creators": "_MAX_TOMBSTONE_CREATORS",
   "pending_per_source": "_MAX_PENDING_PER_SOURCE",
   "pending_total": "_MAX_PENDING_TOTAL",
}


class Runtime(wg._StageRuntime):
    """The wire golden's deterministic stage, with an injectable failure."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: str | None = None

    def execute(self, item: Any) -> RuntimeResult:
        if self.failure is not None:
            self.executed.append(item)
            return RuntimeResult(success=False, failure_scope="PLACEMENT", failure_reason=self.failure)
        return super().execute(item)


class Scenario:
    def __init__(self, name: str, frames: dict[str, bytes], **bounds: int) -> None:
        from mycelium_router.contracts import RouterConfig
        from mycelium_router.fakes import (
            FakeCapacityPort,
            FakeDeviceStateProvider,
            FakeTopologyProvider,
            FakeTransportPort,
            ManualClock,
            SequenceIdSource,
        )
        from mycelium_router.router import Router

        from mycelium_router import relay as relay_module

        self.name = name
        self.frames = frames
        self.bounds = {**PHONE_BOUNDS, **bounds}
        self._relay_module = relay_module
        self._saved = {name: getattr(relay_module, name) for name in _RELAY_BOUND_NAMES.values()}
        for key, name in _RELAY_BOUND_NAMES.items():
            setattr(relay_module, name, self.bounds[key])
        self.clock = ManualClock()
        self.graph, self.request, self.build, self.manifest = wg._route()
        self.runtime = Runtime()
        self.transport = FakeTransportPort()
        self.router = Router(
            node_id=PHONE,
            topology=FakeTopologyProvider(self.graph),
            device_states=FakeDeviceStateProvider({}),
            capacity=FakeCapacityPort(),
            runtime=self.runtime,
            transport=self.transport,
            clock=self.clock,
            id_source=SequenceIdSource(),
            config=RouterConfig(),
        )
        self.steps: list[dict[str, Any]] = []

    def _file(self, data: bytes) -> str:
        digest = hashlib.sha256(data).hexdigest()[:16]
        name = f"{digest}.bin"
        decoded = decode_frame(data)
        if decoded.payload != decode_frame(encode_frame(decoded.message, decoded.payload)).payload:
            raise RuntimeError("frame does not round trip")
        self.frames[name] = data
        return name

    def _sent(self, before: tuple[int, int]) -> list[dict[str, str]]:
        sends = []
        for header, payload in self.transport.hops[before[0]:]:
            node = {"placement-final": FINAL, "placement-entry": ENTRY, "placement-phone": PHONE}[header.destination_placement_id]
            sends.append({"kind": "HopHeader", "to": node, "frame": self._file(encode_frame(header, payload))})
        for report in self.transport.failure_reports[before[1]:]:
            sends.append({"kind": "FailureReport", "to": ENTRY, "frame": self._file(encode_frame(report))})
        return sends

    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    def with_entry(self, node: str):
        """The same route with its entry placement on ``node``."""
        first = self.manifest.ordered_hops[0].placement_id
        stages = tuple(
            replace(stage, placements=tuple(replace(p, node_id=node) if p.placement_id == first else p for p in stage.placements))
            for stage in self.graph.stages
        )
        return replace(self.graph, stages=stages)

    def path(self, path_id: str, request_id: str):
        """Another path id (and request) on the same route."""
        return replace(self.manifest, path_id=path_id, request_id=request_id), replace(self.request, request_id=request_id)

    def manifest_locked(self, source: str, manifest=None, request=None, graph=None) -> None:
        manifest = manifest or self.manifest
        request = request or self.request
        graph = graph or self.graph
        build = replace(self.build, request=request, graph=graph, path_id=manifest.path_id, path_attempt=manifest.path_attempt)
        locked = ManifestLocked(request.request_id, manifest.path_id, manifest.path_attempt, manifest, build)
        # transports/iroh.py: the entry is the first ordered hop's node.
        placements = {p.placement_id: p for stage in graph.stages for p in stage.placements}
        entry = placements[manifest.ordered_hops[0].placement_id].node_id
        accepted = self.router.relay.register_path(
            request,
            manifest,
            graph,
            source_node_id=None if source == entry else source,
            entry_node_id=entry,
        )
        self.steps.append(
            {
                "at": self.clock.now(),
                "from": source,
                "frame": self._file(encode_frame(locked)),
                "accepted": bool(accepted),
                "transport_ack": bool(accepted),
                "sends": [],
            }
        )

    def hop(self, source: str, phase: str, token_index: int, *, attempt=None, failure=None, payload_scale=1.0, manifest=None, request=None) -> None:
        manifest = manifest or self.manifest
        manifest = manifest if attempt is None else replace(manifest, path_attempt=attempt)
        header = wg._header(manifest, request or self.request, phase, token_index)
        seq = len(wg.PROMPT) if phase == "PREFILL" else len(wg.PROMPT) + token_index
        payload = wg._activation(seq, payload_scale)
        self.runtime.failure = failure
        before = (len(self.transport.hops), len(self.transport.failure_reports))
        ran = len(self.runtime.executed)
        result = self.router.receive_hop(header, payload, source_node_id=source)
        self.runtime.failure = None
        self.steps.append(
            {
                "at": self.clock.now(),
                "from": source,
                "frame": self._file(encode_frame(header, payload)),
                "disposition": result.disposition,
                "reason": result.reason or "",
                "executed": len(self.runtime.executed) - ran,
                "transport_ack": True,
                "sends": self._sent(before),
            }
        )

    def cancel(self, source: str, *, attempt=None, topology=None, request_id=None, path_id=None) -> None:
        cancellation = PathCancellation(
            request_id or self.request.request_id,
            path_id or self.manifest.path_id,
            self.manifest.path_attempt if attempt is None else attempt,
            self.manifest.topology_version if topology is None else topology,
        )
        accepted = self.router.receive_path_cancellation(cancellation, source_node_id=source)
        self.steps.append(
            {"at": self.clock.now(), "from": source, "frame": self._file(encode_frame(cancellation)), "accepted": bool(accepted), "transport_ack": True, "sends": []}
        )

    def record(self) -> dict[str, Any]:
        for name, value in self._saved.items():
            setattr(self._relay_module, name, value)
        return {"name": self.name, "bounds": self.bounds, "steps": self.steps}


def _scenarios(frames: dict[str, bytes]) -> list[dict[str, Any]]:
    out = []

    s = Scenario("lifecycle", frames)
    s.manifest_locked(ENTRY)
    s.manifest_locked(ENTRY)  # sender retry: identical registration is accepted again
    s.hop(ENTRY, "PREFILL", -1)
    s.hop(ENTRY, "DECODE", 1)
    s.hop(ENTRY, "DECODE", 1)  # duplicate: cached, nothing re-run or re-sent
    s.hop(ENTRY, "DECODE", 1, payload_scale=2.0)  # same key, other payload
    s.hop(FINAL, "DECODE", 2)  # wrong source node
    s.hop(ENTRY, "DECODE", 2, attempt=1)  # stale attempt
    s.cancel(FINAL)  # only the entry may cancel
    s.cancel(ENTRY, attempt=1)
    s.cancel(ENTRY, topology=8)
    s.cancel(ENTRY)
    s.hop(ENTRY, "DECODE", 3)  # after cancellation
    s.manifest_locked(ENTRY)  # a cancelled attempt never re-registers
    out.append(s.record())

    s = Scenario("pending_cancellation", frames)
    s.cancel(ENTRY)
    s.manifest_locked(ENTRY)  # the entry's tombstone blocks registration
    s.hop(ENTRY, "DECODE", 1)
    out.append(s.record())

    s = Scenario("pending_cancellation_other_source", frames)
    s.cancel(STRANGER)
    s.manifest_locked(ENTRY)
    s.hop(ENTRY, "DECODE", 1)
    out.append(s.record())

    s = Scenario("manifest_sources", frames)
    s.manifest_locked(STRANGER)  # neither entry nor final placement
    s.manifest_locked(FINAL)  # the final placement's node may forward it
    s.hop(ENTRY, "DECODE", 1)
    out.append(s.record())

    s = Scenario("next_attempt", frames)
    s.manifest_locked(ENTRY)
    s.hop(ENTRY, "DECODE", 1)
    s.manifest_locked(ENTRY, manifest=replace(s.manifest, path_attempt=3))
    s.hop(ENTRY, "DECODE", 2)  # attempt 2 is now stale
    s.manifest_locked(ENTRY)  # an older attempt never replaces a newer one
    out.append(s.record())

    def evil(scenario: Scenario):
        """The same route with its entry placement on node-evil, for another request."""
        first = scenario.manifest.ordered_hops[0].placement_id
        stages = tuple(
            replace(stage, placements=tuple(
                replace(p, node_id=EVIL) if p.placement_id == first else p for p in stage.placements
            ))
            for stage in scenario.graph.stages
        )
        return replace(scenario.graph, stages=stages), replace(scenario.request, request_id="request-evil")

    s = Scenario("cross_owner_replacement", frames)
    s.manifest_locked(ENTRY)
    other = replace(s.request, request_id="request-other")
    # The final placement claims the same entry for another request: refused.
    s.manifest_locked(FINAL, manifest=replace(s.manifest, path_attempt=3, request_id="request-other"), request=other)
    evil_graph, evil_request = evil(s)
    s.manifest_locked(EVIL, manifest=replace(s.manifest, path_attempt=7, request_id="request-evil"), request=evil_request, graph=evil_graph)
    s.hop(ENTRY, "DECODE", 1)  # still the original owner's path
    out.append(s.record())

    s = Scenario("foreign_tombstone", frames)
    evil_graph, evil_request = evil(s)
    s.manifest_locked(EVIL, manifest=replace(s.manifest, path_attempt=5, request_id="request-evil"), request=evil_request, graph=evil_graph)
    s.cancel(EVIL, attempt=5, request_id="request-evil")
    s.manifest_locked(ENTRY)  # another owner's tombstones do not bind this owner
    s.hop(ENTRY, "DECODE", 1)
    out.append(s.record())

    s = Scenario("owner_high_water", frames)
    s.manifest_locked(ENTRY, manifest=replace(s.manifest, path_attempt=7))
    s.cancel(ENTRY, attempt=7)
    evil_graph, evil_request = evil(s)
    s.manifest_locked(EVIL, manifest=replace(s.manifest, path_attempt=1, request_id="request-evil"), request=evil_request, graph=evil_graph)
    s.cancel(EVIL, attempt=1, request_id="request-evil")
    s.manifest_locked(ENTRY, manifest=replace(s.manifest, path_attempt=5))  # under A's high-water mark
    s.manifest_locked(ENTRY, manifest=replace(s.manifest, path_attempt=8))
    out.append(s.record())

    s = Scenario("separator_ids", frames)
    # Two paths whose ':'-joined hop keys are equal: each hop still runs.
    first_request = replace(s.request, request_id="r:x")
    first = replace(s.manifest, request_id="r:x", path_id="p")
    second_request = replace(s.request, request_id="r")
    second = replace(s.manifest, request_id="r", path_id="x:p")
    s.manifest_locked(ENTRY, manifest=first, request=first_request)
    s.manifest_locked(ENTRY, manifest=second, request=second_request)
    s.hop(ENTRY, "DECODE", 1, manifest=first, request=first_request)
    s.hop(ENTRY, "DECODE", 1, manifest=second, request=second_request)
    s.cancel(ENTRY, request_id="r:x")  # targets path-phone-golden, unknown here: pending
    out.append(s.record())

    # Round 5 (RELAY-PATH-TABLES.md I5): past the horizon, the stale manifest
    # of a cancelled attempt is refused rather than admitted by evicting its
    # own record to make room; a fresh path evicts the entry's oldest record,
    # after which only that entry's own aged attempt reopens.
    s = Scenario("stale_manifest_after_horizon", frames, quota_per_creator=2)
    for index in range(2):
        manifest, request = s.path(f"old-{index}", f"request-old-{index}")
        s.manifest_locked(ENTRY, manifest=manifest, request=request)
        s.cancel(ENTRY, path_id=manifest.path_id, request_id=request.request_id)
    s.advance(601.0)
    stale, stale_request = s.path("old-0", "request-old-0")
    s.manifest_locked(ENTRY, manifest=stale, request=stale_request)
    fresh, fresh_request = s.path("fresh", "request-fresh")
    s.manifest_locked(ENTRY, manifest=fresh, request=fresh_request)
    s.manifest_locked(ENTRY, manifest=stale, request=stale_request)
    out.append(s.record())

    # I7: a live entry holds its slot; all slots busy refuses the newcomer;
    # past the horizon the idle slot is reclaimed, a busy one never.
    s = Scenario("creator_slots", frames, max_creators=2)
    s.manifest_locked(ENTRY)
    b_graph = s.with_entry("node-b")
    b_manifest, b_request = s.path("path-b", "request-b")
    s.manifest_locked("node-b", manifest=b_manifest, request=b_request, graph=b_graph)
    s.cancel("node-b", path_id="path-b", request_id="request-b")
    c_graph = s.with_entry("node-c")
    c_manifest, c_request = s.path("path-c", "request-c")
    s.manifest_locked("node-c", manifest=c_manifest, request=c_request, graph=c_graph)
    s.hop(ENTRY, "DECODE", 1)
    s.advance(601.0)
    s.manifest_locked("node-c", manifest=c_manifest, request=c_request, graph=c_graph)
    b2_manifest, b2_request = s.path("path-b2", "request-b2")
    s.manifest_locked("node-b", manifest=b2_manifest, request=b2_request, graph=b_graph)
    s.hop(ENTRY, "DECODE", 2)
    out.append(s.record())

    # I7: an entry's pending cancellation it has no slot to charge stays
    # pending and keeps refusing its attempt.
    s = Scenario("pending_without_slot", frames, max_creators=1)
    late_graph = s.with_entry("node-late")
    late_manifest, late_request = s.path("path-late", "request-late")
    s.cancel("node-late", path_id="path-late", request_id="request-late")
    s.manifest_locked(ENTRY)
    s.manifest_locked("node-late", manifest=late_manifest, request=late_request, graph=late_graph)
    s.manifest_locked("node-late", manifest=late_manifest, request=late_request, graph=late_graph)
    out.append(s.record())

    s = Scenario("failures", frames)
    s.manifest_locked(ENTRY)
    s.hop(ENTRY, "DECODE", 1, failure=THERMAL_REASON)
    s.hop(ENTRY, "DECODE", 1, failure=THERMAL_REASON)  # duplicate: cached failure, no resend
    s.hop(ENTRY, "DECODE", 2, failure="stage_runtime_failed")
    s.hop(ENTRY, "DECODE", 3)
    out.append(s.record())
    return out


def generate(check: bool = False) -> dict[str, Any]:
    frames: dict[str, bytes] = {}
    scenarios = _scenarios(frames)
    index = {
        "schema": INDEX_SCHEMA,
        "protocol": ROUTER_WIRE_PROTOCOL,
        "local_node_id": PHONE,
        "phone_bounds": PHONE_BOUNDS,
        "scenarios": scenarios,
        "frames": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(frames.items())},
    }
    rendered = json.dumps(index, indent=2, sort_keys=True) + "\n"
    if check:
        same = (OUTPUT_DIR / "index.json").read_text(encoding="utf-8") == rendered and all(
            (OUTPUT_DIR / name).read_bytes() == data for name, data in frames.items()
        )
        if not same:
            raise SystemExit("phone stage node golden scripts are stale; regenerate them")
        return index
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for old in OUTPUT_DIR.glob("*.bin"):
        if old.name not in frames:
            old.unlink()
    for name, data in frames.items():
        (OUTPUT_DIR / name).write_bytes(data)
    (OUTPUT_DIR / "index.json").write_text(rendered, encoding="utf-8")
    return index


if __name__ == "__main__":
    result = generate(check="--check" in sys.argv[1:])
    print(f"phone stage node golden: {len(result['scenarios'])} scenarios, {len(result['frames'])} frames")
