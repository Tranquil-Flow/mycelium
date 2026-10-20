# SPDX-License-Identifier: AGPL-3.0-only
"""Peer clock offset from signed message timestamps vs receipt time (RTT-corrected).

Every control message already carries its signer's ``issued_at``.  Seen at the receiver, a sample is
``remote_issued_at - local_receipt_time = offset - one_way_delay``; the least delayed sample is the largest, so
the estimate is ``max(sample) + rtt/2`` (the classic minimum-delay filter).  Positive means the REMOTE clock
is ahead of the local one.

The probes only READ timestamps the protocol already exchanges.  They are installed as wrappers (an instance
wrapper on the owner mailbox, a class wrapper on the member's control client) and never alter a request, a
response, a deadline or an exception.
"""
from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any, Callable, Mapping, Sequence

WINDOW_SECONDS = 300.0
MAX_SAMPLES = 256
IN_SYNC_SECONDS = 0.25
_POLL = "/managed-control/poll"
_ACK = "/managed-control/ack"


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float("inf"), float("-inf"))


def estimate_offset(pairs: Sequence[tuple[float, float]], *, rtt_s: float | None = None) -> dict[str, Any]:
    """``pairs`` are ``(remote_issued_at, local_receipt_time)``.  Positive offset: the remote clock is ahead."""
    pairs = [(r, l) for r, l in pairs if _finite(r) and _finite(l)]
    if not pairs:
        return {"available": False, "reason": "no signed message received from this peer yet"}
    best = max(r - l for r, l in pairs)
    corrected = _finite(rtt_s) and rtt_s >= 0
    offset = best + (rtt_s / 2.0 if corrected else 0.0)
    uncertainty = rtt_s / 2.0 if corrected else None
    if abs(offset) < max(IN_SYNC_SECONDS, uncertainty or 0.0):
        direction = "in_sync"
    else:
        direction = "ahead" if offset > 0 else "behind"
    return {"available": True, "offset_s": round(offset, 3),
            "uncertainty_s": None if uncertainty is None else round(uncertainty, 3),
            "rtt_s": round(rtt_s, 4) if corrected else None, "rtt_corrected": bool(corrected),
            "direction": direction, "samples": len(pairs), "confidence": "ok" if len(pairs) >= 3 else "low",
            "basis": "signed message timestamps against receipt time, minimum-delay sample"}


class ClockSamples:
    def __init__(self, *, clock: Callable[[], float] = time.time, window_s: float = WINDOW_SECONDS,
                 maxlen: int = MAX_SAMPLES) -> None:
        self._clock = clock
        self._window = float(window_s)
        self._maxlen = int(maxlen)
        self._by_peer: dict[str, deque[tuple[float, float, float | None]]] = {}
        self._lock = threading.Lock()

    def add(self, peer: str, *, remote_ts: float, local_ts: float, rtt_s: float | None = None) -> None:
        if not isinstance(peer, str) or not _finite(remote_ts) or not _finite(local_ts):
            return
        with self._lock:
            queue = self._by_peer.setdefault(peer, deque(maxlen=self._maxlen))
            queue.append((float(remote_ts), float(local_ts), float(rtt_s) if _finite(rtt_s) else None))

    def _live(self, peer: str) -> list[tuple[float, float, float | None]]:
        now = self._clock()
        with self._lock:
            queue = self._by_peer.get(peer)
            if not queue:
                return []
            while queue and now - queue[0][1] > self._window:
                queue.popleft()
            return list(queue)

    def peers(self) -> list[str]:
        with self._lock:
            names = list(self._by_peer)
        return [name for name in names if self._live(name)]

    def estimate(self, peer: str, rtt_s: float | None = None) -> dict[str, Any]:
        live = self._live(peer)
        if rtt_s is None:
            seen = [s[2] for s in live if s[2] is not None]
            rtt_s = min(seen) if seen else None
        return estimate_offset([(r, l) for r, l, _ in live], rtt_s=rtt_s)

    def last_sample_age(self, peer: str) -> float | None:
        live = self._live(peer)
        if not live:
            return None
        return round(max(0.0, self._clock() - max(l for _, l, _ in live)), 3)


