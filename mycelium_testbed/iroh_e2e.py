# SPDX-License-Identifier: AGPL-3.0-only
"""Subscribe the testbed trace to the *existing* iroh e2e harness.

The packet for WP2 is explicit: reuse ``tests/e2e_request_iroh/harness.py`` (real
ASGI gateway + multi-stage router + iroh sidecar + ``CompleteRequestEvidence`` /
``CancellationEvidence``) rather than re-implementing it, and never add a
production hook to make tracing convenient.

So this module does exactly two things:

* :class:`TracedIrohTransport` - an :class:`IrohTransport` subclass that records
  ``enqueue`` / ``receipt`` / ``delivered`` for the frames the transport already
  moves.  Subclassing is the seam the harness itself already uses
  (``_PausedAfterNativeReceive``); production code is untouched.
* :class:`TracedStageRuntime` - a delegating wrapper that records ``started`` /
  ``finished`` around the command the runtime already executes.

:func:`run_traced_complete_request` composes those with the existing
``run_complete_request`` by temporarily replacing two *test-harness* factories
(``_make_transport``, ``_DeterministicStageRuntime``) for the duration of one
run.  Nothing in ``mycelium_*`` is modified, monkey-patched or re-implemented.

The seam this cannot cover is recorded in ``mycelium_testbed/DESIGN_NOTES.md``
for WP5: in-product lifecycle stamps (a real observer on the router's command
lifecycle and on the transport's send/receive path, stamped in the sender's own
frame) would remove the need for a test-side subclass and would survive into a
physical run.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from mycelium_router.transports.iroh import IrohTransport, IrohTransportError
from mycelium_router.wire import WireError, decode_frame
from mycelium_testbed.trace import (
    ClockOffset,
    EventKind,
    ExchangeSample,
    TraceError,
    TraceEvent,
    TraceRecorder,
    TraceRunDirectory,
    estimate_offsets_by_host,
    load_run,
    summarise,
)

DEFAULT_HOST_IDS: tuple[str, ...] = ("node-a", "node-c")
DEFAULT_REFERENCE_HOST = "node-a"
PRODUCT_ROOTS: tuple[str, ...] = (
    "mycelium_router",
    "mycelium_request_gateway",
    "mycelium_iroh_sidecar",
    "mycelium_qualification",
)


def frame_digest(frame: bytes) -> str:
    """Stable identity of a wire frame - the only cross-host pairing key."""

    return "sha256:" + hashlib.sha256(frame).hexdigest()


def frame_scope(frame: bytes) -> tuple[str | None, str | None, str, str | None]:
    """``(request_id, path_id, message_type, placement_id)`` of a frame, best effort."""

    try:
        decoded = decode_frame(frame)
    except WireError:
        return (None, None, "undecodable", None)
    message = decoded.message
    header = getattr(message, "header", None)
    request = getattr(message, "request", None)
    request_id = getattr(message, "request_id", None) or getattr(
        header, "request_id", None
    ) or getattr(request, "request_id", None)
    path_id = getattr(message, "path_id", None) or getattr(header, "path_id", None)
    placement_id = getattr(message, "destination_placement_id", None) or getattr(
        header, "destination_placement_id", None
    )
    return (
        request_id if isinstance(request_id, str) else None,
        path_id if isinstance(path_id, str) else None,
        type(message).__name__,
        placement_id if isinstance(placement_id, str) else None,
    )


class TracedIrohTransport(IrohTransport):
    """Records what the transport already does; changes no behaviour."""

    def __init__(self, *, recorder: TraceRecorder, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._testbed_recorder = recorder

    def send_router_frame(  # type: ignore[override]
        self, frame: bytes, **kwargs: Any
    ) -> Any:
        recorder = self._testbed_recorder
        request_id, path_id, message_type, placement_id = frame_scope(frame)
        digest = frame_digest(frame)
        destination = kwargs.get("destination_node_id")
        recorder.event(
            EventKind.ENQUEUE,
            request_id=request_id,
            placement_id=placement_id,
            detail={
                "frame_digest": digest,
                "message": message_type,
                "path_id": path_id,
                "destination_node_id": destination,
                "bytes": len(frame),
                "origin": "transport.send_router_frame",
            },
        )
        try:
            receipt = super().send_router_frame(frame, **kwargs)
        except IrohTransportError as error:
            recorder.event(
                EventKind.TERMINAL,
                request_id=request_id,
                placement_id=placement_id,
                status=f"send_failed:{error.code}",
                detail={"frame_digest": digest, "code": error.code, "detail": error.detail},
            )
            raise
        recorder.event(
            EventKind.RECEIPT,
            request_id=request_id,
            placement_id=placement_id,
            detail={
                "frame_digest": digest,
                "message_id": receipt.message_id.hex(),
                "peer_endpoint_id": receipt.peer_endpoint_id,
                "peer_generation": receipt.peer_generation,
                "semantics": receipt.semantics,
                "origin": "transport.send_router_frame",
            },
        )
        return receipt

    def _recv(self, client: Any):  # type: ignore[override]
        delivery = super()._recv(client)
        if delivery is None:
            return delivery
        recorder = self._testbed_recorder
        message_id, source_node_id, generation, frame = delivery
        request_id, path_id, message_type, placement_id = frame_scope(frame)
        recorder.event(
            EventKind.DELIVERED,
            request_id=request_id,
            placement_id=placement_id,
            detail={
                "frame_digest": frame_digest(frame),
                "message": message_type,
                "message_id": message_id.hex(),
                "source_node_id": source_node_id,
                "peer_generation": generation,
                "path_id": path_id,
                "bytes": len(frame),
                "origin": "transport._recv",
            },
        )
        return delivery


def command_id_for(item: Any) -> str:
    """Deterministic command identity for a router work item."""

    idempotency_key = getattr(item, "idempotency_key", None)
    if isinstance(idempotency_key, str) and idempotency_key:
        return f"cmd-{idempotency_key}"
    return (
        f"cmd-{getattr(item, 'request_id', 'unknown')}"
        f"-{getattr(item, 'placement_id', 'unknown')}"
        f"-{getattr(item, 'phase', 'unknown')}"
        f"-{getattr(item, 'token_index', 0)}"
    )


class TracedStageRuntime:
    """Delegates to a Router runtime port, recording ``started`` / ``finished``."""

    def __init__(
        self,
        inner: Any,
        *,
        recorder_for_placement: Callable[[str], TraceRecorder],
    ) -> None:
        self._testbed_inner = inner
        self._testbed_recorder_for_placement = recorder_for_placement

    def __getattr__(self, name: str) -> Any:
        return getattr(self._testbed_inner, name)

    def _recorder(self, item: Any) -> TraceRecorder:
        placement_id = getattr(item, "placement_id", None)
        if not isinstance(placement_id, str) or not placement_id:
            raise TraceError(
                f"traced_runtime_item_without_placement:{type(item).__name__}"
            )
        return self._testbed_recorder_for_placement(placement_id)

    def _started(self, item: Any) -> tuple[TraceRecorder, str]:
        recorder = self._recorder(item)
        command_id = command_id_for(item)
        recorder.event(
            EventKind.STARTED,
            request_id=getattr(item, "request_id", None),
            command_id=command_id,
            placement_id=item.placement_id,
            detail={
                "phase": getattr(item, "phase", None),
                "token_index": getattr(item, "token_index", None),
                "hop_index": getattr(item, "hop_index", None),
                "path_id": getattr(item, "path_id", None),
                "path_attempt": getattr(item, "path_attempt", None),
                "origin": "runtime.execute",
            },
        )
        return recorder, command_id

    def _finished(
        self,
        recorder: TraceRecorder,
        item: Any,
        command_id: str,
        result: Any,
    ) -> None:
        success = bool(getattr(result, "success", True))
        reason = getattr(result, "failure_reason", None) or "runtime_failure"
        token_id = getattr(result, "token_id", None)
        recorder.event(
            EventKind.FINISHED,
            request_id=getattr(item, "request_id", None),
            command_id=command_id,
            placement_id=item.placement_id,
            status="ok" if success else f"failed:{reason}",
            detail={
                "phase": getattr(item, "phase", None),
                "token_index": getattr(item, "token_index", None),
                "token_id": token_id,
                "origin": "runtime.execute",
            },
        )

    def execute(self, item: Any) -> Any:
        recorder, command_id = self._started(item)
        try:
            result = self._testbed_inner.execute(item)
        except BaseException as error:
            recorder.event(
                EventKind.FINISHED,
                request_id=getattr(item, "request_id", None),
                command_id=command_id,
                placement_id=item.placement_id,
                status=f"raised:{type(error).__name__}",
                detail={"error": str(error)[:200], "origin": "runtime.execute"},
            )
            raise
        self._finished(recorder, item, command_id, result)
        return result

    def execute_batch(self, batch: Any) -> Any:
        items = tuple(getattr(batch, "items", ()))
        started = [self._started(item) for item in items]
        try:
            results = self._testbed_inner.execute_batch(batch)
        except BaseException as error:
            for recorder, command_id in started:
                recorder.event(
                    EventKind.FINISHED,
                    request_id=None,
                    command_id=command_id,
                    status=f"raised:{type(error).__name__}",
                    detail={"error": str(error)[:200], "origin": "runtime.execute_batch"},
                )
            raise
        if len(results) != len(items):
            raise TraceError(
                f"runtime_batch_result_mismatch:items={len(items)}:results={len(results)}"
            )
        for item, (recorder, command_id), result in zip(items, started, results):
            self._finished(recorder, item, command_id, result)
        return results


@dataclass(frozen=True)
class TracedIrohRun:
    """The result of one traced iroh e2e run."""

    run_dir: Path
    summary_path: Path
    request_id: str
    reference_host: str
    host_ids: tuple[str, ...]
    events: tuple[TraceEvent, ...]
    samples: tuple[ExchangeSample, ...]
    offsets: Mapping[str, ClockOffset]
    summary: str
    evidence: Any


@contextmanager
def _patched(module: Any, attribute: str, replacement: Any) -> Iterator[None]:
    original = getattr(module, attribute)
    setattr(module, attribute, replacement)
    try:
        yield
    finally:
        setattr(module, attribute, original)


def _load_iroh_harness() -> Any:
    """Import the existing harness lazily, so this module imports from any cwd."""

    try:
        from tests.e2e_request_iroh import harness  # noqa: PLC0415 - lazy on purpose

        return harness
    except ImportError as error:  # pragma: no cover - depends on the caller's cwd
        raise TraceError(
            f"iroh_e2e_harness_not_importable:{error}:run_from_repository_root"
        ) from error


def placement_hosts(harness: Any) -> dict[str, str]:
    """``placement_id -> node_id`` from the harness's own deterministic graph."""

    graph = harness._local_three_stage_graph()
    mapping: dict[str, str] = {}
    for stage in graph.stages:
        for placement in stage.placements:
            mapping[placement.placement_id] = placement.node_id
    if not mapping:
        raise TraceError("harness_graph_has_no_placements")
    return mapping


