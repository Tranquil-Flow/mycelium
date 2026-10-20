# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV9 owner surface: Interactive / Batch-overnight service mode, the batch job queue, and its status.

The mode is the owner's choice (``service-mode.json`` in the app state directory).  Choosing it also
publishes the same file into every local node's private capacity root, where the node applies it to
its relay before the next request (``PhysicalNodeService._refresh_service_mode``).  Queued batch jobs
run only while Batch mode is on, the route serves, no chat is running and Adaptive sharing (and the
A13 power/heat/network rules) allow new work; interactive requests always go first.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from mycelium_router import service_modes as sm

from .job_queue import JobQueue, JobQueueError

MODE_ROUTES = frozenset({"/api/service-mode", "/api/batch/submit", "/api/batch/cancel",
                         "/api/batch/jobs", "/api/batch/result"})
_JOB_ROWS_IN_STATUS = 20
_lock = threading.Lock()


def _error(code: str):
    from .app import DemoError
    return DemoError(code)


def current_mode(app: Any) -> str:
    return sm.read_service_mode(app.root)


def queue_for(app: Any) -> JobQueue:
    with _lock:
        queue = getattr(app, "_job_queue", None)
        if queue is None:
            queue = JobQueue(Path(app.root) / "batch-jobs")
            try:
                app._job_queue = queue
            except AttributeError:
                pass
        return queue


def _adaptive_line_and_paused(app: Any) -> tuple[str, bool]:
    from .adaptive_owner import adaptive_status
    status = adaptive_status(app)
    paused = status.get("state") == "error" or (
        bool(status.get("enabled")) and (status.get("mode") != "normal" or status.get("stopped")
                                         or status.get("new_admissions_allowed") is False))
    return status.get("line", "Adaptive sharing: off"), paused


def sharing_hold(app: Any, *, fresh: bool = False) -> tuple[bool, str | None]:
    """Whether Adaptive sharing and the A13 power/heat/network rules allow new work now: ``(allowed, line)``."""
    line, paused = _adaptive_line_and_paused(app)
    if fresh:
        violation = app._resource_check()
    else:
        from .app import read_resource_policy
        from .resources import UNKNOWN_OBSERVATION, policy_violation
        observation = getattr(app, "resource_observation", None) or {}
        try:  # a not-yet-observed Mac is not a violation: the next job start observes it for real
            violation = None if observation == UNKNOWN_OBSERVATION else policy_violation(
                read_resource_policy(app.root) or {}, observation)
        except Exception:  # noqa: BLE001 - status never fails on a policy read
            violation = None
    if paused:
        return False, line
    if violation:
        return False, "Sharing is limited by your power, heat or network rules"
    return True, None


def batch_hold(app: Any, *, fresh: bool = False) -> tuple[str | None, str | None]:
    """Why queued batch jobs are not running now (``(code, line)``), or ``(None, None)``."""
    from .chat import service as chat_service
    if current_mode(app) != sm.BATCH:
        return "interactive_mode", None
    allowed, line = sharing_hold(app, fresh=fresh)
    if not allowed:
        return "sharing_not_allowed", line
    chat = chat_service(app)
    if chat._serving() is None:
        return "route_not_ready", None
    if chat._busy is not None:
        return "interactive_request_running", None
    return None, None


def service_mode_status(app: Any) -> dict[str, Any]:
    mode = current_mode(app)
    queue = queue_for(app)
    listed = queue.list(_JOB_ROWS_IN_STATUS)
    code, line = batch_hold(app)
    return {**sm.describe(mode), "queue": listed, "batch_hold": code, "batch_hold_line": line}


def _apply_to_local_nodes(app: Any, mode: str) -> int:
    from . import adaptive_host as ah
    applied = 0
    try:
        roots = ah.capacity_roots(app)
    except Exception:  # noqa: BLE001 - no local node yet: the app file still holds the choice
        roots = []
    for root in roots:
        try:
            sm.write_service_mode(root, mode)
            applied += 1
        except OSError:
            continue
    return applied


