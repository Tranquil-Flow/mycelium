# SPDX-License-Identifier: AGPL-3.0-only
"""L3 scenario capability: S1, S2 and S3 on the shipped request plane.

PLAN v2 §2.3 splits the old all-or-nothing C5 round into independent scenarios.
The three the M1 demo needs are:

* **S1** - 20 sequential requests, all reaching a successful terminal.
* **S2** - 8 concurrent requests.
* **S3** - 10 cancellations mid-stream; the next request succeeds.

This module is the **capability** to run those three, one scenario per command,
one JSON evidence record per run, with the verdict recorded honestly.  It is not
the M2 fix and it does not touch ``mycelium_a13_macos``: no worker replacement is
implemented here and nothing is repaired.

What is REAL in a scenario run (all shipped code, no double):

* ``mycelium_request_gateway.asgi.RequestGatewayASGIApplication`` - the shipped
  HTTP surface, driven through a real ASGI scope/receive/send cycle.
* ``mycelium_request_gateway.service.RequestGatewayService`` - admission,
  bounded replay, per-request worker thread, cancellation latch, terminal states.
* ``mycelium_request_gateway.backend.RouterSessionBackend`` and
  ``mycelium_router.router.Router`` - routing, admission, decode scheduling and
  the request-scoped KV/capacity release that cancellation has to prove.
* ``mycelium_router.transports.iroh.IrohTransport`` over **two real native
  ``mycelium-iroh-sidecar`` processes** (one per node), started by the harness and
  observable as OS processes - the request really crosses a process boundary.
* The streamed client events themselves: token indexes, acknowledgement cursors,
  ``completed`` / ``cancelled`` terminals.

What is SIMULATED (recorded in every evidence record as ``simulated``):

* **The model.**  ``model: none (not present locally)`` is *not* what this
  capability found - see :func:`local_model_facts`.  A real Qwen2.5-0.5B-Instruct
  snapshot IS on this host, but no shipped code path on this plane loads it: the
  execution seam under the Router is ``mycelium_router.mlx_runtime.MLXRuntimePort``
  and it needs a per-host route bundle (stage packs, load proofs, assignment)
  built by ``scripts/build_qwen_live_route.py``.  Until such a bundle exists this
  capability runs the deterministic stage runtime
  (``tests/e2e_request_iroh/harness.py::_DeterministicStageRuntime``) and records
  the real weights as *present but unused*, never as a model result.
* The route qualification fixture (in-repo synthetic evidence, ``local_evidence_only``).
* ``ManualClock`` / ``FakeCapacityPort`` / ``FakeTopologyProvider`` device state.
* No fault proxy, no second Mac, no physical boundary.

Everything is loopback/local.  No synthetic load generator exists in this file:
S2 is 8 concurrent *requests* through the shipped gateway, not 8 busy loops, and
nothing here burns CPU outside the requests under test.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

#: Protocol tag of the evidence record this capability writes.
EVIDENCE_PROTOCOL = "mycelium.testbed.l3_scenarios.v1"

#: Ladder level these scenarios run at (PLAN v2 §2.1).
SCENARIO_LEVEL = "L3"

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

#: Tokens per request.  Bounded so a scenario stays inside PLAN's L3 budget
#: (< 10 min per scenario) while still requiring a real prefill plus real decode
#: round trips through the native transport.
MAX_NEW_TOKENS = 4

#: Wall bound on one request from submission to terminal.
REQUEST_BOUND_SECONDS = 60.0

#: Wall bound on one whole scenario.
SCENARIO_BOUND_SECONDS = 420.0

#: How long a stalled decode is held before the gate gives up (and records that
#: it gave up) so a broken cancellation cannot wedge the run forever.
STALL_BOUND_SECONDS = 20.0

#: Wall bound on post-terminal cleanup observation (paths, pending deliveries).
CLEANUP_BOUND_SECONDS = 10.0

#: How long to wait for a client-visible token after the remote decode stalls.
MID_STREAM_TOKEN_BOUND_SECONDS = 10.0

#: Gateway knobs, recorded in the evidence because they decide the answer:
#: ``max_concurrent_requests`` is the executor width of the shipped service and
#: its default is 4, which would silently cap S2's eight concurrent requests.
GATEWAY_MAX_BUFFERED_EVENTS = 64
GATEWAY_MAX_SESSIONS = 32
GATEWAY_MAX_CONCURRENT_REQUESTS = 8
GATEWAY_MAX_PENDING_REQUESTS = 16

#: The real 0.5B snapshot this host may carry.  Probed read-only; never fetched.
LOCAL_MODEL_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("Qwen/Qwen2.5-0.5B-Instruct", "models--Qwen--Qwen2.5-0.5B-Instruct"),
    ("Qwen/Qwen3-0.6B", "models--Qwen--Qwen3-0.6B"),
)

_MODEL_WEIGHT_SUFFIXES = (".safetensors", ".npz", ".gguf", ".bin")


class ScenarioError(RuntimeError):
    """The scenario could not be run at all (a precondition, not a verdict)."""


# --------------------------------------------------------------------------- #
# Facts that go into every record
# --------------------------------------------------------------------------- #


def _scrubbed_environment() -> dict[str, str]:
    """Environment for child processes: no inherited interpreter or git state."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _git(*arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=str(REPOSITORY_ROOT),
        env=_scrubbed_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def repository_facts() -> dict[str, Any]:
    """The exact bytes: commit, tracked modifications and the product digest."""

    from mycelium_testbed.iroh_e2e import _product_package_digest

    return {
        "repository_root": str(REPOSITORY_ROOT),
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "tracked_modifications": [
            line for line in _git("status", "--porcelain").splitlines() if line
        ],
        "product_package_digest": _product_package_digest(REPOSITORY_ROOT),
    }