# -- owner side: signed poll/ack requests from members ----------------------------------------------------

def install_mailbox_probe(mailbox: Any, state: Any) -> None:
    """Record ``(member issued_at, receipt time)`` for every poll/ack the owner mailbox ACCEPTS."""
    if getattr(mailbox, "_diag_probe_state", None) is state:
        return
    base = getattr(mailbox, "_diag_original_handle_http", None) or mailbox.handle_http

    def handle_http(method: str, path: str, body: Mapping[str, Any]) -> Any:
        try:
            received = float(mailbox._clock())
        except Exception:
            received = time.time()
        result = base(method, path, body)  # a refused request raises: no sample
        if path in (_POLL, _ACK):
            try:
                statement = body["statement"]
                node, issued = statement["target_node_id"], statement["issued_at"]
                state.clock.add(node, remote_ts=issued, local_ts=received)
            except Exception:
                pass
        return result

    mailbox._diag_original_handle_http = base
    mailbox.handle_http = handle_http
    mailbox._diag_probe_state = state


# -- member side: the owner's signed responses --------------------------------------------------------------

_client_probe: dict[str, Any] = {"state": None, "originals": None}
_client_lock = threading.Lock()


def install_client_probe(state: Any) -> None:
    from mycelium_a13_macos import managed_control as mc
    with _client_lock:
        _client_probe["state"] = state
        if _client_probe["originals"] is not None:
            return
        cls = mc.ManagedControlClient
        originals = {"_request": cls._request, "poll": cls.poll, "ack": cls.ack}
        _client_probe["originals"] = originals

        def _request(self: Any, path: str, envelope: Mapping[str, Any]) -> Any:
            target = _client_probe["state"]
            if target is None:
                return originals["_request"](self, path, envelope)
            began = time.monotonic()
            response = originals["_request"](self, path, envelope)
            elapsed = time.monotonic() - began
            try:
                issued = response["statement"]["issued_at"]
                target.clock.add(self.owner_node_id, remote_ts=issued, local_ts=self._clock(), rtt_s=elapsed)
            except Exception:
                pass
            return response

        def _timed(kind: str) -> Callable[..., Any]:
            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                target = _client_probe["state"]
                if target is None:
                    return originals[kind](self, *args, **kwargs)
                began = time.monotonic()
                code: str | None = None
                result = None
                try:
                    result = originals[kind](self, *args, **kwargs)
                    return result
                except mc.ManagedControlError as error:
                    code = str(error.code)
                    raise
                except BaseException:
                    code = "managed_control_unexpected_error"
                    raise
                finally:
                    command = _command_facts(self, result) if kind == "poll" else None
                    target.client.record(kind, time.monotonic() - began, code, at=time.time(), command=command)
            wrapper.__name__ = kind
            return wrapper

        cls._request = _request
        cls.poll = _timed("poll")
        cls.ack = _timed("ack")


def _command_facts(client: Any, delivery: Any) -> dict[str, Any] | None:
    try:
        if delivery["delivery_kind"] != "command":
            return None
        statement = delivery["command_envelope"]["statement"]
        issued, expires = float(statement["issued_at"]), float(statement["expires_at"])
        return {"ttl_seconds": round(expires - issued, 3),
                "remaining_on_this_clock_seconds": round(expires - float(client._clock()), 3)}
    except Exception:
        return None


def uninstall_client_probe() -> None:
    from mycelium_a13_macos import managed_control as mc
    with _client_lock:
        originals = _client_probe["originals"]
        _client_probe["state"] = None
        _client_probe["originals"] = None
        if originals is None:
            return
        cls = mc.ManagedControlClient
        cls._request, cls.poll, cls.ack = originals["_request"], originals["poll"], originals["ack"]
