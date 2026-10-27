# SPDX-License-Identifier: AGPL-3.0-only
"""Owner one-click demo run: Start, Stop and Rebuild with visible progress.

``OwnerRun`` only sequences steps and owns the state machine; the real work is
done by a *chain* object (``demo_chain.AppDemoChain`` in the app, a fake in
tests).  The chain must provide::

    run_step(name, ctx)          # block until step ``name`` is done or raise
    teardown(ctx, *, failed)     # undo whatever this run started (best effort)
    monitor(ctx) -> str | None   # optional: a closed error code once serving
    ceiling_reached(ctx) -> str | None   # optional, cheap: the execution-ceiling code once the wall allowance is spent

Steps run in ``STEP_NAMES`` order.  A step fails with ``OwnerRunError(code)``
(a closed ``[a-z][a-z0-9_]*`` code); any other exception is reduced to
``owner_run_step_failed`` so no exception text reaches the UI.  Stop interrupts
the waiting step (``ctx.check_stopped`` / ``ctx.wait_until``), then tears down.
Rebuild = Stop, then Start a fresh run whose ``ctx.reason`` is ``'rebuild'``
(the compile step passes that to the chain-compiler interface).
"""
from __future__ import annotations

from datetime import datetime, timezone
import re
import secrets
import threading
import time
import uuid
from typing import Any, Callable

STEPS: tuple[tuple[str, str], ...] = (
    ('model', 'Model ready on this Mac'),
    ('contributor', 'This Mac joins the run'),
    ('descriptors', 'Joined Macs report in'),
    ('observe', 'Native hello on every Mac'),
    ('compile', 'Compile the route over the current members'),
    ('slices', 'Every Mac fetches only its own slice'),
    ('publish', 'Publish work to every Mac'),
    ('targets', 'Every Mac accepts and starts its slice'),
    ('activate', 'Activate and qualify the route'),
    ('serving', 'Serving prompts'),
)
STEP_NAMES = tuple(name for name, _ in STEPS)
#: The closed code of a run the owner ended because the workers' wall allowance (``max_wall_seconds``) is spent.
EXECUTION_CEILING_CODE = 'execution_ceiling_reached'
_CODE = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')
# 'failing' too: a failed run is still tearing down (announcing 'stopped',
# ticking this Mac's contributor) until it settles on 'failed'.
_ACTIVE = frozenset({'running', 'serving', 'stopping', 'failing'})


class OwnerRunError(RuntimeError):
    """A closed ``code`` plus, for resource refusals, a human ``message`` with numbers."""

    def __init__(self, code: str, message: str | None = None, fix: dict[str, str] | None = None) -> None:
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else 'owner_run_step_failed'
        self.message = message[:600] if isinstance(message, str) and message else None
        self.fix = dict(fix) if isinstance(fix, dict) else None  # what to do next (a pointer, never a secret)
        super().__init__(self.code)


class _Stopped(BaseException):
    """Raised inside a step when the owner pressed Stop; never a failure."""


def new_run_identity() -> tuple[str, str]:
    """A run id the managed observation accepts, unique even within one second."""
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    return 'a13-' + secrets.token_hex(3) + '-qwen-' + stamp, str(uuid.uuid4())


class RunContext:
    def __init__(self, run: 'OwnerRun', run_id: str, deployment_id: str, reason: str) -> None:
        self._run = run
        self.run_id = run_id
        self.deployment_id = deployment_id
        self.reason = reason
        self.stop_event = threading.Event()
        #: Chain-owned results shown in status (registry_origin, participants, ...).
        self.result: dict[str, Any] = {}
        #: Chain-owned private state (workflow handles etc.); never shown.
        self.state: dict[str, Any] = {}

    def progress(self, detail: str | None) -> None:
        self._run._progress(self, detail)

    def check_stopped(self) -> None:
        if self.stop_event.is_set():
            raise _Stopped()

    def wait_until(self, predicate: Callable[[], Any], *, budget: float, code: str,
                   interval: float = 0.5) -> Any:
        """Poll ``predicate`` until truthy; Stop interrupts; budget expiry fails ``code``."""
        deadline = self._run._clock() + budget
        while True:
            self.check_stopped()
            value = predicate()
            if value:
                return value
            if self._run._clock() >= deadline:
                raise OwnerRunError(code)
            if self.stop_event.wait(interval):
                raise _Stopped()