def local_model_facts() -> dict[str, Any]:
    """Is a real 0.5B snapshot on this host, and does this plane load it?

    Read-only.  Nothing is downloaded: a missing snapshot is reported as missing.
    Presence of weights is *not* a claim that a model ran - the ``loaded`` field
    is what separates the two, and on this plane it is false.
    """

    hub = Path("~/.cache/huggingface/hub").expanduser()
    attempts: list[dict[str, Any]] = []
    for model_id, directory in LOCAL_MODEL_CANDIDATES:
        root = hub / directory
        snapshot = None
        files: dict[str, int] = {}
        for candidate in sorted(root.glob("snapshots/*")):
            present: dict[str, int] = {}
            for name in ("config.json", "tokenizer.json", "vocab.json", "merges.txt",
                         "generation_config.json", "model.safetensors"):
                path = candidate / name
                try:
                    resolved = path.resolve(strict=True)
                except OSError:
                    continue
                try:
                    present[name] = resolved.stat().st_size
                except OSError:
                    continue
            if present:
                files = present
                snapshot = candidate
                if any(name.endswith(_MODEL_WEIGHT_SUFFIXES) for name in present):
                    break
        weights_bytes = sum(
            size for name, size in files.items() if name.endswith(_MODEL_WEIGHT_SUFFIXES)
        )
        attempts.append(
            {
                "model_id": model_id,
                "hub_directory": str(root),
                "snapshot": None if snapshot is None else str(snapshot),
                "files": files,
                "weights_bytes": weights_bytes,
                "present": bool(weights_bytes),
            }
        )
    chosen = next((item for item in attempts if item["present"]), None)
    return {
        "probe": "local HuggingFace hub cache, read-only, no download",
        "candidates": attempts,
        "real_0_5b_present_locally": chosen is not None,
        "selected": None if chosen is None else chosen["model_id"],
        "loaded_by_this_capability": False,
        "loaded_reason": (
            "no shipped loader is wired to this plane: the execution seam below the "
            "Router is mycelium_router.mlx_runtime.MLXRuntimePort, which needs a "
            "per-host route bundle (stage packs + load proofs + assignment) built by "
            "scripts/build_qwen_live_route.py; this capability does not build one and "
            "does not download anything"
        ),
        "executor_actually_used": (
            "tests/e2e_request_iroh/harness.py::_DeterministicStageRuntime "
            "(deterministic token ids, no weights)"
        ),
    }


def quiet_host_guard(*, ceiling: float = 8.0) -> dict[str, Any]:
    """PLAN v2 §2.1's quiet-host guard, recorded (never silently enforced)."""

    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:  # pragma: no cover - platform without a load average
        load1 = load5 = load15 = float("nan")
    return {
        "ceiling": ceiling,
        "load1": load1,
        "load5": load5,
        "load15": load15,
        "quiet": bool(load1 <= ceiling),
    }


def _digest_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return "sha256:" + digest.hexdigest()


# --------------------------------------------------------------------------- #
# The harness seam
# --------------------------------------------------------------------------- #


def _load_harness() -> Any:
    """Import the *existing* iroh e2e harness (:mod:`tests.e2e_request_iroh`).

    Not re-implemented: the shipped gateway ASGI surface, the real Router pair and
    the two real native sidecar processes come from that harness.  Only the two
    things it hard-codes for a *single* request are replaced by this capability -
    see :class:`ScenarioStack`.
    """

    root = str(REPOSITORY_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from tests.e2e_request_iroh import harness  # noqa: PLC0415 - lazy on purpose
    except ImportError as error:  # pragma: no cover - depends on the tree
        raise ScenarioError(f"iroh_e2e_harness_not_importable:{error}") from error
    return harness


def shipped_sidecar_path(harness: Any) -> Path:
    """Where the harness insists the real native sidecar binary lives."""

    binary = Path(harness.BINARY)
    if not binary.is_file():
        raise ScenarioError(
            "native_iroh_sidecar_missing:{0}: build it with 'cargo build --locked "
            "--bin mycelium-iroh-sidecar' inside native/iroh_transport (target/ is "
            "git-ignored)".format(binary)
        )
    return binary


@contextlib.contextmanager
def patched(module: Any, attribute: str, replacement: Any) -> Iterator[None]:
    """The same substitution seam :mod:`mycelium_testbed.iroh_e2e` already uses."""

    original = getattr(module, attribute)
    setattr(module, attribute, replacement)
    try:
        yield
    finally:
        setattr(module, attribute, original)


class StallGate:
    """Hold one remote decode in flight so a cancellation is genuinely mid-stream.

    S3 needs the request to be running *when* the cancel is issued.  The harness
    proves that deterministically with an interlock on the far node's decode; this
    gate is the same idea, but re-armable, because S3 cancels ten requests in a
    row.  It stalls only the first matching decode after each :meth:`arm`, and it
    records whether it ever entered the stall - a cancel that never had a request
    to interrupt is not evidence of anything.
    """

    def __init__(self, *, stage_index: int = 1, token_index: int = 1) -> None:
        self.stage_index = stage_index
        self.token_index = token_index
        self._lock = threading.Lock()
        self._armed = False
        self._fired = False
        self._entered = threading.Event()
        self._release = threading.Event()
        self._entered_at: float | None = None
        self._released_at: float | None = None
        self._stalled_path: str | None = None
        self._holds = 0
        self._timeouts = 0

    def arm(self) -> None:
        with self._lock:
            self._armed = True
            self._fired = False
            self._entered = threading.Event()
            self._release = threading.Event()
            self._entered_at = None
            self._released_at = None
            self._stalled_path = None

    def disarm(self) -> None:
        with self._lock:
            self._armed = False
            self._release.set()


    def before_execute(self, item: Any, stage_index: int) -> None:
        """Called by the gated executor *before* the stub executes the hop."""

        with self._lock:
            if not self._armed or self._fired:
                return
            if getattr(item, "phase", None) != "DECODE":
                return
            if stage_index != self.stage_index:
                return
            if (getattr(item, "token_index", 0) or 0) < self.token_index:
                return
            self._fired = True
            self._stalled_path = getattr(item, "path_id", None)
            self._entered_at = time.monotonic()
            entered, release = self._entered, self._release
        entered.set()
        if not release.wait(timeout=STALL_BOUND_SECONDS):
            with self._lock:
                self._timeouts += 1
                self._release.set()
        with self._lock:
            self._holds += 1
            self._released_at = time.monotonic()

    def wait_entered(self, timeout: float) -> bool:
        return self._entered.wait(timeout=timeout)

    def release(self) -> None:
        with self._lock:
            self._release.set()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stage_index": self.stage_index,
                "token_index": self.token_index,
                "armed": self._armed,
                "stall_entered": self._entered.is_set(),
                "stalled_path_id": self._stalled_path,
                "held_seconds": (
                    None
                    if self._entered_at is None or self._released_at is None
                    else self._released_at - self._entered_at
                ),
                "holds": self._holds,
                "stall_timeouts": self._timeouts,
            }


