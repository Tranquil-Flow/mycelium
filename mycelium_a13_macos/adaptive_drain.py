# SPDX-License-Identifier: AGPL-3.0-only
"""A16 drain-to-withdraw wiring over the real capacity ledger and managed runtime.

The deterministic controller (``adaptive_host``) decides *when* to drain and withdraw; this module
connects those decisions to what is actually running on the target:

* **in flight** = the live (``RESERVED``/``COMMITTED``) reservations in the node's own capacity
  ledger (``<run root>/<deployment>.capacity.sqlite3``), read read-only.  A ledger that cannot be
  read counts as *not drained*, never as zero.
* **footprint** = the worker process group's resident bytes (numbers only).
* **unload** = ``ManagedTargetRuntime.unload_model()`` (exact worker close with cleanup proof),
  attempted only when no request is in flight.  The controller reaches ``withdraw`` only after
  ``mark_cleanup_complete``.

Nothing here truncates a running decode batch: dispositions are decisions per request through
``inflight_disposition``; the terminal and handoff actions are injected hooks owned by the A4/A6
gateway.  A missing hook is reported (``abort_unavailable``) and blocks the unload; it never
fabricates a terminal or a success.
"""
from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from .adaptive_host import AdaptiveHostController, inflight_disposition

LEDGER_SUFFIX = ".capacity.sqlite3"
_LIVE = ("RESERVED", "COMMITTED")


@dataclass(frozen=True)
class DrainHooks:
    """Gateway-owned seams.  All optional; absent hooks never invent an outcome."""

    cancelled: Callable[[Mapping[str, Any]], bool] | None = None
    successor: Callable[[Mapping[str, Any]], Mapping[str, bool] | None] | None = None
    handoff: Callable[[Mapping[str, Any]], bool] | None = None
    abort: Callable[[Mapping[str, Any]], bool] | None = None


class _Unobservable(Exception):
    pass


ABORT_DEADLINE_S = 2.0  # one bounded wait per abort command; never extends a request deadline
_MAX_ABORTED = 256


def _cancel_ended_request(response: Any) -> bool:
    """True only when the node reports it moved the request into its terminal CANCELLED state."""
    try:
        if response["ok"] is not True:
            return False
        result = response["result"]["observation"]["details"]["result"]
        return result["cancelled"] is True
    except (KeyError, TypeError):
        return False


def product_hooks(runtime: Any) -> DrainHooks:
    """Hooks over seams that already exist in the product.

    * ``abort`` (A4): the node's existing ``cancel`` worker command (the entry router's
      cancellation, the same command the owner control pump delivers), one bounded call per
      request.  Success = the node reports it cancelled that request; a request this node
      cannot cancel (not entry-owned, unknown, already terminal elsewhere) is *not* a success,
      so the unload keeps blocking exactly as without a hook.  A request once ended is
      remembered (its ledger row is released by the normal path teardown) and not re-sent.
    * ``handoff``/``successor`` (A6): intentionally absent.  A6 recovery needs the owner-side
      authority (grants, replay checkpoint, qualified successor binding); the target has no
      seam to start or prove one, so it must not claim one.  ``cancelled`` is absent as well:
      owner cancellation already reaches the ledger through the ordinary path release.
    """
    ended: dict[str, None] = {}

    def abort(row: Mapping[str, Any]) -> bool:
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return False
        if request_id in ended:
            return True
        try:
            response = runtime.worker.send_before(
                command_id="a16-abort-" + uuid.uuid4().hex,
                command="cancel",
                payload={"request_id": request_id},
                deadline_monotonic_s=time.monotonic() + ABORT_DEADLINE_S,
            )
        except Exception:
            return False
        if not _cancel_ended_request(response):
            return False
        ended[request_id] = None
        while len(ended) > _MAX_ABORTED:
            ended.pop(next(iter(ended)))
        return True

    return DrainHooks(abort=abort)


def target_runtime(app: Any) -> Any | None:
    """The attached managed target runtime of ``app``, or ``None`` (owner role / not running)."""
    return getattr(getattr(app, "authority", None), "_runtime", None)


def live_reservations(root: str | Path) -> list[dict[str, Any]]:
    """Live reservations of the node ledger in ``root`` (read-only); raises when unobservable."""
    paths = sorted(Path(root).glob("*" + LEDGER_SUFFIX))
    if not paths:
        raise _Unobservable()
    rows: list[dict[str, Any]] = []
    try:
        for path in paths:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
            try:
                connection.row_factory = sqlite3.Row
                for row in connection.execute(
                    "SELECT reservation_id, request_id, kv_bytes, lease_expires_at, status "
                    "FROM reservations WHERE status IN (?, ?) ORDER BY reservation_id", _LIVE,
                ):
                    rows.append(dict(row))
            finally:
                connection.close()
    except sqlite3.Error as error:
        raise _Unobservable() from error
    return rows


def inflight_count(runtime: Any | None) -> int:
    if runtime is None:
        return 0
    try:
        return len(live_reservations(runtime.root))
    except Exception:
        return 1  # cannot prove the host is drained


def footprint_bytes(runtime: Any | None) -> int:
    if runtime is None:
        return 0
    try:
        value = int(runtime.worker.resident_bytes())
    except Exception:
        return int(getattr(runtime, "_a16_last_footprint", 0))  # last good value, never invented
    try:
        runtime._a16_last_footprint = value
    except AttributeError:
        pass
    return value


def drain_step(
    controller: AdaptiveHostController,
    runtime: Any,
    *,
    wall_clock: Callable[[], float] = time.time,
    hooks: DrainHooks | None = None,
) -> dict[str, Any]:
    """One drain/withdraw tick: decide per in-flight request, unload only when none remain."""
    if not controller.enabled or controller.status()["mode"] not in ("drain", "withdraw"):
        return {"action": "none"}
    hooks = hooks or DrainHooks()
    mode = controller.status()["mode"]
    report: dict[str, Any] = {"action": "drain", "inflight": 0, "dispositions": {},
                              "abort_unavailable": 0, "handoff_refused": 0, "unloaded": False}
    try:
        rows = live_reservations(runtime.root)
    except Exception:
        report["unobservable"] = True
        return report
    report["inflight"] = len(rows)
    now = wall_clock()
    for row in rows:
        successor = hooks.successor(row) if hooks.successor is not None else None
        disposition = inflight_disposition(
            mode=mode, deadline_remaining_s=row["lease_expires_at"] - now,
            cancelled=bool(hooks.cancelled(row)) if hooks.cancelled is not None else False,
            successor=successor, can_finish=row["lease_expires_at"] > now,
            critical=controller.critical,
        )
        counts = report["dispositions"]
        counts[disposition] = counts.get(disposition, 0) + 1
        if disposition == "a6_handoff":
            if hooks.handoff is None or not hooks.handoff(row):
                report["handoff_refused"] += 1
        elif disposition == "abort_terminal":
            if hooks.abort is None or not hooks.abort(row):
                report["abort_unavailable"] += 1
    if getattr(runtime, "model_unloaded", False):
        controller.mark_cleanup_complete()
        report["unloaded"] = True
        return report
    try:
        remaining = len(live_reservations(runtime.root))
    except Exception:
        return report
    if remaining == 0:
        try:
            unloaded = bool(runtime.unload_model())
        except Exception:
            unloaded = False
        report["unloaded"] = unloaded
        if unloaded:
            controller.mark_cleanup_complete()
    return report