class OwnerRun:
    def __init__(self, chain: Any, *, clock: Callable[[], float] = time.monotonic,
                 identities: Callable[[], tuple[str, str]] = new_run_identity) -> None:
        self.chain = chain
        self._clock = clock
        self._identities = identities
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._ctx: RunContext | None = None
        self._state = 'idle'
        self._steps = {name: {'state': 'pending', 'detail': None} for name in STEP_NAMES}
        self._step: str | None = None
        self._failed_step: str | None = None
        self._error: str | None = None
        self._error_message: str | None = None
        self._error_fix: dict[str, str] | None = None
        self._reason: str | None = None
        self._started_at: float | None = None
        self._ever_started = False
        self._rebuilding = False

    #: How often the serving run checks its execution ceiling on its own (nobody has to poll ``status``).
    _ceiling_poll_s = 1.0

    # -- public -------------------------------------------------------------
    def active(self) -> bool:
        with self._lock:
            return self._state in _ACTIVE

    def status(self) -> dict[str, Any]:
        with self._lock:
            ctx = self._ctx
            serving = self._state == 'serving' and ctx is not None
        if serving:
            # Outside the lock: the monitor may tick this Mac's own contributor,
            # which takes the app guard (never hold both in this order).
            monitor = getattr(self.chain, 'monitor', None)
            code = None
            if monitor is not None:
                try:
                    code = monitor(ctx)
                except Exception:
                    code = 'owner_run_monitor_failed'
            if code:
                with self._lock:
                    if self._state == 'serving' and self._ctx is ctx:
                        self._fail(ctx, 'serving', OwnerRunError(code).code)
                        ctx.stop_event.set()
        return self.status_snapshot()

    def status_snapshot(self) -> dict[str, Any]:
        """Read progress only; never tick a contributor or monitor the run.

        The run lock protects in-memory transitions, not chain operations.
        Admission and maintenance retain ``status()`` and its live checks.
        """
        import copy
        with self._lock:
            ctx = self._ctx
            return copy.deepcopy({
                'protocol': 'mycelium.a13_owner_run.v1',
                'state': self._state, 'reason': self._reason, 'step': self._step,
                'failed_step': self._failed_step, 'error_code': self._error,
                'error_message': self._error_message, 'error_fix': self._error_fix,
                'run_id': None if ctx is None else ctx.run_id,
                'deployment_id': None if ctx is None else ctx.deployment_id,
                'steps': [{'name': name, 'label': label, **self._steps[name]} for name, label in STEPS],
                'registry_origin': None if ctx is None else ctx.result.get('registry_origin'),
                'participants': None if ctx is None else ctx.result.get('participants'),
                'started_at': self._started_at,
                'route_ready': self._state == 'serving',
                'cleanup_unproven': None if ctx is None else ctx.result.get('cleanup_unproven'),
            })

    def start(self, reason: str = 'start', *, execution_ceiling: dict | None = None) -> dict[str, Any]:
        if reason not in {'start', 'rebuild'}:
            raise OwnerRunError('owner_run_reason_invalid')
        if execution_ceiling is not None:
            from .execution_admission import checked_ceiling
            try: execution_ceiling = checked_ceiling(execution_ceiling)
            except ValueError: raise OwnerRunError('execution_ceiling_invalid') from None
        with self._lock:
            if self._state in _ACTIVE:
                raise OwnerRunError('owner_run_active')
            run_id, deployment_id = self._identities()
            ctx = RunContext(self, run_id, deployment_id, reason)
            if execution_ceiling is not None:
                ctx.state['execution_ceiling'] = execution_ceiling
            self._ctx = ctx
            self._state = 'running'
            self._steps = {name: {'state': 'pending', 'detail': None} for name in STEP_NAMES}
            self._step = None
            self._failed_step = None
            self._error = None
            self._error_message = None
            self._error_fix = None
            self._reason = reason
            self._started_at = time.time()
            self._ever_started = True
            self._thread = threading.Thread(target=self._main, args=(ctx,), name='a13-owner-run', daemon=True)
            self._thread.start()
        return self.status()

    def _end_at_ceiling(self, ctx: RunContext) -> None:
        check = getattr(self.chain, 'ceiling_reached', None)
        if check is None:
            return
        try:
            code = check(ctx)
        except Exception:
            return
        if code:
            with self._lock:
                if self._state == 'serving' and self._ctx is ctx:
                    self._fail(ctx, 'serving', OwnerRunError(code).code)
                    ctx.stop_event.set()

    def stop(self) -> dict[str, Any]:
        with self._lock:
            ctx = self._ctx
            if self._state in {'running', 'serving'} and ctx is not None:
                self._state = 'stopping'
                ctx.stop_event.set()
        return self.status()

    def rebuild(self, *, join_seconds: float = 180.0) -> dict[str, Any]:
        with self._lock:
            if not self._ever_started:
                raise OwnerRunError('owner_run_rebuild_requires_run')
            thread = self._thread
            ceiling = None if self._ctx is None else self._ctx.state.get('execution_ceiling')
        self.stop()
        if thread is not None and thread is not threading.current_thread():
            thread.join(join_seconds)
            if thread.is_alive():
                raise OwnerRunError('owner_run_stop_incomplete')
        if ceiling is None:
            return self.start('rebuild')
        return self.start('rebuild', execution_ceiling=ceiling)

    def rebuild_async(self) -> dict[str, Any]:
        """Rebuild without blocking the caller (an HTTP handler must not wait)."""
        with self._lock:
            if not self._ever_started:
                raise OwnerRunError('owner_run_rebuild_requires_run')
            if self._rebuilding:
                raise OwnerRunError('owner_run_rebuild_in_progress')
            self._rebuilding = True

        def work():
            try:
                self.rebuild()
            except OwnerRunError as error:
                with self._lock:
                    self._error = error.code
                    self._state = 'failed'
            finally:
                with self._lock:
                    self._rebuilding = False
        # Stop before the worker exists: stop() ends whatever run is current, so
        # a later call could land on the fresh run the worker already started.
        self.stop()
        threading.Thread(target=work, name='a13-owner-rebuild', daemon=True).start()
        value = self.status()
        value['rebuild_requested'] = True
        return value

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    # -- internals ----------------------------------------------------------
    def _progress(self, ctx: RunContext, detail: str | None) -> None:
        with self._lock:
            if ctx is self._ctx and self._step is not None:
                self._steps[self._step]['detail'] = None if detail is None else str(detail)[:200]

    def _fail(self, ctx: RunContext, step: str, code: str, message: str | None = None,
              fix: dict[str, str] | None = None) -> None:
        with self._lock:
            if ctx is not self._ctx:
                return
            self._failed_step = step
            self._error = code
            self._error_message = message
            self._error_fix = fix
            self._steps[step]['state'] = 'failed'
            self._state = 'failing'

    def _main(self, ctx: RunContext) -> None:
        failed = False
        try:
            for name in STEP_NAMES:
                with self._lock:
                    self._step = name
                    self._steps[name]['state'] = 'active'
                ctx.check_stopped()
                try:
                    self.chain.run_step(name, ctx)
                except _Stopped:
                    raise
                except OwnerRunError as error:
                    self._fail(ctx, name, error.code, error.message, error.fix); failed = True
                    return
                except Exception:
                    self._fail(ctx, name, 'owner_run_step_failed'); failed = True
                    return
                with self._lock:
                    self._steps[name]['state'] = 'done'
            with self._lock:
                if self._state == 'running':
                    self._state = 'serving'
            # Serving: hold until Stop (or a monitor failure sets the event).  The workers expire on their own wall
            # clocks (PV34): the run ends itself first, whether or not anything polls status.
            while not ctx.stop_event.wait(self._ceiling_poll_s):
                self._end_at_ceiling(ctx)
            with self._lock:
                failed = self._state == 'failing'
        except _Stopped:
            with self._lock:
                if self._step is not None and self._steps[self._step]['state'] == 'active':
                    self._steps[self._step]['state'] = 'pending'
        finally:
            with self._lock:
                if self._state not in {'failing'}:
                    self._state = 'stopping'
            try:
                self.chain.teardown(ctx, failed=failed)
            except Exception:
                pass
            with self._lock:
                if ctx is self._ctx:
                    self._state = 'failed' if failed or self._state == 'failing' else 'stopped'
