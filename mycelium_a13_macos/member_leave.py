# SPDX-License-Identifier: AGPL-3.0-or-later
"""Member-side leave for a joined Mac: ``Leave smoothly`` and ``Instant leave``.

* **Instant leave** = what happens when the internet drops, done on purpose and completely: stop
  admitting new work, withdraw the standing consent, disconnect (close the managed runtime, stop
  renewing the membership lease, turn contribution off) and cancel any download.  Every step runs
  even when an earlier one failed; the swarm adapts through its existing loss handling.
* **Leave smoothly** = stop admitting new work, tell the owner (signed ``planning`` notice over the member channel,
  see ``member_withdrawal``) which requests are still running here so the owner can use its existing A7 KV-standby /
  A6 replay path instead of waiting for loss detection, wait for the owner's answer (bounded by
  ``OWNER_ACK_TIMEOUT_S``), wait (bounded) for the requests to finish, unload the model with cleanup proof (A16
  withdraw), publish the withdrawal when the adaptive controller is on, withdraw the consent, send one signed
  ``final`` withdrawal (``drained``, or ``forced`` after a fallback) and only then disconnect.  If the in-flight count
  has not shrunk for ``LEAVE_SMOOTH_STALL_S`` (or the whole drain exceeds ``LEAVE_SMOOTH_CAP_S``) it falls back to an
  instant leave and says so (the final then says ``forced``).

The state is persisted (``leave-state.json``) so a restart finishes a leave that was interrupted (no final: the
owner sees a Mac that vanished).  The signed final lets the owner record "left smoothly" and self-revoke that
generation; ``Leave now`` sends nothing, so the owner sees what it sees when the internet drops.  Either way the
peer records a voluntary leave so the Mac can ask for a new pairing later (same node, new generation).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

PROTOCOL = "mycelium.a13_member_leave.v1"
MODES = ("smooth", "instant")
LEAVE_SMOOTH_STALL_S = 30
LEAVE_SMOOTH_CAP_S = 120
OWNER_ACK_TIMEOUT_S = 5.0     # how long a leave waits for the owner's answer to a signed notice
POLL_S = 0.5
_FILE = "leave-state.json"
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_STATES = frozenset({"leaving", "draining", "handing_off", "withdrawing", "left"})
_FALLBACKS = frozenset({None, "stall", "cap", "restart"})


class LeaveError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class LeaveSteps:
    """The seams a leave drives.  ``handoff`` is ``None`` where no clean handoff exists."""

    fence: Callable[[], Any]               # stop admitting new work
    inflight: Callable[[], int]            # requests still running here
    handoff: Callable[[], bool] | None     # hand running work to a successor (optional)
    unload: Callable[[], Any]              # unload the model with cleanup proof
    publish_withdrawal: Callable[[], Any]  # publish the zero-capacity / withdraw state
    withdraw_consent: Callable[[], Any]
    disconnect: Callable[[], Any]
    cancel_work: Callable[[], Any]
    #: Signed final withdrawal (``outcome`` = ``drained`` | ``forced``); ``None`` where no owner channel exists.
    announce_withdrawal: Callable[[str], Any] | None = None


def bounded(call: Callable[[], Any], seconds: float) -> Any:
    """Run ``call`` for at most ``seconds``; a timeout or failure is a ``LeaveError`` with a stable code."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:  # noqa: BLE001 - carried to the caller
            outcome["error"] = error

    worker = threading.Thread(target=run, daemon=True, name="member-leave-owner-notice")
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        raise LeaveError("owner_ack_timeout")
    error = outcome.get("error")
    if error is not None:
        code = getattr(error, "code", None)
        if type(error).__module__.startswith("mycelium_") and isinstance(code, str) and _CODE.fullmatch(code):
            raise LeaveError(code) from None
        raise LeaveError("owner_notice_failed") from None
    return outcome.get("value")


def _write_private(path: Path, document: dict[str, Any]) -> None:
    temp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(json.dumps(document, sort_keys=True, separators=(",", ":")))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _load(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("protocol") != PROTOCOL
            or value.get("state") not in _STATES or value.get("mode") not in MODES
            or value.get("fallback") not in _FALLBACKS or not isinstance(value.get("steps"), list)):
        return None
    return value