def _exact(body: Any, keys: set[str], optional: frozenset[str] | set[str] = frozenset()) -> dict[str, Any]:
    if not isinstance(body, dict) or not keys <= set(body) <= keys | set(optional):
        raise _error("invalid_request")
    return body


def service_mode_action(app: Any, path: str, body: Any) -> dict[str, Any]:
    queue = queue_for(app)
    if path != "/api/service-mode" and getattr(app, "installation_role", "owner") != "owner":
        raise _error("owner_role_required")
    try:
        if path == "/api/service-mode":
            mode = _exact(body, {"mode"})["mode"]
            try:
                sm.knobs_for(mode)
            except ValueError:
                raise _error("service_mode_invalid") from None
            sm.write_service_mode(app.root, mode)
            return {**service_mode_status(app), "applied_to_local_nodes": _apply_to_local_nodes(app, mode)}
        if path == "/api/batch/submit":
            body = _exact(body, {"prompt", "max_new_tokens"}, {"priority"})
            priority = body.get("priority", "batch")
            if priority == "batch" and current_mode(app) != sm.BATCH:
                raise _error("service_mode_batch_required")
            return queue.submit(body["prompt"], body["max_new_tokens"], priority=priority)
        if path == "/api/batch/cancel":
            job_id = _exact(body, {"id"})["id"]
            return {"id": job_id, "state": queue.cancel(job_id)}
        if path == "/api/batch/jobs":
            if body != {}:
                raise _error("invalid_request")
            return queue.list()
        if path == "/api/batch/result":
            job_id = _exact(body, {"id"})["id"]
            job = queue.get(job_id)
            return {"id": job["id"], "state": job["state"], "result": job["result"], "error": job["error"],
                    "priority": job["priority"]}
    except JobQueueError as error:
        raise _error(error.code) from None
    raise _error("unknown_action")


def _spawn(app: Any, fn) -> None:
    spawn = getattr(app, "_batch_spawn", None)
    if spawn is not None:
        spawn(fn)
        return
    threading.Thread(target=fn, name="a13-batch-job", daemon=True).start()


def _client_factory(app: Any):
    factory = getattr(app, "_batch_client_factory", None)
    if factory is None:
        from .chat import RegistryClient
        factory = RegistryClient
    return factory


def service_tick(app: Any) -> int:
    """Start at most one queued job that may run now; returns how many were started (0 or 1)."""
    queue = queue_for(app)
    counts = queue.counts()
    if not counts["queued"]:
        return 0
    from .chat import service as chat_service
    chat = chat_service(app)
    serving = chat._serving()
    if serving is None:
        return 0
    knobs = sm.knobs_for(current_mode(app))
    allowed, _ = sharing_hold(app, fresh=True)
    if not allowed:  # nothing starts, interactive or batch, while this Mac is not sharing
        return 0
    hold, _ = batch_hold(app, fresh=True)
    batch_allowed = hold is None
    job = queue.next_runnable(batch_allowed=batch_allowed, interactive_busy=chat._busy is not None,
                              in_flight=counts["running"], concurrency=knobs.batch_job_concurrency)
    if job is None:
        return 0
    origin = serving["registry_origin"]
    queue.mark_running(job["id"])
    qos = job["priority"]
    client = _client_factory(app)(origin)

    def work() -> None:
        pieces: list[str] = []

        def token(text: str) -> None:
            pieces.append(text)
            queue.append_tokens(job["id"], [text])

        try:
            result = client.stream(job["prompt"], job["max_new_tokens"], token, qos_class=qos)
        except Exception as error:  # noqa: BLE001 - only a bounded code is kept
            from .chat import failure_code
            queue.fail(job["id"], failure_code(error))
            return
        terminal = result.get("terminal")
        if terminal == "completed":
            queue.complete(job["id"], "".join(pieces))
        elif terminal == "cancelled":
            queue.cancel(job["id"])
        else:
            queue.fail(job["id"], result.get("code") or "chat_generation_failed")

    _spawn(app, work)
    return 1
