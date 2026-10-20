# SPDX-License-Identifier: AGPL-3.0-only
"""Assemble the diagnostics document from state the app already holds. Reads only; every section degrades to
``{"available": false, "reason": ...}`` instead of raising or guessing."""
from __future__ import annotations

import gc
import os
import time
import weakref
from pathlib import Path
from typing import Any, Callable

from .findings import derive_findings
from .host import host_metrics
from .mailbox_health import empty_peer, mailbox_health
from .state import DiagState, state_for

SCHEMA = "mycelium.diagnostics.v1"
HOST_CACHE_SECONDS = 5.0
ROUTE_CACHE_SECONDS = 10.0
_MAX_CHATS = 25
NO_ROUTE = "no route is open in this app (start the demo first)"


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"available": False, "reason": reason, **extra}


# -- the open route (liveness and timings) ---------------------------------------------------------------

def _route_is_open(route: Any) -> bool:
    return route is not None and bool(getattr(route, "_open", False)) and not getattr(route, "_closed", True)


def _open_route(state: DiagState) -> Any | None:
    """The live route open in this process, found without changing the route (nothing registers routes)."""
    now = time.monotonic()
    cached = state.route_cache
    if cached is not None and now - cached[0] < ROUTE_CACHE_SECONDS:
        if cached[1] is None:
            return None
        route = cached[1]()
        if _route_is_open(route):
            return route
    found = None
    for obj in gc.get_objects():
        if type(obj).__name__ == "PhysicalLiveRoute" and _route_is_open(obj):
            found = obj
    state.route_cache = (now, None if found is None else weakref.ref(found))
    return found


def _route_liveness(route: Any) -> dict[str, dict[str, Any]] | str:
    try:
        overview = getattr(route, "peer_liveness_overview", None)  # present once the V1.1 lane is merged
        if callable(overview):
            return dict(overview())
        from mycelium_live.liveness import LivenessState, SubjectKind
        labels = {LivenessState.FRESH: "fresh", LivenessState.RECOVERED: "fresh", LivenessState.SUSPECT: "stale",
                  LivenessState.QUARANTINED: "quarantined"}
        now_ms = route._monotonic_ms()
        result = {}
        for item in route._liveness.snapshots():
            if item.identity.kind is not SubjectKind.PEER:
                continue
            result[item.identity.subject_id] = {
                "state": item.state.value, "label": labels.get(item.state, "failed"),
                "last_seen_seconds_ago": round(max(0, now_ms - item.last_fresh_ms) / 1000.0, 3),
                "consecutive_misses": item.consecutive_misses,
                "recovery_observations": item.consecutive_signed_fresh,
                "next_probe_in_seconds": round(max(0, item.next_keepalive_due_ms - now_ms) / 1000.0, 3)}
        return result
    except Exception as error:
        return f"the route's liveness could not be read ({type(error).__name__})"


def _timings(route: Any | None) -> dict[str, Any]:
    per_hop = _unavailable("the route records whole-request timings (prefill, first token, per-token) and frame "
                           "counts per peer, not the time spent in each hop")
    if route is None:
        return {"available": False, "reason": NO_ROUTE, "per_hop": per_hop}
    try:
        recent = list(getattr(route, "_recent_inferences", ()))
    except Exception:
        recent = []
    if not recent:
        return {"available": False, "reason": "the open route has not served a request yet", "per_hop": per_hop}
    last = recent[-1]
    out: dict[str, Any] = {"context_tokens": last.get("context_tokens"), "output_tokens": last.get("output_tokens")}
    for key in ("prefill_ms", "ttft_ms", "tpot_ms", "total_ms"):
        value = last.get(key)
        out[key] = None if value is None else round(float(value), 2)
    total, tokens, tpot = out["total_ms"], out["output_tokens"], out["tpot_ms"]
    out["tokens_per_second"] = round(tokens * 1000.0 / total, 2) if total and tokens else None
    out["decode_tokens_per_second"] = round(1000.0 / tpot, 2) if tpot else None
    out["frames_by_peer"] = [
        {"node_id": item.get("node_id"), "frames_sent": item.get("frames_sent"),
         "frames_received": item.get("frames_received")} for item in last.get("peer_counter_deltas") or []]
    return {"available": True, "last_request": out, "per_hop": per_hop}