class MemberLeave:
    def __init__(self, root: Path | str, steps: LeaveSteps, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 wall: Callable[[], float] = time.time,
                 stall_s: float = LEAVE_SMOOTH_STALL_S, cap_s: float = LEAVE_SMOOTH_CAP_S,
                 poll_s: float = POLL_S) -> None:
        self._path = Path(root) / _FILE
        self._steps = steps
        self._clock, self._sleep, self._wall = clock, sleep, wall
        self._stall, self._cap, self._poll = stall_s, cap_s, poll_s
        self._lock = threading.Lock()

    # -- persisted state ------------------------------------------------------
    def status(self) -> dict[str, Any] | None:
        return _load(self._path)

    def _save(self, record: dict[str, Any]) -> None:
        with self._lock:
            _write_private(self._path, record)

    # -- entry points -----------------------------------------------------------
    def begin(self, mode: str, *, fallback: str | None = None) -> dict[str, Any]:
        """Validate and persist the start of a leave (the caller then ``execute``\\ s it)."""
        if mode not in MODES:
            raise LeaveError("leave_mode_invalid")
        record = {"protocol": PROTOCOL, "mode": mode,
                  "state": "draining" if mode == "smooth" and fallback is None else "leaving",
                  "started_at": self._wall(), "fallback": fallback, "steps": [],
                  "handoff": "unavailable"}
        self._save(record)
        return record

    def run(self, mode: str) -> dict[str, Any]:
        return self.execute(self.begin(mode))

    def resume(self) -> dict[str, Any] | None:
        """Finish an interrupted leave after a restart (instant: nothing is in flight any more)."""
        record = self.status()
        if record is None or record["state"] == "left":
            return None
        return self.execute(self.begin("instant", fallback="restart"))

    def execute(self, record: dict[str, Any]) -> dict[str, Any]:
        self._step(record, "fence", self._steps.fence)
        if record["mode"] == "smooth" and record["fallback"] is None:
            self._smooth(record)
        if record["fallback"] is None and record["mode"] == "smooth":
            record["state"] = "withdrawing"
            self._save(record)
            self._step(record, "unload", self._steps.unload)
            self._step(record, "publish_withdrawal", self._steps.publish_withdrawal)
        # Privacy order: these always run, whatever failed before.
        self._step(record, "withdraw_consent", self._steps.withdraw_consent)
        if record["mode"] == "smooth" and self._steps.announce_withdrawal is not None:
            outcome = "drained" if record["fallback"] is None else "forced"
            announce = self._steps.announce_withdrawal
            self._step(record, "announce_withdrawal", lambda: announce(outcome))
        self._step(record, "disconnect", self._steps.disconnect)
        self._step(record, "cancel_work", self._steps.cancel_work)
        record["state"] = "left"
        record["finished_at"] = self._wall()
        self._save(record)
        return record

    # -- internals ------------------------------------------------------------------
    def _step(self, record: dict[str, Any], name: str, action: Callable[[], Any]) -> bool:
        try:
            action()
            row = {"name": name, "ok": True, "code": None}
        except Exception as error:  # noqa: BLE001 - recorded; the remaining steps still run
            code = getattr(error, "code", None)
            row = {"name": name, "ok": False,
                   "code": code if isinstance(code, str) and _CODE.fullmatch(code) else "step_failed"}
        record["steps"].append(row)
        return row["ok"]

    def _smooth(self, record: dict[str, Any]) -> None:
        if self._steps.handoff is not None:
            record["state"] = "handing_off"
            self._save(record)
            try:
                record["handoff"] = "done" if self._steps.handoff() else "refused"
            except Exception:  # noqa: BLE001 - in-flight work then finishes within the bound
                record["handoff"] = "failed"
        record["state"] = "draining"
        self._save(record)
        start = self._clock()
        last_progress, best = start, None
        while True:
            try:
                count = int(self._steps.inflight())
            except Exception:  # noqa: BLE001 - cannot prove it drained: count as unchanged
                count = best if best is not None else 1
            if count <= 0:
                return
            now = self._clock()
            if best is None or count < best:
                best, last_progress = count, now
            if now - last_progress >= self._stall:
                record["fallback"] = "stall"
                break
            if now - start >= self._cap:
                record["fallback"] = "cap"
                break
            self._sleep(self._poll)
        record["state"] = "leaving"
        self._save(record)