def _gated_runtime_class(stall: StallGate) -> type:
    """``_DeterministicStageRuntime`` with the stall gate in front of ``execute``."""

    harness = _load_harness()

    class GatedDeterministicStageRuntime(harness._DeterministicStageRuntime):  # type: ignore[misc]
        """Subclass, not a re-implementation: the stub still does the work."""

        def execute(self, item: Any) -> Any:
            stall.before_execute(item, self._stage_by_placement[item.placement_id])
            return super().execute(item)

    GatedDeterministicStageRuntime.__name__ = "GatedDeterministicStageRuntime"
    return GatedDeterministicStageRuntime


class RequestIds:
    """Deterministic, unique request ids for N requests (the harness issues one)."""

    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self._count = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            self._count += 1
            index = self._count
        return f"{self.prefix}-{index:03d}"

    @property
    def issued(self) -> int:
        with self._lock:
            return self._count


# --------------------------------------------------------------------------- #
# One scenario's shipped-path stack
# --------------------------------------------------------------------------- #


class ScenarioStack:
    """The shipped request plane for one scenario, reusing the harness's topology.

    Two things in the harness are single-request by construction and are therefore
    re-composed here (the product classes are imported, not copied):

    * ``harness._FixedRequestId`` raises on the second request;
    * ``harness._service_stack`` builds ``RequestGatewayService`` with
      ``max_concurrent_requests`` left at its default of 4, which cannot express
      S2's eight concurrent requests.

    Everything else - the two real sidecar processes, the two real Routers, the real
    ``IrohTransport`` pair, the qualification fixture, the ASGI application - comes
    from the harness unchanged.
    """

    def __init__(
        self,
        *,
        request_prefix: str,
        stall: StallGate | None = None,
        max_concurrent_requests: int = GATEWAY_MAX_CONCURRENT_REQUESTS,
    ) -> None:
        from mycelium_request_gateway.asgi import RequestGatewayASGIApplication
        from mycelium_request_gateway.auth import StaticBearerAuthenticator
        from mycelium_request_gateway.backend import RouterSessionBackend
        from mycelium_request_gateway.contracts import (
            InferenceSubmission,
            qualification_binding,
        )
        from mycelium_request_gateway.service import RequestGatewayService

        self.harness = harness = _load_harness()
        self.sidecar_binary = shipped_sidecar_path(harness)
        self.stall = stall
        self.request_ids = RequestIds(request_prefix)
        runtime_class = (
            harness._DeterministicStageRuntime
            if stall is None
            else _gated_runtime_class(stall)
        )
        with patched(harness, "_DeterministicStageRuntime", runtime_class):
            self.topology = harness._Topology(self.sidecar_binary)

        qualification = harness._synthetic_qualification()
        self.source = harness._QualificationSource(None)
        self.port = harness._DistributedRouterPort(self.topology.router_a)
        self.backend = RouterSessionBackend(
            router=self.port,
            codec=harness._DeterministicCodec(),
            clock=self.topology.clock.now,
            excluded_placements=frozenset({"node-b-stage-000"}),
            sampling_seed=0,
        )
        self.service = RequestGatewayService(
            qualification_source=self.source,
            backend=self.backend,
            request_id_source=self.request_ids,
            max_buffered_events=GATEWAY_MAX_BUFFERED_EVENTS,
            max_sessions=GATEWAY_MAX_SESSIONS,
            max_concurrent_requests=max_concurrent_requests,
            max_pending_requests=max(
                GATEWAY_MAX_PENDING_REQUESTS, max_concurrent_requests
            ),
        )
        self.app = RequestGatewayASGIApplication(
            self.service,
            authenticator=StaticBearerAuthenticator(harness.AUTH_TOKEN),
        )
        self.qualification = qualification
        self.submission = InferenceSubmission(
            prompt=harness.PROMPT,
            max_new_tokens=MAX_NEW_TOKENS,
            qualification=qualification_binding(qualification),
        )
        # The unqualified POST is the harness's own negative control: the route
        # gate must refuse it before any mutation.  Kept because a plane that
        # answers an unqualified request is not the shipped gate at all.
        self.unqualified = self.harness._post(self.app, self.submission)
        self.source.value = qualification

    # -- infrastructure facts ------------------------------------------------- #

    def infrastructure_facts(self) -> dict[str, Any]:
        harness = self.harness
        pids = {
            "entry_sidecar_pid": self.topology.first_sidecar.pid,
            "remote_sidecar_pid": self.topology.second_sidecar.pid,
        }
        return {
            "sidecar_binary": str(self.sidecar_binary),
            "sidecar_binary_digest": _digest_file(self.sidecar_binary),
            "sidecar_is_native_child_process": os.getpid() not in set(pids.values()),
            **pids,
            "transport_types": [
                type(self.topology.first).__name__,
                type(self.topology.second).__name__,
            ],
            "production_router_count": 2,
            "gateway_application": "mycelium_request_gateway.asgi.RequestGatewayASGIApplication",
            "gateway_service": "mycelium_request_gateway.service.RequestGatewayService",
            "gateway_knobs": {
                "max_buffered_events": GATEWAY_MAX_BUFFERED_EVENTS,
                "max_sessions": GATEWAY_MAX_SESSIONS,
                "max_concurrent_requests": self.service._max_concurrent_requests,
                "max_pending_requests": self.service._max_pending_requests,
            },
            "unqualified_post": {
                "status": self.unqualified[0],
                "body": self.unqualified[1],
                "refused_as_expected": self.unqualified[1] == {"error": "route_dropped"},
            },
        }

    # -- driving one request -------------------------------------------------- #

    def start_request(self) -> "RequestRun":
        """Submit one request through the shipped ASGI surface and subscribe."""

        run = RequestRun(stack=self, index=self.request_ids.issued + 1)
        status, body = self.harness._post(self.app, self.submission)
        run.admitted_status = status
        # The session token is a live credential for this loopback plane; the
        # digest is recorded and the value is not.
        run.admitted_body = {
            key: ("redacted; see session_token_digest" if key == "session_token" else value)
            for key, value in (body or {}).items()
        }
        request_id = body.get("request_id")
        session_token = body.get("session_token")
        if status != 202 or not isinstance(request_id, str):
            run.error = f"request_not_admitted:{status}:{json.dumps(body, sort_keys=True)}"
            return run
        run.request_id = request_id
        run.session_token_digest = (
            None
            if not isinstance(session_token, str)
            else "sha256:" + hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        )
        if not isinstance(session_token, str):
            run.error = "missing_session_token"
            return run
        run.session_token = session_token
        run.subscription = self.service.subscribe(
            request_id, last_event_id=None, owner_token=session_token
        )
        run.accepted_at = time.monotonic()
        return run

    def cancel(self, run: "RequestRun") -> bool:
        if run.request_id is None or run.session_token is None:
            return False
        run.cancel_issued_at = time.monotonic()
        # How much of the answer the client had already seen when the cancel was
        # issued: S3's "mid-stream" claim is this number, not an intention.
        run.tokens_at_cancel = run.tokens_delivered()
        return bool(
            self.service.cancel(run.request_id, owner_token=run.session_token)
        )

    def route_path_id(self, run: "RequestRun") -> str | None:
        try:
            record = self.topology.router_a.get_request(run.request_id or "")
        except Exception:
            return None
        manifest = getattr(record, "manifest", None)
        return None if manifest is None else manifest.path_id

    def cleanup_facts(self, run: "RequestRun", path_id: str | None) -> dict[str, Any]:
        """What the shipped plane released after the terminal (measured, not assumed)."""

        request_id = run.request_id or ""
        session = self.service._sessions.get(request_id)
        facts: dict[str, Any] = {"gateway_session_present": session is not None}
        if session is not None:
            with session.condition:
                facts.update(
                    {
                        "gateway_worker_done": bool(session.worker_done),
                        "gateway_submission_released": session.submission is None,
                        "gateway_snapshot_released": session.captured is None,
                        "gateway_subscription_detached": not session.active_subscription,
                    }
                )
        facts["adapter_cancellation_latch"] = request_id in getattr(
            self.backend, "_cancelled", set()
        )
        if path_id is None:
            facts["path_id"] = None
            facts["entry_path_released"] = None
            facts["remote_path_released"] = None
        else:
            facts["path_id"] = path_id
            facts["entry_path_released"] = (
                not self.topology.runtime_a.has_path(path_id)
                and path_id not in self.topology.router_a.relay._paths
            )
            facts["remote_path_released"] = (
                not self.topology.runtime_c.has_path(path_id)
                and path_id not in self.topology.router_c.relay._paths
            )
            facts["entry_transport_cleanup_complete"] = (
                self.topology.first.cancellation_cleanup_complete(request_id, path_id)
            )
            facts["remote_transport_cleanup_complete"] = (
                self.topology.second.cancellation_cleanup_complete(request_id, path_id)
            )
        facts["pending_deliveries"] = (
            self.topology.first.pending_delivery_count
            + self.topology.second.pending_delivery_count
        )
        facts["transport_fatal_error"] = (
            None
            if not self.topology.first.fatal_error and not self.topology.second.fatal_error
            else str(self.topology.first.fatal_error or self.topology.second.fatal_error)
        )
        return facts

    def wait_for_cleanup(self, run: "RequestRun", path_id: str | None) -> dict[str, Any]:
        deadline = time.monotonic() + CLEANUP_BOUND_SECONDS
        facts = self.cleanup_facts(run, path_id)
        while time.monotonic() < deadline:
            if facts["transport_fatal_error"] is not None or path_id is None:
                break
            if (
                facts.get("entry_path_released")
                and facts.get("remote_path_released")
                and facts.get("pending_deliveries") == 0
                and facts.get("entry_transport_cleanup_complete") is not False
                and facts.get("remote_transport_cleanup_complete") is not False
            ):
                break
            time.sleep(0.01)
            facts = self.cleanup_facts(run, path_id)
        return facts

    def close(self) -> None:
        errors: list[BaseException] = []
        for close_resource in (self.service.close, self.topology.close):
            try:
                close_resource()
            except BaseException as error:  # pragma: no cover - teardown reporting
                errors.append(error)
        if errors:  # pragma: no cover - teardown reporting
            raise ScenarioError(f"scenario_teardown_failed:{errors[0]!r}")


    # -- concurrency (S2: eight concurrent *requests*, no load generator) ------ #

    def submit_concurrent(self, count: int) -> list["RequestRun"]:
        """Submit ``count`` requests back to back, then drain them on own threads.

        Submission is serialised only by the gateway's own admission; the requests
        overlap because each one gets its own session, its own client subscription
        and its own driver thread.  The only work performed is the requests
        themselves - no spinner, no busy loop, no artificial load.
        """

        runs = [self.start_request() for _ in range(count)]
        threads: list[threading.Thread] = []
        for index, run in enumerate(runs):
            if run.subscription is None:
                continue
            thread = threading.Thread(
                target=run.drive,
                kwargs={"bound_seconds": REQUEST_BOUND_SECONDS},
                name=f"{self.request_ids.prefix}-request-{index + 1}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join(timeout=REQUEST_BOUND_SECONDS + 5.0)
        return runs


@dataclass
class RequestRun:
    """One request's measured life, from submission to its terminal event."""

    stack: "ScenarioStack"
    index: int
    request_id: str | None = None
    session_token: str | None = None
    session_token_digest: str | None = None
    admitted_status: int | None = None
    admitted_body: Mapping[str, Any] | None = None
    submitted_at: float = field(default_factory=time.monotonic)
    accepted_at: float | None = None
    first_token_at: float | None = None
    terminal_at: float | None = None
    terminal_kind: str | None = None
    terminal_code: str | None = None
    terminal_sequence: int | None = None
    cancel_issued_at: float | None = None
    cancel_accepted: bool | None = None
    tokens_at_cancel: int | None = None
    tokens: list[int] = field(default_factory=list)
    lifecycle_kinds: list[str] = field(default_factory=list)
    acked_cursor: int | None = None
    error: str | None = None
    subscription: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- measurement ---------------------------------------------------------- #

    def tokens_delivered(self) -> int:
        with self._lock:
            return len(self.tokens)

    def note(self, event: Any) -> None:
        with self._lock:
            self.lifecycle_kinds.append(event.kind)
            if event.kind == "token":
                if event.token_index is not None:
                    self.tokens.append(event.token_index)
                if self.first_token_at is None:
                    self.first_token_at = time.monotonic()
            if event.terminal:
                self.terminal_at = time.monotonic()
                self.terminal_kind = event.kind
                self.terminal_code = event.code
                self.terminal_sequence = event.sequence

    def drive(self, *, bound_seconds: float = REQUEST_BOUND_SECONDS) -> None:
        """Stream the client's events to a terminal, or record why it stopped."""

        if self.subscription is None:
            return
        deadline = time.monotonic() + bound_seconds
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    with self._lock:
                        self.error = self.error or "request_bound_expired_without_terminal"
                    return
                event = self.subscription.next_event(timeout=min(5.0, remaining))
                if event is None:
                    continue
                self.note(event)
                self.subscription.ack(event.sequence)
                with self._lock:
                    self.acked_cursor = event.sequence
                if event.terminal:
                    return
        except BaseException as error:  # a client-side fault is data, not a crash
            with self._lock:
                self.error = f"drive_failed:{type(error).__name__}:{error}"
        finally:
            try:
                self.subscription.close()
            except BaseException:  # pragma: no cover - teardown reporting
                pass

    def record(self, *, path_id: str | None = None, cleanup: Mapping[str, Any] | None = None
               ) -> dict[str, Any]:
        with self._lock:
            tokens = list(self.tokens)
            lifecycle = list(self.lifecycle_kinds)
            terminal_at, terminal_kind = self.terminal_at, self.terminal_kind
            terminal_code, first_token_at = self.terminal_code, self.first_token_at
            cancel_issued_at = self.cancel_issued_at
            accepted_at = self.accepted_at
            submitted_at = self.submitted_at
            error, acked = self.error, self.acked_cursor
            cancel_accepted = self.cancel_accepted
            session_token_digest = self.session_token_digest
            terminal_sequence = self.terminal_sequence
            tokens_at_cancel = self.tokens_at_cancel
        document: dict[str, Any] = {
            "index": self.index,
            "request_id": self.request_id,
            "session_token_digest": session_token_digest,
            "admitted_status": self.admitted_status,
            "admitted_body": dict(self.admitted_body or {}),
            "submitted_at": submitted_at,
            "seconds_to_accepted": (
                None if accepted_at is None else accepted_at - submitted_at
            ),
            "seconds_to_first_token": (
                None if first_token_at is None else first_token_at - submitted_at
            ),
            "seconds_to_terminal": (
                None if terminal_at is None else terminal_at - submitted_at
            ),
            "terminal_kind": terminal_kind,
            "terminal_code": terminal_code,
            "terminal_sequence": terminal_sequence,
            "token_indexes": tokens,
            "token_count": len(tokens),
            "event_kinds": lifecycle,
            "acked_cursor": acked,
            "cancel_issued_at": cancel_issued_at,
            "cancel_accepted": cancel_accepted,
            "tokens_before_cancel": (
                None
                if cancel_issued_at is None
                else tokens_at_cancel
            ),
            "seconds_cancel_to_terminal": (
                None
                if cancel_issued_at is None or terminal_at is None
                else terminal_at - cancel_issued_at
            ),
            "path_id": path_id,
            "error": error,
        }
        if cleanup is not None:
            document["cleanup"] = dict(cleanup)
        return document


# --------------------------------------------------------------------------- #
# Scenario definitions, verdicts and the evidence record
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Scenario:
    identifier: str
    requests: int
    concurrent: bool
    cancellations: int
    title: str


SCENARIOS: dict[str, Scenario] = {
    "S1": Scenario(
        identifier="S1",
        requests=20,
        concurrent=False,
        cancellations=0,
        title="20 sequential requests, all reaching a successful terminal",
    ),
    "S2": Scenario(
        identifier="S2",
        requests=8,
        concurrent=True,
        cancellations=0,
        title="8 concurrent requests, all reaching a successful terminal",
    ),
    "S3": Scenario(
        identifier="S3",
        requests=10,
        concurrent=False,
        cancellations=10,
        title="10 cancellations mid-stream, then the next request succeeds",
    ),
}

SHIPPED_PATH = (
    "mycelium_request_gateway.asgi.RequestGatewayASGIApplication (real ASGI cycle)",
    "mycelium_request_gateway.service.RequestGatewayService (admission/replay/"
    "cancellation/terminals)",
    "mycelium_request_gateway.backend.RouterSessionBackend (per-request routing)",
    "mycelium_router.router.Router x2 (production routers, real decode scheduling)",
    "mycelium_router.transports.iroh.IrohTransport over the real "
    "mycelium-iroh-sidecar native processes (two OS processes)",
    "native/iroh_transport/target/debug/mycelium-iroh-sidecar (real Rust binary)",
)

REAL_ELEMENTS = (
    "the shipped gateway ASGI application and service, driven over HTTP semantics",
    "the shipped Router pair including admission, decode scheduling and "
    "request-scoped capacity/KV release",
    "the real native iroh sidecar binary, two processes, real frames between them",
    "the client stream itself: token events, acknowledgements, cursors, terminals",
    "cancellation through the shipped gateway/backend/router path and its cleanup",
)

SIMULATED_ELEMENTS = (
    "model: none loaded (a real 0.5B snapshot is on disk but this plane has no "
    "loader for it); the executor is harness._DeterministicStageRuntime, which "
    "returns deterministic token ids and holds no weights",
    "route qualification: in-repo synthetic fixture (test-signed, "
    "local_evidence_only=true, route_ready=false)",
    "mycelium_router.fakes ManualClock / FakeCapacityPort / FakeTopologyProvider / "
    "FakeDeviceStateProvider (device state is a fixture, not a measurement)",
    "no fault proxy, no latency/loss injection",
    "one Mac, loopback only; no physical boundary and no second device",
    "the graded numbers are properties of this plane, not of the hardware",
)

NOT_COVERED = (
    "the real 0.5B model load and real tokens (MLXRuntimePort needs a per-host "
    "route bundle built by scripts/build_qwen_live_route.py)",
    "real iroh between two *machines*; here both sidecars are local processes",
    "the a13_macos managed worker and TargetApplication control plane - the request "
    "plane exercised here is the gateway/router plane, not the managed-worker plane",
    "worker death (S4/S5) and its replacement/re-authorisation; see "
    "mycelium_testbed/real_worker_kill.py for the capability that lane owns",
    "network change (S6), long soak (S7), the 1800 s daemon lifetime bound",
    "two-Mac clock offsets and cross-host timing (L4)",
)


def _evaluate_s1(observations: Mapping[str, Any]) -> list[str]:
    gaps: list[str] = []
    runs = observations["requests"]
    if len(runs) != SCENARIOS["S1"].requests:
        gaps.append(
            f"requests_issued:{len(runs)}: expected {SCENARIOS['S1'].requests}"
        )
    for run in runs:
        label = f"request_{run['index']}"
        if run["admitted_status"] != 202:
            gaps.append(f"{label}_not_admitted:{run['admitted_status']}:{run['error']}")
            continue
        if run["terminal_kind"] != "completed":
            gaps.append(
                f"{label}_terminal:{run['terminal_kind']!r}:{run['terminal_code']!r}"
                f"{'' if run['error'] is None else ':' + run['error']}"
            )
        elif run["token_count"] != MAX_NEW_TOKENS:
            gaps.append(
                f"{label}_token_count:{run['token_count']}: expected {MAX_NEW_TOKENS}"
            )
    return gaps


def _evaluate_s2(observations: Mapping[str, Any]) -> list[str]:
    gaps: list[str] = []
    runs = observations["requests"]
    concurrent = observations["concurrency"]
    if len(runs) != SCENARIOS["S2"].requests:
        gaps.append(f"requests_issued:{len(runs)}: expected {SCENARIOS['S2'].requests}")
    admitted = [run for run in runs if run["admitted_status"] == 202]
    if len(admitted) != SCENARIOS["S2"].requests:
        gaps.append(
            f"admitted:{len(admitted)}: expected {SCENARIOS['S2'].requests} "
            f"(gateway max_concurrent_requests="
            f"{observations['gateway_knobs']['max_concurrent_requests']}, "
            f"max_pending_requests={observations['gateway_knobs']['max_pending_requests']})"
        )
    for run in runs:
        if run["admitted_status"] != 202:
            gaps.append(
                f"request_{run['index']}_not_admitted:{run['admitted_status']}:{run['error']}"
            )
            continue
        if run["terminal_kind"] != "completed":
            gaps.append(
                f"request_{run['index']}_terminal:{run['terminal_kind']!r}:"
                f"{run['terminal_code']!r}"
            )
        elif run["token_count"] != MAX_NEW_TOKENS:
            gaps.append(
                f"request_{run['index']}_token_count:{run['token_count']}: "
                f"expected {MAX_NEW_TOKENS}"
            )
    if concurrent["admitted_before_first_terminal"] != SCENARIOS["S2"].requests:
        gaps.append(
            "not_concurrent: only "
            f"{concurrent['admitted_before_first_terminal']} of "
            f"{SCENARIOS['S2'].requests} requests were admitted before the first "
            "terminal event"
        )
    return gaps


def _evaluate_s3(observations: Mapping[str, Any]) -> list[str]:
    gaps: list[str] = []
    cancellations = observations["cancellations"]
    if len(cancellations) != SCENARIOS["S3"].cancellations:
        gaps.append(
            f"cancellations_issued:{len(cancellations)}: expected "
            f"{SCENARIOS['S3'].cancellations}"
        )
    for entry in cancellations:
        label = f"cancel_{entry['index']}"
        if entry["stall_entered_seconds"] is None:
            gaps.append(f"{label}_never_reached_a_running_decode")
        if not entry["cancel_accepted"]:
            gaps.append(f"{label}_gateway_refused_the_cancellation")
        if entry["tokens_before_cancel"] < 1:
            gaps.append(
                f"{label}_not_mid_stream: {entry['tokens_before_cancel']} client token "
                "events had been delivered when the cancellation was issued"
            )
        if entry["terminal_kind"] != "cancelled":
            gaps.append(
                f"{label}_terminal:{entry['terminal_kind']!r}:{entry['terminal_code']!r} "
                "(expected 'cancelled')"
            )
        cleanup = entry.get("cleanup") or {}
        if cleanup.get("pending_deliveries"):
            gaps.append(
                f"{label}_pending_deliveries:{cleanup['pending_deliveries']}: the "
                "cancellation left frames in flight"
            )
        if cleanup.get("entry_path_released") is False or cleanup.get(
            "remote_path_released"
        ) is False:
            gaps.append(
                f"{label}_path_not_released: entry={cleanup.get('entry_path_released')} "
                f"remote={cleanup.get('remote_path_released')}"
            )
        if cleanup.get("transport_fatal_error") is not None:
            gaps.append(f"{label}_transport_fatal:{cleanup['transport_fatal_error']}")
    following = observations.get("next_request")
    if following is None:
        gaps.append("next_request_after_cancellations_missing")
    elif following["terminal_kind"] != "completed":
        gaps.append(
            "next_request_after_cancellations_failed:"
            f"{following['terminal_kind']!r}:{following['terminal_code']!r}"
        )
    elif following["token_count"] != MAX_NEW_TOKENS:
        gaps.append(
            f"next_request_token_count:{following['token_count']}: expected "
            f"{MAX_NEW_TOKENS}"
        )
    return gaps


EVALUATORS: dict[str, Callable[[Mapping[str, Any]], list[str]]] = {
    "S1": _evaluate_s1,
    "S2": _evaluate_s2,
    "S3": _evaluate_s3,
}


def concurrency_window(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Measured overlap: how many requests were in flight at the same time.

    In flight = submitted (admitted by the gateway) and not yet terminal.  The
    admission stamp is the submission time, which is when the shipped service
    accepted the request; the terminal stamp comes from the client's own event
    stream.
    """

    events: list[tuple[float, int]] = []
    for run in runs:
        if run["admitted_status"] != 202:
            continue
        start = run["submitted_at"]
        end = run["submitted_at"] + (run["seconds_to_terminal"] or 0.0)
        events.append((start, 1))
        events.append((end, -1))
    events.sort(key=lambda item: (item[0], -item[1]))
    in_flight = 0
    peak = 0
    over_one_seconds = 0.0
    for index, (stamp, delta) in enumerate(events):
        in_flight += delta
        peak = max(peak, in_flight)
        if index + 1 < len(events):
            nxt = events[index + 1][0]
            if in_flight > 1 and nxt > stamp:
                over_one_seconds += nxt - stamp
    first_terminal = min(
        (run["submitted_at"] + (run["seconds_to_terminal"] or 0.0) for run in runs
         if run["admitted_status"] == 202 and run["terminal_kind"] is not None),
        default=None,
    )
    admitted_before_first_terminal = (
        0
        if first_terminal is None
        else sum(
            1
            for run in runs
            if run["admitted_status"] == 202 and run["submitted_at"] <= first_terminal
        )
    )
    return {
        "peak_in_flight": peak,
        "seconds_with_more_than_one_in_flight": over_one_seconds,
        "admitted_before_first_terminal": admitted_before_first_terminal,
        "first_terminal_at": first_terminal,
    }


def _base_record(scenario: Scenario, *, run_id: str) -> dict[str, Any]:
    return {
        "protocol": EVIDENCE_PROTOCOL,
        "scenario": scenario.identifier,
        "scenario_title": scenario.title,
        "level": SCENARIO_LEVEL,
        "run_id": run_id,
        "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "interpreter": sys.executable,
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "repository": repository_facts(),
        "quiet_host": quiet_host_guard(),
        "shipped_path": list(SHIPPED_PATH),
        "real": list(REAL_ELEMENTS),
        "simulated": list(SIMULATED_ELEMENTS),
        "not_covered": list(NOT_COVERED),
        "model": local_model_facts(),
        "scenario_parameters": {
            "requests": scenario.requests,
            "concurrent": scenario.concurrent,
            "cancellations": scenario.cancellations,
            "max_new_tokens": MAX_NEW_TOKENS,
            "request_bound_seconds": REQUEST_BOUND_SECONDS,
            "scenario_bound_seconds": SCENARIO_BOUND_SECONDS,
            "prompt": None,
        },
    }


def _finish(record: dict[str, Any], *, started: float) -> dict[str, Any]:
    record["elapsed_seconds"] = time.monotonic() - started
    record["finished_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return record


def write_evidence(path: Path | str, record: Mapping[str, Any]) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(record, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return path


def _one_request(stack: ScenarioStack, *, bound: float, stall: StallGate | None = None
                 ) -> RequestRun:
    if stall is not None:
        stall.arm()
    return stack.start_request()


# --------------------------------------------------------------------------- #
# The three scenarios
# --------------------------------------------------------------------------- #


def run_s1(*, run_id: str, evidence_dir: Path) -> tuple[dict[str, Any], Path]:
    """S1 - 20 sequential requests, every one reaching a successful terminal."""

    scenario = SCENARIOS["S1"]
    started = time.monotonic()
    record = _base_record(scenario, run_id=run_id)
    stack = ScenarioStack(request_prefix="l3-s1")
    runs: list[RequestRun] = []
    observations: dict[str, Any] = {}
    try:
        record["infrastructure"] = stack.infrastructure_facts()
        record["scenario_parameters"]["prompt"] = stack.harness.PROMPT
        records: list[dict[str, Any]] = []
        for _ in range(scenario.requests):
            run = stack.start_request()
            run.drive(bound_seconds=REQUEST_BOUND_SECONDS)
            records.append(run.record(path_id=stack.route_path_id(run)))
            runs.append(run)
        record["concurrency"] = concurrency_window(records)
        observations = {
            "requests": records,
            "gateway_knobs": record["infrastructure"]["gateway_knobs"],
        }
        record["requests"] = records
        record["counts"] = {
            "issued": len(records),
            "admitted": sum(1 for item in records if item["admitted_status"] == 202),
            "completed": sum(1 for item in records if item["terminal_kind"] == "completed"),
            "cancelled": sum(1 for item in records if item["terminal_kind"] == "cancelled"),
            "failed": sum(1 for item in records if item["terminal_kind"] == "failed"),
            "no_terminal": sum(1 for item in records if item["terminal_kind"] is None),
        }
        record["terminal_states"] = [
            {
                "index": item["index"],
                "terminal_kind": item["terminal_kind"],
                "terminal_code": item["terminal_code"],
                "token_count": item["token_count"],
            }
            for item in records
        ]
        record["timings"] = {
            "seconds_to_first_token": [
                item["seconds_to_first_token"] for item in records
            ],
            "seconds_to_terminal": [item["seconds_to_terminal"] for item in records],
        }
    finally:
        stack.close()
    record["gaps"] = _evaluate_s1(observations) if observations else ["scenario_did_not_run"]
    record["verdict"] = "GREEN" if not record["gaps"] else "RED"
    path = write_evidence(Path(evidence_dir) / "S1-sequential-20.json", _finish(record, started=started))
    return record, path


def run_s2(*, run_id: str, evidence_dir: Path) -> tuple[dict[str, Any], Path]:
    """S2 - 8 concurrent requests through the shipped gateway (requests, not loops)."""

    scenario = SCENARIOS["S2"]
    started = time.monotonic()
    record = _base_record(scenario, run_id=run_id)
    stack = ScenarioStack(
        request_prefix="l3-s2",
        max_concurrent_requests=scenario.requests,
    )
    observations: dict[str, Any] = {}
    try:
        record["infrastructure"] = stack.infrastructure_facts()
        record["scenario_parameters"]["prompt"] = stack.harness.PROMPT
        runs = stack.submit_concurrent(scenario.requests)
        records = [run.record(path_id=stack.route_path_id(run)) for run in runs]
        record["concurrency"] = concurrency_window(records)
        observations = {
            "requests": records,
            "concurrency": record["concurrency"],
            "gateway_knobs": record["infrastructure"]["gateway_knobs"],
        }
        record["requests"] = records
        record["counts"] = {
            "issued": len(records),
            "admitted": sum(1 for item in records if item["admitted_status"] == 202),
            "completed": sum(1 for item in records if item["terminal_kind"] == "completed"),
            "cancelled": sum(1 for item in records if item["terminal_kind"] == "cancelled"),
            "failed": sum(1 for item in records if item["terminal_kind"] == "failed"),
            "no_terminal": sum(1 for item in records if item["terminal_kind"] is None),
        }
        record["terminal_states"] = [
            {
                "index": item["index"],
                "terminal_kind": item["terminal_kind"],
                "terminal_code": item["terminal_code"],
                "token_count": item["token_count"],
            }
            for item in records
        ]
    finally:
        stack.close()
    record["gaps"] = _evaluate_s2(observations) if observations else ["scenario_did_not_run"]
    record["verdict"] = "GREEN" if not record["gaps"] else "RED"
    path = write_evidence(Path(evidence_dir) / "S2-concurrent-8.json", _finish(record, started=started))
    return record, path


def run_s3(*, run_id: str, evidence_dir: Path) -> tuple[dict[str, Any], Path]:
    """S3 - 10 cancellations mid-stream, then the next request must succeed."""

    scenario = SCENARIOS["S3"]
    started = time.monotonic()
    record = _base_record(scenario, run_id=run_id)
    stall = StallGate()
    stack = ScenarioStack(request_prefix="l3-s3", stall=stall)
    observations: dict[str, Any] = {}
    try:
        record["infrastructure"] = stack.infrastructure_facts()
        record["scenario_parameters"]["prompt"] = stack.harness.PROMPT
        cancellations: list[dict[str, Any]] = []
        for index in range(1, scenario.cancellations + 1):
            run = _one_request(stack, bound=REQUEST_BOUND_SECONDS, stall=stall)
            entry: dict[str, Any] = {"index": index, "request_id": run.request_id}
            if run.subscription is None:
                entry.update(
                    {
                        "stall_entered_seconds": None,
                        "cancel_accepted": False,
                        "tokens_before_cancel": 0,
                        "terminal_kind": run.terminal_kind,
                        "terminal_code": run.terminal_code,
                        "error": run.error,
                    }
                )
                cancellations.append(entry)
                stall.disarm()
                continue
            driver = threading.Thread(
                target=run.drive, name=f"l3-s3-cancel-{index}", daemon=True
            )
            driver.start()
            # The request must be genuinely in flight: a decode of this request
            # has to be stalled on the far node before the cancellation is issued.
            entered = stall.wait_entered(timeout=STALL_BOUND_SECONDS)
            entry["stall_entered_seconds"] = (
                None if not entered else time.monotonic() - run.submitted_at
            )
            if entered:
                deadline = time.monotonic() + MID_STREAM_TOKEN_BOUND_SECONDS
                while run.tokens_delivered() < 1 and time.monotonic() < deadline:
                    time.sleep(0.005)
            entry["tokens_before_cancel"] = run.tokens_delivered()
            entry["cancel_accepted"] = stack.cancel(run)
            run.cancel_accepted = entry["cancel_accepted"]
            stall.release()
            driver.join(timeout=REQUEST_BOUND_SECONDS)
            entry["driver_finished"] = not driver.is_alive()
            path_id = stack.route_path_id(run)
            entry.update(run.record(path_id=path_id))
            entry["cleanup"] = stack.wait_for_cleanup(run, path_id)
            entry["stall"] = stall.snapshot()
            cancellations.append(entry)
            stall.disarm()
        # The next request, after ten cancellations: it must complete.
        stall.disarm()
        following = stack.start_request()
        following.drive(bound_seconds=REQUEST_BOUND_SECONDS)
        next_record = following.record(path_id=stack.route_path_id(following))
        observations = {
            "cancellations": cancellations,
            "next_request": next_record,
            "gateway_knobs": record["infrastructure"]["gateway_knobs"],
        }
        record["cancellations"] = cancellations
        record["next_request"] = next_record
        record["counts"] = {
            "cancellations_issued": len(cancellations),
            "cancellations_accepted": sum(
                1 for item in cancellations if item.get("cancel_accepted")
            ),
            "cancelled_terminals": sum(
                1 for item in cancellations if item.get("terminal_kind") == "cancelled"
            ),
            "mid_stream_cancellations": sum(
                1 for item in cancellations if item.get("tokens_before_cancel", 0) >= 1
            ),
            "next_request_terminal": next_record["terminal_kind"],
        }
        record["timings"] = {
            "seconds_cancel_to_terminal": [
                item.get("seconds_cancel_to_terminal") for item in cancellations
            ]
        }
    finally:
        stall.disarm()
        stack.close()
    record["gaps"] = _evaluate_s3(observations) if observations else ["scenario_did_not_run"]
    record["verdict"] = "GREEN" if not record["gaps"] else "RED"
    path = write_evidence(
        Path(evidence_dir) / "S3-cancellations-10.json", _finish(record, started=started)
    )
    return record, path


RUNNERS: dict[str, Callable[..., tuple[dict[str, Any], Path]]] = {
    "S1": run_s1,
    "S2": run_s2,
    "S3": run_s3,
}


# --------------------------------------------------------------------------- #
# Command line: one scenario, one command, one evidence record
# --------------------------------------------------------------------------- #


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mycelium_testbed.l3_scenarios",
        description=(
            "Run one L3 scenario (S1/S2/S3) on the shipped request plane and write "
            "one JSON evidence record.  Exit code 0 = GREEN, 1 = RED (an honest "
            "verdict, including a reproduced gap), 2 = the scenario could not run."
        ),
    )
    parser.add_argument("scenario", choices=["S1", "S2", "S3", "all"])
    parser.add_argument(
        "--evidence-dir",
        required=True,
        help="absolute directory for the JSON evidence record(s)",
    )
    parser.add_argument("--run-id", default=None, help="run identity stamped in the record")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    evidence_dir = Path(arguments.evidence_dir)
    if not evidence_dir.is_absolute():
        print("l3_scenarios: --evidence-dir must be absolute", file=sys.stderr)
        return 2
    run_id = arguments.run_id or f"l3-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    identifiers = list(SCENARIOS) if arguments.scenario == "all" else [arguments.scenario]
    worst = 0
    for identifier in identifiers:
        started = time.monotonic()
        try:
            record, path = RUNNERS[identifier](run_id=run_id, evidence_dir=evidence_dir)
        except ScenarioError as error:
            print(f"L3_{identifier} UNRUNNABLE {error}")
            worst = max(worst, 2)
            continue
        summary = {
            "scenario": identifier,
            "verdict": record["verdict"],
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "counts": record.get("counts"),
            "gaps": record["gaps"],
            "evidence": str(path),
        }
        print("L3_EVIDENCE " + json.dumps(summary, sort_keys=True, default=str))
        if record["verdict"] != "GREEN":
            worst = max(worst, 1)
    return worst


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