# -- chats, relay, run -------------------------------------------------------------------------------------

def _chats(app: Any, limit: int) -> dict[str, Any]:
    service = getattr(app, "chat", None)
    if service is None:
        return {"available": False, "reason": "no chat has been served by this app yet", "recent": [],
                "error_codes": {}}
    try:
        with service._lock:
            ids = list(service._order)[-min(limit, _MAX_CHATS):]
            records = [dict(service._requests[i]) for i in ids if i in service._requests]
        now = service._clock()
    except Exception:
        return _unavailable("the chat history could not be read", recent=[], error_codes={})
    recent, codes = [], {}
    for record in reversed(records):  # newest first
        started, finished = record.get("started"), record.get("finished")
        duration = None if finished is None or started is None else max(0.0, finished - started)
        count = len(record.get("tokens") or [])  # a count only; the text itself is never copied
        anchor = finished if finished is not None else started
        recent.append({
            "terminal": record.get("terminal") if record.get("done") else "running",
            "error_code": record.get("error"),
            "age_s": None if anchor is None else round(max(0.0, now - anchor), 1),
            "duration_s": None if duration is None else round(duration, 2),
            "output_tokens": count,
            "tokens_per_second": round(count / duration, 2) if duration and count else None,
            "clamped": bool(record.get("clamped"))})
        if record.get("error"):
            codes[record["error"]] = codes.get(record["error"], 0) + 1
    return {"available": True, "recent": recent, "error_codes": codes,
            "note": "the code is what the chat was told; a generic backend_failed hides the router's own reason"}


def _relay(app: Any, state: DiagState, now: float) -> dict[str, Any]:
    relay = getattr(app, "public_relay", None)
    if relay is None:
        return {"state": "private", "provider": None, "origin": None, "error_code": None, "renewals": 0,
                "started_at": None, "expires_in_seconds": None}
    try:
        status = dict(relay.status())
    except Exception:
        return {"state": "unknown", "provider": None, "origin": None, "error_code": "relay_status_unreadable"}
    with state.lock:
        key = id(relay)
        if key not in state.relay_first_seen:
            state.relay_first_seen[key] = now
            if state.relay_last_id is not None:
                state.relay_generations += 1
            state.relay_last_id = key
        first = state.relay_first_seen[key]
        renewals = state.relay_generations
    interval = getattr(getattr(relay, "_timer", None), "interval", None)
    interval = float(interval) if isinstance(interval, (int, float)) and not isinstance(interval, bool) else None
    return {"state": status.get("state"), "provider": status.get("provider"), "origin": status.get("public_origin"),
            "error_code": status.get("error_code"), "seed_bound": status.get("seed_bound"),
            "started_at": first, "started_is_first_observation": True, "max_seconds": interval,
            "expires_in_seconds": None if interval is None else round(first + interval - now, 1),
            "renewals": renewals}


def _run_status(app: Any) -> dict[str, Any] | None:
    """The owner run's status, read once per document (reading it can tick the run's monitor)."""
    run = getattr(app, "owner_run", None)
    if run is None:
        return None
    try:
        return dict(run.status())
    except Exception:
        return {"state": "unknown"}


def _run(status: dict[str, Any] | None) -> dict[str, Any]:
    if status is None:
        return {"state": "idle"}
    return {"state": status.get("state"), "step": status.get("step"), "error_code": status.get("error_code"),
            "failed_step": status.get("failed_step")}


# -- peers (owner) and the member's own view -------------------------------------------------------------

def _participant_labels(status: dict[str, Any] | None) -> dict[str, str]:
    try:
        return {str(p.get("node_id")): str(p.get("label") or "") for p in (status or {}).get("participants") or []}
    except Exception:
        return {}


def _round(value: Any) -> Any:
    return round(value, 3) if isinstance(value, float) else value


