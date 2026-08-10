"""Atomic selection across independently qualified live deployments."""
from __future__ import annotations

from dataclasses import dataclass, replace
import copy
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping
import uuid

from mycelium_router.contracts import ExecutionGraph, RequestContext

from .health import LIVE_QUALIFICATION_REFRESH_AFTER_MS
from .route import LiveRoute, RouteCounters
from .router_port import LiveRouterPort


class DeploymentSelectionError(RuntimeError):
    """A requested deployment switch cannot be performed safely."""


@dataclass(frozen=True, slots=True)
class QualifiedDeploymentRuntime:
    deployment_id: str
    model_id: str
    quantization: str
    qualified_at_unix_ms: int
    route: LiveRoute
    graph: ExecutionGraph
    codec: Any
    qualification: Any
    placement_projection: Mapping[str, Any] | None = None
    topology_projection: Mapping[str, Any] | None = None


class LiveDeploymentRegistry:
    """QualificationSource, RouterPort, and codec for one selected deployment."""

    is_simulated = False

    def __init__(
        self,
        runtimes: list[QualifiedDeploymentRuntime],
        *,
        state_path: Path | None = None,
        qualification_refresher: (
            Callable[[QualifiedDeploymentRuntime], Any] | None
        ) = None,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        if len(runtimes) < 2:
            raise ValueError("deployment_registry_requires_two_or_more")
        if len({runtime.deployment_id for runtime in runtimes}) != len(runtimes):
            raise ValueError("deployment_registry_duplicate")
        self._runtimes = {runtime.deployment_id: runtime for runtime in runtimes}
        self._routers = {
            runtime.deployment_id: LiveRouterPort(
                route=runtime.route,
                execution_graph=runtime.graph,
            )
            for runtime in runtimes
        }
        self._selected = runtimes[0].deployment_id
        self._requests: dict[str, str] = {}
        self._incidents: list[dict[str, Any]] = []
        self._incident_sequence = 0
        self._promotion_previous: dict[str, str] = {}
        self._promotion_reports: dict[str, Mapping[str, Any]] = {}
        self._candidate_canaries: set[str] = set()
        self._thread_binding = threading.local()
        self._lock = threading.RLock()
        self._qualification_refresher = qualification_refresher
        self._clock_unix_ms = clock_unix_ms or (lambda: int(time.time() * 1_000))
        self._state_path = None if state_path is None else Path(state_path)
        restored = self._restored_selection()
        if restored is not None:
            self._selected = restored
        self._persist()

    def _restored_selection(self) -> str | None:
        path = self._state_path
        if path is None or not path.is_file() or path.is_symlink():
            return None
        try:
            if path.stat().st_size > 1_048_576:
                return None
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, RecursionError):
            return None
        if not isinstance(document, dict):
            return None
        deployment_id = document.get("selected_deployment_id")
        runtime = self._runtimes.get(deployment_id)
        if (
            document.get("protocol") != "mycelium.live_deployment_registry.v1"
            or runtime is None
            or not runtime.route.is_alive()
            or runtime.qualification.route_ready is not True
        ):
            return None
        return runtime.deployment_id

    def _current(self) -> QualifiedDeploymentRuntime:
        return self._runtimes[self._selected]

    def _refresh_current_if_needed(self) -> QualifiedDeploymentRuntime:
        runtime = self._current()
        issued_at = getattr(runtime.qualification, "issued_at_unix_ms", None)
        if (
            self._qualification_refresher is None
            or self._requests
            or type(issued_at) is not int
            or self._clock_unix_ms() - issued_at
            < LIVE_QUALIFICATION_REFRESH_AFTER_MS
        ):
            return runtime
        try:
            qualification = self._qualification_refresher(runtime)
        except Exception:
            # Preserve the previous binding so later reads can retry. Browser
            # freshness admission remains fail closed once its hour elapses.
            return runtime
        if (
            qualification.route_ready is not True
            or qualification.deployment_id != runtime.deployment_id
            or qualification.model_id != runtime.model_id
        ):
            return runtime
        refreshed = replace(
            runtime,
            qualified_at_unix_ms=qualification.issued_at_unix_ms,
            qualification=qualification,
        )
        self._runtimes[runtime.deployment_id] = refreshed
        self._persist()
        return refreshed

    def current(self) -> Any | None:
        with self._lock:
            runtime = self._refresh_current_if_needed()
            return runtime.qualification if runtime.route.is_alive() else None

    def current_deployment(self) -> ExecutionGraph:
        with self._lock:
            return self._current().graph

    def encode(self, prompt: str) -> tuple[int, ...]:
        with self._lock:
            runtime = self._current()
            self._thread_binding.deployment_id = runtime.deployment_id
            return runtime.codec.encode(prompt)

    def policy_response(self, prompt: str) -> str | None:
        with self._lock:
            runtime = self._current()
            self._thread_binding.deployment_id = runtime.deployment_id
            policy = getattr(runtime.codec, "policy_response", None)
            return None if not callable(policy) else policy(prompt)

    def decode_token(self, token_id: int) -> str:
        deployment_id = getattr(self._thread_binding, "deployment_id", None)
        with self._lock:
            runtime = self._runtimes.get(deployment_id)
            if runtime is None:
                raise RuntimeError("request_deployment_binding_missing")
            return runtime.codec.decode_token(token_id)

    def admit(
        self,
        request: RequestContext,
        client_sink: object,
        *,
        pinned_deployment: ExecutionGraph | None = None,
        **kwargs: object,
    ) -> str:
        with self._lock:
            runtime = self._current()
            bound = getattr(self._thread_binding, "deployment_id", None)
            if (
                bound != runtime.deployment_id
                or pinned_deployment is None
                or pinned_deployment.deployment_id != runtime.deployment_id
            ):
                raise RuntimeError("deployment_changed_during_admission")
            if request.request_id in self._requests:
                raise RuntimeError("duplicate_request_id")
            self._requests[request.request_id] = runtime.deployment_id
            router = self._routers[runtime.deployment_id]
        try:
            return router.admit(
                request,
                client_sink,
                pinned_deployment=pinned_deployment,
                **kwargs,
            )
        except BaseException:
            self.release_request(request.request_id)
            raise

    def _request_router(self, request_id: str) -> LiveRouterPort | None:
        with self._lock:
            deployment_id = self._requests.get(request_id)
            return None if deployment_id is None else self._routers[deployment_id]

    def decode_one(self, request_id: str) -> bool:
        router = self._request_router(request_id)
        return False if router is None else router.decode_one(request_id)

    def request_status(self, request_id: str) -> str:
        router = self._request_router(request_id)
        return "UNKNOWN" if router is None else router.request_status(request_id)

    def cancel(self, request_id: str) -> bool:
        router = self._request_router(request_id)
        return False if router is None else router.cancel(request_id)

    def release_request(self, request_id: str) -> None:
        with self._lock:
            deployment_id = self._requests.pop(request_id, None)
            router = (
                None if deployment_id is None else self._routers.get(deployment_id)
            )
        if router is not None:
            router.release_request(request_id)

    def select(self, deployment_id: str) -> Mapping[str, Any]:
        with self._lock:
            if deployment_id not in self._runtimes:
                raise DeploymentSelectionError("deployment_unknown")
            if self._requests:
                raise DeploymentSelectionError("deployment_switch_busy")
            target = self._runtimes[deployment_id]
            if not target.route.is_alive() or target.qualification.route_ready is not True:
                raise DeploymentSelectionError("deployment_not_qualified")
            previous = self._current()
            self._selected = deployment_id
            if previous.deployment_id != deployment_id:
                previous_status = previous.route.public_status()
                failed = (
                    not previous.route.is_alive()
                    or previous_status.get("counters", {}).get("fatal") is not None
                )
                self._incident_sequence += 1
                self._incidents.append(
                    {
                        "protocol": "mycelium.live_route_incident.v1",
                        "incident_id": f"registry-incident-{self._incident_sequence}",
                        "deployment_id": previous.deployment_id,
                        "request_id": None,
                        "state": (
                            "qualified_failover_selected"
                            if failed
                            else "qualified_deployment_selected"
                        ),
                        "reason": (
                            str(
                                previous_status.get("counters", {}).get("fatal")
                                or "route_unavailable"
                            )
                            if failed
                            else "operator_selection"
                        ),
                        "observed_at_unix_ms": int(time.time() * 1_000),
                    }
                )
                self._incidents = self._incidents[-64:]
            status = self.registry_status()
            self._persist(status)
            return status

    def promote_candidate(self, report: Mapping[str, Any]) -> Mapping[str, Any]:
        """Atomically promote a qualified planner-v2 candidate without rebinding requests."""

        from mycelium_candidate_promotion import validate_candidate_promotion_report

        validated = validate_candidate_promotion_report(report)
        with self._lock:
            candidate_id = validated["candidate_deployment_id"]
            incumbent_id = validated["incumbent_deployment_id"]
            if validated["decision"] != "promote":
                raise DeploymentSelectionError("candidate_evidence_rejected")
            if incumbent_id != self._selected:
                raise DeploymentSelectionError("candidate_incumbent_changed")
            target = self._runtimes.get(candidate_id)
            if target is None:
                raise DeploymentSelectionError("deployment_unknown")
            if candidate_id in self._candidate_canaries:
                raise DeploymentSelectionError("candidate_canary_active")
            if not target.route.is_alive() or target.qualification.route_ready is not True:
                raise DeploymentSelectionError("deployment_not_qualified")
            self._promotion_previous[candidate_id] = incumbent_id
            self._promotion_reports[candidate_id] = copy.deepcopy(validated)
            self._selected = candidate_id
            self._incident_sequence += 1
            self._incidents.append(
                {
                    "protocol": "mycelium.live_route_incident.v1",
                    "incident_id": f"registry-incident-{self._incident_sequence}",
                    "deployment_id": candidate_id,
                    "request_id": None,
                    "state": "qualified_candidate_promoted",
                    "reason": validated["planner_snapshot_digest"],
                    "observed_at_unix_ms": int(time.time() * 1_000),
                }
            )
            self._incidents = self._incidents[-64:]
            status = self.registry_status()
            self._persist(status)
            return status

    def canary_candidate(
        self,
        candidate_id: str,
        *,
        case_id: str,
        prompt: str,
        max_new_tokens: int,
    ) -> Mapping[str, Any]:
        """Exercise one qualified candidate without changing public selection."""

        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or not isinstance(case_id, str)
            or not case_id
            or len(case_id) > 128
            or not isinstance(prompt, str)
            or not prompt.strip()
            or len(prompt) > 4_096
            or type(max_new_tokens) is not int
            or not 1 <= max_new_tokens <= 64
        ):
            raise DeploymentSelectionError("candidate_canary_invalid")
        with self._lock:
            if candidate_id == self._selected:
                raise DeploymentSelectionError("candidate_already_selected")
            runtime = self._runtimes.get(candidate_id)
            if runtime is None:
                raise DeploymentSelectionError("deployment_unknown")
            if not runtime.route.is_alive() or runtime.qualification.route_ready is not True:
                raise DeploymentSelectionError("deployment_not_qualified")
            if candidate_id in self._candidate_canaries:
                raise DeploymentSelectionError("candidate_canary_active")
            self._candidate_canaries.add(candidate_id)

        request_id = f"candidate-canary-{uuid.uuid4().hex}"
        emitted_at: list[float] = []

        class _CanarySink:
            def emit(self, _token_index: int, _token_id: int) -> None:
                emitted_at.append(time.monotonic())

        started_at = time.monotonic()
        try:
            prompt_token_ids = runtime.codec.encode(prompt)
            result = runtime.route.infer(
                prompt_token_ids,
                max_new_tokens=max_new_tokens,
                request_id=request_id,
                sink=_CanarySink(),
            )
            completed_at = time.monotonic()
            status = runtime.route.public_status()
            recent = status.get("recent_inferences", [])
            inference = recent[-1] if isinstance(recent, list) and recent else {}
            peer_deltas = inference.get("peer_counter_deltas", [])
            frames_by_node = {
                item["node_id"]: int(item.get("frames_sent", 0))
                + int(item.get("frames_received", 0))
                for item in peer_deltas
                if isinstance(item, Mapping) and isinstance(item.get("node_id"), str)
            }
            elapsed_ms = max((completed_at - started_at) * 1_000.0, 0.001)
            ttft_ms = (
                max((emitted_at[0] - started_at) * 1_000.0, 0.001)
                if emitted_at
                else elapsed_ms
            )
            tpot_ms = inference.get("tpot_ms")
            if not isinstance(tpot_ms, (int, float)) or isinstance(tpot_ms, bool):
                tpot_ms = elapsed_ms / max(len(result.token_ids), 1)
            return {
                "protocol": "mycelium.candidate_canary_result.v1",
                "case_id": case_id,
                "candidate_deployment_id": candidate_id,
                "model_id": runtime.model_id,
                "completed": True,
                "output_text": "".join(
                    runtime.codec.decode_token(token_id)
                    for token_id in result.token_ids
                ),
                "output_token_count": len(result.token_ids),
                "ttft_ms": ttft_ms,
                "tpot_ms": max(float(tpot_ms), 0.001),
                "total_ms": elapsed_ms,
                "output_tokens_per_second": max(
                    len(result.token_ids) * 1_000.0 / elapsed_ms,
                    0.001,
                ),
                "frames_per_request_by_stage": {
                    stage.stage_id: sum(
                        frames_by_node.get(placement.node_id, 0)
                        for placement in stage.placements
                    )
                    for stage in runtime.graph.stages
                },
            }
        finally:
            runtime.route.release_request(request_id)
            with self._lock:
                self._candidate_canaries.discard(candidate_id)

    def rollback_candidate(self, candidate_id: str, *, reason: str) -> Mapping[str, Any]:
        """Restore the captured incumbent while admitted requests keep their original router."""

        with self._lock:
            incumbent_id = self._promotion_previous.get(candidate_id)
            if self._selected != candidate_id or incumbent_id is None:
                raise DeploymentSelectionError("candidate_rollback_unavailable")
            incumbent = self._runtimes.get(incumbent_id)
            if (
                incumbent is None
                or not incumbent.route.is_alive()
                or incumbent.qualification.route_ready is not True
            ):
                raise DeploymentSelectionError("candidate_incumbent_unavailable")
            if not isinstance(reason, str) or not reason or len(reason) > 128:
                raise DeploymentSelectionError("candidate_rollback_reason_invalid")
            self._selected = incumbent_id
            self._promotion_previous.pop(candidate_id, None)
            self._incident_sequence += 1
            self._incidents.append(
                {
                    "protocol": "mycelium.live_route_incident.v1",
                    "incident_id": f"registry-incident-{self._incident_sequence}",
                    "deployment_id": candidate_id,
                    "request_id": None,
                    "state": "qualified_candidate_rolled_back",
                    "reason": reason,
                    "observed_at_unix_ms": int(time.time() * 1_000),
                }
            )
            self._incidents = self._incidents[-64:]
            status = self.registry_status()
            self._persist(status)
            return status

    def registry_status(self) -> Mapping[str, Any]:
        with self._lock:
            return {
                "protocol": "mycelium.live_deployment_registry.v1",
                "selected_deployment_id": self._selected,
                "switching_allowed": not self._requests,
                "deployments": [
                    {
                        "deployment_id": runtime.deployment_id,
                        "model_id": runtime.model_id,
                        "quantization": runtime.quantization,
                        "topology_size": len(runtime.graph.stages),
                        "health": (
                            "qualified" if runtime.route.is_alive() else "unavailable"
                        ),
                        "qualified_at_unix_ms": runtime.qualified_at_unix_ms,
                        "qualification_id": runtime.qualification.qualification_id,
                    }
                    for runtime in self._runtimes.values()
                ],
            }

    def _persist(self, status: Mapping[str, Any] | None = None) -> None:
        path = self._state_path
        if path is None:
            return
        if not path.is_absolute() or path.name in {"", ".", ".."}:
            raise ValueError("deployment_registry_state_path_invalid")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        encoded = (
            json.dumps(
                dict(status or self.registry_status()),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(encoded)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    def public_status(self) -> Mapping[str, Any]:
        with self._lock:
            runtime = self._current()
            placement = copy.deepcopy(runtime.placement_projection)
            topology = copy.deepcopy(runtime.topology_projection)
            report = self._promotion_reports.get(self._selected)
            incidents = copy.deepcopy(self._incidents)
        status = dict(runtime.route.public_status())
        with self._lock:
            if placement is not None and report is not None:
                placement["promotion"] = {
                    "candidate_deployment_id": report[
                        "candidate_deployment_id"
                    ],
                    "incumbent_deployment_id": report[
                        "incumbent_deployment_id"
                    ],
                    "decision": report["decision"],
                    "reasons": copy.deepcopy(report["reasons"]),
                    "sample_size": report["metrics"]["sample_size"],
                }
            status["placement"] = placement
            status["topology"] = topology
            route_incidents = status.get("incidents", [])
            status["incidents"] = [
                *(
                    list(route_incidents)
                    if isinstance(route_incidents, list)
                    else []
                ),
                *incidents,
            ][-64:]
            return status

    def membership_status(self, *, qualification: Any | None) -> Mapping[str, Any]:
        with self._lock:
            return self._current().route.membership_status(
                qualification=qualification
            )

    def product_membership_records(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            source = getattr(self._current().route, "product_membership_records", None)
            if not callable(source):
                raise RuntimeError("product_membership_source_unavailable")
            return tuple(dict(item) for item in source())

    def product_assignment_records(self) -> tuple[dict[str, Any], ...]:
        with self._lock:
            source = getattr(self._current().route, "product_assignment_records", None)
            if not callable(source):
                raise RuntimeError("product_assignment_source_unavailable")
            return tuple(dict(item) for item in source())

    def product_pseudonym_salt(self) -> bytes:
        with self._lock:
            source = getattr(self._current().route, "product_pseudonym_salt", None)
            if not callable(source):
                raise RuntimeError("product_pseudonym_salt_unavailable")
            return bytes(source())

    def counters(self) -> RouteCounters:
        with self._lock:
            return self._current().route.counters()

    def is_alive(self) -> bool:
        with self._lock:
            return self._current().route.is_alive()

    def close(self) -> None:
        for runtime in self._runtimes.values():
            runtime.route.close()

    def cleanup(self) -> None:
        for runtime in self._runtimes.values():
            runtime.route.cleanup()


__all__ = [
    "DeploymentSelectionError",
    "LiveDeploymentRegistry",
    "QualifiedDeploymentRuntime",
]