# ---------------------------------------------------------------- application adapter

def app_steps(app: Any) -> LeaveSteps:
    """Seams over a joined ``DemoApp`` (target role).

    ``handoff`` is the member-side seam for the owner's A7 KV-standby / A6 replay recovery: the A7 successor
    hand-off and the A6 recovery need the owner-side authority (grants, replay checkpoint, qualified successor
    binding), which a member cannot start or prove itself (see ``adaptive_drain.product_hooks``).  What it can do
    is tell the owner, signed, which requests are running here; the owner decides (``leave_handoff``) and the
    answer is kept as ``handoff_decisions``.  Requests the owner did not take finish here within the bound.
    """
    from . import adaptive_drain

    def guarded(function: Callable[..., Any]) -> Callable[..., Any]:
        def call(*args: Any) -> Any:
            with app.guard:
                return function(*args)
        return call

    def controller() -> Any:
        runtime = getattr(app, "_adaptive_runtime", None)
        controller = getattr(runtime, "controller", None)
        return controller if controller is not None and controller.enabled else None

    def fence() -> None:
        app._leave_fence = True   # DemoApp._resource_check refuses with the existing ``owner_stop``
        adaptive = controller()
        if adaptive is not None:
            adaptive.stop_serving()   # A16: drain/withdraw publication through the existing machinery

    def inflight() -> int:
        return adaptive_drain.inflight_count(adaptive_drain.target_runtime(app))

    def client() -> Any:
        injected = getattr(app, "_leave_client", None)
        if injected is not None:
            return injected
        from .member_withdrawal import MemberWithdrawalClient
        return MemberWithdrawalClient(app.authority.peer)

    def notice(phase: str, in_flight: list[str], outcome: str | None) -> dict[str, Any]:
        from .member_withdrawal import NOTICE
        return {"protocol": NOTICE, "phase": phase, "issued_at": time.time(),
                "generation": int(app.authority.member.generation), "in_flight": in_flight, "outcome": outcome}

    def running_requests() -> tuple[list[str], bool]:
        """(ids safe to name, whether the ledger could be read and every id fit the bound)."""
        from .member_withdrawal import MAX_IN_FLIGHT, valid_request_id
        runtime = adaptive_drain.target_runtime(app)
        if runtime is None:
            return [], True
        try:
            rows = adaptive_drain.live_reservations(runtime.root)
        except Exception:  # noqa: BLE001 - cannot prove what is running
            return [], False
        ids: list[str] = []
        complete = True
        for row in rows:
            request_id = row.get("request_id")
            if not valid_request_id(request_id) or request_id in ids or len(ids) >= MAX_IN_FLIGHT:
                complete = False
                continue
            ids.append(request_id)
        return ids, complete

    def handoff() -> bool:
        from .member_withdrawal import HANDLED
        ids, complete = running_requests()
        ack = bounded(lambda: client().send(notice("planning", ids, None)), OWNER_ACK_TIMEOUT_S)
        decisions = dict(ack.get("handoff", {}))
        app._leave_handoff_decisions = decisions
        return complete and all(decisions.get(request_id) in HANDLED for request_id in ids)

    def announce_withdrawal(outcome: str) -> None:
        bounded(lambda: client().send(notice("final", [], outcome)), OWNER_ACK_TIMEOUT_S)

    def unload() -> None:
        runtime = adaptive_drain.target_runtime(app)
        if runtime is not None and not getattr(runtime, "model_unloaded", False):
            if not runtime.unload_model():
                raise LeaveError("leave_unload_failed")

    def publish_withdrawal() -> None:
        adaptive = controller()
        if adaptive is not None:
            adaptive.mark_cleanup_complete()   # the zero-capacity overlay / withdraw marker

    def withdraw_consent() -> None:
        from .standing_consent import withdraw_standing_consent
        withdraw_standing_consent(app.root, now=time.time())   # None (never granted) is fine

    def disconnect() -> None:
        app._left = True   # the renewal loop skips a left Mac, so the lease ages out
        failure: Exception | None = None
        try:
            app.authority.stop_control()
        except Exception as error:  # noqa: BLE001
            failure = error
        member = getattr(app.authority, "member", None)
        try:
            if member is not None and member.generation is not None:
                app.authority.peer.set_contribution_enabled(
                    enabled=False, expected_generation=member.generation)
        except Exception as error:  # noqa: BLE001
            failure = failure or error
        try:
            app.authority.peer.record_voluntary_leave()   # last: opens the re-join path (same node, new generation)
        except Exception as error:  # noqa: BLE001
            failure = failure or error
        if failure is not None:
            raise failure

    def cancel_work() -> None:
        app._cancel_acquisition()

    return LeaveSteps(
        fence=guarded(fence), inflight=guarded(inflight), handoff=guarded(handoff), unload=guarded(unload),
        publish_withdrawal=guarded(publish_withdrawal), withdraw_consent=guarded(withdraw_consent),
        disconnect=guarded(disconnect), cancel_work=guarded(cancel_work),
        announce_withdrawal=guarded(announce_withdrawal))