def _product_package_digest(repository_root: Path) -> str | None:
    digest = hashlib.sha256()
    files: list[Path] = []
    for name in PRODUCT_ROOTS:
        root = repository_root / name
        if not root.is_dir():
            continue
        files.extend(
            path
            for path in root.rglob("*.py")
            if path.is_file() and "__pycache__" not in path.parts
        )
    if not files:
        return None
    for path in sorted(files):
        digest.update(path.relative_to(repository_root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\n")
    return "sha256:" + digest.hexdigest()


def run_traced_complete_request(
    native_binary: str | Path,
    run_dir: str | Path,
    *,
    reference_host: str = DEFAULT_REFERENCE_HOST,
    host_ids: Sequence[str] = DEFAULT_HOST_IDS,
    fsync: bool = True,
    request_id: str | None = None,
) -> TracedIrohRun:
    """Run the existing complete-request e2e once, with the lifecycle trace on."""

    harness = _load_iroh_harness()
    repository_root = Path(harness.__file__).resolve().parents[2]
    hosts = tuple(host_ids)
    if reference_host not in hosts:
        raise TraceError(f"reference_host_not_among_host_ids:{reference_host}:{hosts}")
    traced_request_id = request_id or harness.REQUEST_ID
    mapping = placement_hosts(harness)

    original_make_transport = harness._make_transport
    original_runtime_class = harness._DeterministicStageRuntime

    with TraceRunDirectory(
        run_dir,
        run_id=Path(run_dir).name,
        host_id=reference_host,
        reference_host=reference_host,
        fsync=fsync,
        product_package_digest=_product_package_digest(repository_root),
        product_roots=tuple(str(repository_root / name) for name in PRODUCT_ROOTS),
        notes=(
            "lifecycle trace of tests/e2e_request_iroh/harness.run_complete_request; "
            "frames paired by sha256(frame) between host recorders"
        ),
    ) as run:
        recorders = {
            host: TraceRecorder(host, run=run, role="traced_iroh_e2e") for host in hosts
        }

        def recorder_for_placement(placement_id: str) -> TraceRecorder:
            node_id = mapping.get(placement_id)
            if node_id is None:
                raise TraceError(f"unknown_placement_in_harness_graph:{placement_id}")
            recorder = recorders.get(node_id)
            if recorder is None:
                # A placement the graph reserves but this run never routes to is
                # fine; one that actually executes on an untraced host is not.
                raise TraceError(f"no_recorder_for_placement_host:{placement_id}:{node_id}")
            return recorder

        def transport_factory(
            node_id: str, sidecar: Any, peer_node_id: str, peer: Any, **kwargs: Any
        ) -> Any:
            kwargs.setdefault(
                "transport_type",
                lambda **transport_kwargs: TracedIrohTransport(
                    recorder=recorders[node_id], **transport_kwargs
                ),
            )
            return original_make_transport(node_id, sidecar, peer_node_id, peer, **kwargs)

        runtime_factory = lambda **kwargs: TracedStageRuntime(  # noqa: E731
            inner=original_runtime_class(**kwargs),
            recorder_for_placement=recorder_for_placement,
        )

        recorders[reference_host].event(
            EventKind.ENQUEUE,
            request_id=traced_request_id,
            detail={
                "origin": "gateway_submit",
                "prompt": harness.PROMPT,
                "hosts": list(hosts),
            },
        )
        with _patched(harness, "_make_transport", transport_factory), _patched(
            harness, "_DeterministicStageRuntime", runtime_factory
        ):
            try:
                evidence = harness.run_complete_request(Path(native_binary))
            except BaseException as error:
                recorders[reference_host].event(
                    EventKind.TERMINAL,
                    request_id=traced_request_id,
                    status=f"failed:{type(error).__name__}",
                    detail={"error": str(error)[:300], "origin": "gateway_submit"},
                )
                raise
        recorders[reference_host].event(
            EventKind.TERMINAL,
            request_id=traced_request_id,
            status="completed",
            detail={
                "origin": "gateway_submit",
                "token_indexes": list(evidence.token_indexes),
                "acknowledged_cursor": evidence.acknowledged_cursor,
                "local_evidence_only": evidence.local_evidence_only,
                "route_ready": evidence.route_ready,
            },
        )

    loaded = load_run(run_dir)
    summary = summarise(run_dir, reference_host=reference_host)
    summary_path = Path(run_dir) / "summary.txt"
    summary_path.write_text(summary, encoding="utf-8")
    try:
        offsets = estimate_offsets_by_host(loaded.samples, reference_host=reference_host)
    except TraceError:
        offsets = {}
    return TracedIrohRun(
        run_dir=Path(run_dir),
        summary_path=summary_path,
        request_id=traced_request_id,
        reference_host=reference_host,
        host_ids=hosts,
        events=loaded.events,
        samples=loaded.samples,
        offsets=offsets,
        summary=summary,
        evidence=evidence,
    )


__all__ = [
    "DEFAULT_HOST_IDS",
    "DEFAULT_REFERENCE_HOST",
    "PRODUCT_ROOTS",
    "TracedIrohRun",
    "TracedIrohTransport",
    "TracedStageRuntime",
    "command_id_for",
    "frame_digest",
    "frame_scope",
    "placement_hosts",
    "run_traced_complete_request",
]