def _owner_peers(app: Any, state: DiagState, now: float, window_s: float, anonymize: bool,
                 run_status: dict[str, Any] | None) -> tuple[list, dict]:
    authority = getattr(app, "authority", None)
    coordinator = getattr(authority, "coordinator", None)
    if coordinator is None:
        return [], {"available": False, "reason": "this app has no swarm authority yet"}
    try:
        members = list(coordinator.members())
    except Exception:
        return [], {"available": False, "reason": "the swarm members could not be read"}
    database = getattr(getattr(authority, "control_mailbox", None), "database", None)
    if database is None:
        health: dict[str, Any] = {"available": False, "reason": "the command mailbox is not open"}
    else:
        health = mailbox_health(database, now=now, window_s=window_s)
    route = _open_route(state)
    liveness: Any = NO_ROUTE if route is None else _route_liveness(route)
    labels = _participant_labels(run_status)

    def is_own(node: str) -> bool:
        label = labels.get(node, "").lower()
        return "owner" in label or "this mac" in label

    remote_total = sum(1 for m in members if not is_own(str(m.get("node_id"))))
    remote_index = 0
    peers = []
    for member in sorted(members, key=lambda m: str(m.get("node_id"))):
        node = str(member.get("node_id"))
        own = is_own(node)
        if own:
            name = "This Mac"
        else:
            remote_index += 1
            name = "The joined Mac" if remote_total == 1 and not anonymize else f"Joined Mac {remote_index}"
        per = health["peers"].get(node, empty_peer()) if health.get("available") else None
        mail = {"available": True, **per} if per is not None else dict(health)
        if isinstance(liveness, dict):
            live = {"available": True, **liveness[node]} if node in liveness else \
                _unavailable("the open route does not track this peer")
        else:
            live = _unavailable(liveness)
        peers.append({
            "node_id": node, "name": name, "is_this_mac": own,
            "lifecycle_state": member.get("lifecycle_state"), "generation": member.get("generation"),
            "membership": {"liveness_status": member.get("liveness_status"),
                           "liveness_dead": member.get("liveness_dead"),
                           "liveness_reason": member.get("liveness_reason"),
                           "missed_keepalives": member.get("missed_keepalives")},
            "last_seen": {"membership_age_s": _round(member.get("liveness_age_seconds")),
                          "signed_message_age_s": state.clock.last_sample_age(node)},
            "clock": state.clock.estimate(node, rtt_s=None if per is None else per.get("min_round_trip_s")),
            "liveness": live, "mailbox": mail})
    return peers, health


def _member_view(state: DiagState) -> dict[str, Any]:
    owner, best = None, -1
    for peer in state.clock.peers():
        count = state.clock.estimate(peer).get("samples", 0)
        if count > best:
            owner, best = peer, count
    clock = state.clock.estimate(owner) if owner is not None else \
        _unavailable("no signed response from the owner received yet")
    return {"owner_clock": clock, "claim_loop": state.client.summary()}


# -- the document -------------------------------------------------------------------------------------------

def collect(app: Any, *, now: float | None = None, window_minutes: float = 10.0, chat_limit: int = 10,
            anonymize: bool = False, host_runner: Callable[[list[str]], str] | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    state = state_for(app)
    role = getattr(app, "installation_role", "owner")
    doc: dict[str, Any] = {"schema": SCHEMA, "generated_at": round(now, 3), "role": role,
                           "window_minutes": window_minutes, "peers": [], "member_view": None}
    run_status = _run_status(app)
    if role == "owner":
        peers, health = _owner_peers(app, state, now, window_minutes * 60.0, anonymize, run_status)
        doc["peers"] = peers
        doc["mailbox"] = {k: v for k, v in health.items() if k != "peers"}
    else:
        doc["member_view"] = _member_view(state)
    doc["run"] = _run(run_status)
    doc["chats"] = _chats(app, chat_limit)
    doc["relay"] = _relay(app, state, now)
    doc["timings"] = _timings(_open_route(state))
    cached = state.host_cache
    if cached is not None and host_runner is None and time.monotonic() - cached[0] < HOST_CACHE_SECONDS:
        doc["host"] = dict(cached[1])
    else:
        root = getattr(app, "root", None)
        doc["host"] = host_metrics(runner=host_runner, disk_path=Path(root) if root else Path(os.sep))
        if host_runner is None:
            state.host_cache = (time.monotonic(), dict(doc["host"]))
    doc["findings"] = derive_findings(doc)
    return doc