def _leave_for(app: Any) -> MemberLeave:
    leave = getattr(app, "_leave", None)
    if leave is None:
        leave = app._leave = MemberLeave(app.root, app_steps(app))
    return leave


def leave_status(app: Any) -> dict[str, Any] | None:
    record = _leave_for(app).status()
    if record is None:
        return None
    view = {key: record[key] for key in ("mode", "state", "fallback", "handoff", "steps") if key in record}
    decisions = getattr(app, "_leave_handoff_decisions", None)
    if isinstance(decisions, dict):
        view["handoff_decisions"] = dict(decisions)    # what the owner did with each running request
    return view


def leave_action(app: Any, body: Any) -> dict[str, Any]:
    """``POST /api/leave {mode: smooth|instant, confirm: true}`` (joined Mac only)."""
    from .app import DemoError
    if app.installation_role != "target":
        raise DemoError("target_role_required")
    if not isinstance(body, dict) or "mode" not in body or not set(body) <= {"mode", "confirm"}:
        raise DemoError("invalid_request")
    if body.get("confirm") is not True:
        raise DemoError("leave_confirmation_required")
    mode = body["mode"]
    if mode not in MODES:
        raise DemoError("leave_mode_invalid")
    thread = getattr(app, "_leave_thread", None)
    if thread is not None and thread.is_alive():
        return leave_status(app)          # a leave is already running
    leave = _leave_for(app)
    if mode == "instant":
        leave.run("instant")              # synchronous: complete when the answer arrives
        return leave_status(app)
    record = leave.begin("smooth")
    app._leave_fence = True               # new work stops now, before the thread's first tick
    thread = threading.Thread(target=leave.execute, args=(record,), daemon=True, name="member-leave")
    app._leave_thread = thread
    thread.start()
    return leave_status(app)


def resume_leave(app: Any) -> dict[str, Any] | None:
    """On start: finish an interrupted leave; keep a completed one fenced (no renewal, no work)."""
    leave = _leave_for(app)
    record = leave.status()
    if record is None:
        return None
    if record["state"] == "left":
        app._leave_fence = True
        app._left = True
        return record
    app._leave_fence = True
    return leave.resume()


def clear_leave(app: Any) -> None:
    """A new membership (re-join after a leave) ends the finished leave: no fence, no stopped renewal."""
    record = _leave_for(app).status()
    if record is not None and record["state"] != "left":
        return                            # a leave still running is not undone by a join
    (Path(app.root) / _FILE).unlink(missing_ok=True)
    app._leave_fence = False
    app._left = False
    app._leave = None
    app._leave_thread = None
    app._leave_handoff_decisions = None
