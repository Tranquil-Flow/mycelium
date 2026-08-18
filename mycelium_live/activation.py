"""Operator-controlled activation of already-prepared physical deployments."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any, Callable, Mapping

from mycelium_physical_runner import load_operator_plan
from mycelium_physical_runner.errors import RunnerError
from physical_inference_node import execution_graph_from_document

from .registry import (
    LiveDeploymentRegistry,
    QualifiedDeploymentRuntime,
)


ACTIVATION_PROTOCOL = "mycelium.deployment_activation.v1"
MAX_CANDIDATES = 128
MAX_PLAN_BYTES = 2 * 1024 * 1024
_SAFE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_REVISION = re.compile(r"[0-9a-f]{40}\Z")
_PHASES = {
    "validating_plan": 1,
    "opening_route": 2,
    "qualifying_route": 3,
    "registering": 4,
}
_STATES = {"prepared", "activating", "qualified", "active", "unavailable", "failed"}
_STATUS_KEYS = {
    "protocol",
    "generation",
    "busy_candidate_id",
    "invalid_candidate_count",
    "candidates",
}
_CANDIDATE_KEYS = {
    "candidate_id",
    "deployment_id",
    "model_id",
    "model_revision",
    "quantization",
    "topology_size",
    "plan_digest",
    "state",
    "phase",
    "completed_steps",
    "total_steps",
    "reason_code",
}


class DeploymentActivationError(RuntimeError):
    """A bounded public activation failure."""

    def __init__(self, code: str) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("deployment_activation_error_code_invalid")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class PreparedDeployment:
    candidate_id: str
    deployment_id: str
    model_id: str
    model_revision: str
    quantization: str
    topology_size: int
    plan_digest: str
    plan_path: Path
    plan_bytes: bytes


@dataclass(frozen=True, slots=True)
class _Attempt:
    plan_digest: str
    state: str
    phase: str | None
    reason_code: str | None


RuntimeLoader = Callable[
    [Path, Callable[[str], None]],
    QualifiedDeploymentRuntime,
]


def validate_activation_status(document: Mapping[str, Any]) -> None:
    """Validate the closed browser-facing deployment activation contract."""
    if set(document) != _STATUS_KEYS or document.get("protocol") != ACTIVATION_PROTOCOL:
        raise ValueError("deployment_activation_status_invalid")
    generation = document.get("generation")
    invalid_count = document.get("invalid_candidate_count")
    busy_candidate_id = document.get("busy_candidate_id")
    candidates = document.get("candidates")
    if (
        not isinstance(generation, int)
        or isinstance(generation, bool)
        or generation < 0
        or not isinstance(invalid_count, int)
        or isinstance(invalid_count, bool)
        or invalid_count < 0
        or (
            busy_candidate_id is not None
            and (not isinstance(busy_candidate_id, str) or not busy_candidate_id)
        )
        or not isinstance(candidates, list)
    ):
        raise ValueError("deployment_activation_status_invalid")
    seen: set[str] = set()
    activating: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or set(candidate) != _CANDIDATE_KEYS:
            raise ValueError("deployment_activation_candidate_invalid")
        candidate_id = candidate.get("candidate_id")
        state = candidate.get("state")
        phase = candidate.get("phase")
        reason = candidate.get("reason_code")
        completed = candidate.get("completed_steps")
        total = candidate.get("total_steps")
        strings = (
            "candidate_id",
            "deployment_id",
            "model_id",
            "model_revision",
            "quantization",
            "plan_digest",
        )
        if (
            not all(
                isinstance(candidate.get(key), str) and candidate.get(key)
                for key in strings
            )
            or candidate_id in seen
            or candidate_id != candidate.get("deployment_id")
            or _REVISION.fullmatch(candidate["model_revision"]) is None
            or _SHA256.fullmatch(candidate["plan_digest"]) is None
            or state not in _STATES
            or not isinstance(candidate.get("topology_size"), int)
            or isinstance(candidate.get("topology_size"), bool)
            or candidate["topology_size"] < 1
            or not isinstance(completed, int)
            or isinstance(completed, bool)
            or total != 4
            or not 0 <= completed <= total
        ):
            raise ValueError("deployment_activation_candidate_invalid")
        if state == "activating":
            if (
                phase not in _PHASES
                or reason is not None
                or completed != _PHASES[phase]
            ):
                raise ValueError("deployment_activation_candidate_invalid")
            activating.append(candidate_id)
        elif state in {"qualified", "active"}:
            if phase is not None or reason is not None or completed != total:
                raise ValueError("deployment_activation_candidate_invalid")
        elif state in {"failed", "unavailable"}:
            if phase is not None or not isinstance(reason, str) or not reason:
                raise ValueError("deployment_activation_candidate_invalid")
        elif phase is not None or reason is not None or completed != 0:
            raise ValueError("deployment_activation_candidate_invalid")
        seen.add(candidate_id)
    if (busy_candidate_id is None and activating) or (
        busy_candidate_id is not None and activating != [busy_candidate_id]
    ):
        raise ValueError("deployment_activation_busy_state_invalid")


def _private_directory(path: Path, code: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise DeploymentActivationError(code)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise DeploymentActivationError(code) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise DeploymentActivationError(code)
    return candidate


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _prepared(path: Path) -> PreparedDeployment:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or not 0 < metadata.st_size <= MAX_PLAN_BYTES
        ):
            raise DeploymentActivationError("candidate_plan_unsafe")
        plan_bytes = path.read_bytes()
        config = load_operator_plan(path)
        run_plan = config.controller.get("run_plan")
        nodes = run_plan.get("nodes") if isinstance(run_plan, Mapping) else None
        if not isinstance(nodes, list) or not nodes:
            raise DeploymentActivationError("candidate_graph_invalid")
        graph_documents: list[dict[str, Any]] = []
        for node in nodes:
            configure = node.get("configure") if isinstance(node, Mapping) else None
            graph = configure.get("graph") if isinstance(configure, Mapping) else None
            if not isinstance(graph, dict):
                raise DeploymentActivationError("candidate_graph_invalid")
            graph_documents.append(graph)
        reference = _canonical(graph_documents[0])
        if any(_canonical(graph) != reference for graph in graph_documents[1:]):
            raise DeploymentActivationError("candidate_graph_disagreement")
        graph = execution_graph_from_document(graph_documents[0])
    except DeploymentActivationError:
        raise
    except (OSError, RunnerError, TypeError, ValueError, RecursionError) as exc:
        code = getattr(exc, "code", "candidate_plan_invalid")
        public = (
            code
            if isinstance(code, str) and _SAFE_CODE.fullmatch(code)
            else "candidate_plan_invalid"
        )
        raise DeploymentActivationError(public) from exc
    digest = "sha256:" + hashlib.sha256(plan_bytes).hexdigest()
    return PreparedDeployment(
        candidate_id=graph.deployment_id,
        deployment_id=graph.deployment_id,
        model_id=graph.model_id,
        model_revision=graph.resolved_commit,
        quantization="int8-weight-only",
        topology_size=len(graph.stages),
        plan_digest=digest,
        plan_path=path,
        plan_bytes=plan_bytes,
    )


class PreparedDeploymentActivation:
    """Discover prepared plans and qualify one candidate in a background worker."""

    def __init__(
        self,
        *,
        candidate_root: Path,
        state_root: Path,
        registry: LiveDeploymentRegistry,
        runtime_loader: RuntimeLoader,
    ) -> None:
        self._candidate_root = _private_directory(
            Path(candidate_root), "candidate_plan_root_unsafe"
        )
        self._state_root = _private_directory(
            Path(state_root), "activation_state_root_unsafe"
        )
        if not isinstance(registry, LiveDeploymentRegistry):
            raise ValueError("deployment_activation_registry_invalid")
        if not callable(runtime_loader):
            raise ValueError("deployment_activation_loader_invalid")
        self._registry = registry
        self._runtime_loader = runtime_loader
        self._generation = 0
        self._candidates: dict[str, PreparedDeployment] = {}
        self._invalid_candidate_count = 0
        self._attempts: dict[str, _Attempt] = {}
        self._busy_candidate_id: str | None = None
        self._busy_candidate: PreparedDeployment | None = None
        self._worker: threading.Thread | None = None
        self._stopping = False
        self._lock = threading.RLock()
        self.refresh()

    def _discover(self) -> tuple[dict[str, PreparedDeployment], int]:
        valid: dict[str, PreparedDeployment] = {}
        conflicts: set[str] = set()
        invalid = 0
        try:
            paths = sorted(self._candidate_root.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            raise DeploymentActivationError("candidate_plan_root_unavailable") from exc
        for path in paths[: MAX_CANDIDATES + 1]:
            if path.suffix != ".json":
                continue
            if len(valid) + invalid >= MAX_CANDIDATES:
                invalid += 1
                continue
            try:
                candidate = _prepared(path)
            except DeploymentActivationError:
                invalid += 1
                continue
            previous = valid.get(candidate.candidate_id)
            if previous is not None and previous.plan_digest != candidate.plan_digest:
                conflicts.add(candidate.candidate_id)
                invalid += 2
                continue
            valid[candidate.candidate_id] = candidate
        for candidate_id in conflicts:
            valid.pop(candidate_id, None)
        return valid, invalid

    def refresh(self) -> Mapping[str, Any]:
        candidates, invalid = self._discover()
        with self._lock:
            if {key: value.plan_digest for key, value in self._candidates.items()} != {
                key: value.plan_digest for key, value in candidates.items()
            } or invalid != self._invalid_candidate_count:
                self._generation += 1
            self._candidates = candidates
            self._invalid_candidate_count = invalid
            return self._status_locked()

    def _registered(self) -> dict[str, Mapping[str, Any]]:
        status = self._registry.registry_status()
        return {
            item["deployment_id"]: item
            for item in status["deployments"]
            if isinstance(item, Mapping)
        }

    def _status_locked(self) -> Mapping[str, Any]:
        registered = self._registered()
        selected = self._registry.registry_status()["selected_deployment_id"]
        candidates = dict(self._candidates)
        if self._busy_candidate is not None:
            candidates.setdefault(
                self._busy_candidate.candidate_id,
                self._busy_candidate,
            )
        projected = []
        for candidate_id, candidate in sorted(candidates.items()):
            deployment = registered.get(candidate.deployment_id)
            attempt = self._attempts.get(candidate_id)
            if attempt is not None and attempt.state == "activating":
                state = attempt.state
                phase = attempt.phase
                reason = attempt.reason_code
            elif deployment is not None:
                state = (
                    "unavailable"
                    if deployment.get("health") != "qualified"
                    else "active"
                    if candidate.deployment_id == selected
                    else "qualified"
                )
                phase = None
                reason = None if state != "unavailable" else "route_unavailable"
            elif attempt is not None and attempt.plan_digest == candidate.plan_digest:
                state = attempt.state
                phase = attempt.phase
                reason = attempt.reason_code
            else:
                state = "prepared"
                phase = None
                reason = None
            projected.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "deployment_id": candidate.deployment_id,
                    "model_id": candidate.model_id,
                    "model_revision": candidate.model_revision,
                    "quantization": candidate.quantization,
                    "topology_size": candidate.topology_size,
                    "plan_digest": candidate.plan_digest,
                    "state": state,
                    "phase": phase,
                    "completed_steps": 4
                    if state in {"qualified", "active"}
                    else _PHASES.get(phase, 0),
                    "total_steps": 4,
                    "reason_code": reason,
                }
            )
        document = {
            "protocol": ACTIVATION_PROTOCOL,
            "generation": self._generation,
            "busy_candidate_id": self._busy_candidate_id,
            "invalid_candidate_count": self._invalid_candidate_count,
            "candidates": projected,
        }
        validate_activation_status(document)
        return document

    def status(self) -> Mapping[str, Any]:
        return self.refresh()

    def _snapshot(self, candidate: PreparedDeployment) -> Path:
        snapshots = self._state_root / "candidate-plans"
        snapshots.mkdir(mode=0o700, exist_ok=True)
        if snapshots.is_symlink() or snapshots.stat().st_uid != os.geteuid():
            raise DeploymentActivationError("activation_state_root_unsafe")
        destination = snapshots / f"{candidate.plan_digest[7:]}.json"
        if destination.exists():
            if (
                destination.is_symlink()
                or destination.read_bytes() != candidate.plan_bytes
            ):
                raise DeploymentActivationError("candidate_snapshot_conflict")
            return destination
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                output.write(candidate.plan_bytes)
                output.flush()
                os.fsync(output.fileno())
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        return destination

    def activate(self, candidate_id: str) -> Mapping[str, Any]:
        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > 255
        ):
            raise DeploymentActivationError("candidate_id_invalid")
        self.refresh()
        with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                raise DeploymentActivationError("candidate_unknown")
            deployment = self._registered().get(candidate.deployment_id)
            if deployment is not None and deployment.get("health") == "qualified":
                return self._status_locked()
            if self._busy_candidate_id is not None:
                if self._busy_candidate_id == candidate_id:
                    return self._status_locked()
                raise DeploymentActivationError("activation_busy")
            if self._stopping:
                raise DeploymentActivationError("activation_stopping")
            snapshot = self._snapshot(candidate)
            self._busy_candidate_id = candidate_id
            self._busy_candidate = candidate
            self._attempts[candidate_id] = _Attempt(
                candidate.plan_digest,
                "activating",
                "validating_plan",
                None,
            )
            self._generation += 1
            worker = threading.Thread(
                target=self._activate_worker,
                args=(candidate, snapshot),
                name=f"mycelium-activate-{candidate_id[:24]}",
                daemon=True,
            )
            self._worker = worker
            worker.start()
            return self._status_locked()

    def unload(self, candidate_id: str) -> Mapping[str, Any]:
        """Unload one candidate-backed standby while retaining its immutable plan."""

        if (
            not isinstance(candidate_id, str)
            or not candidate_id
            or len(candidate_id) > 255
        ):
            raise DeploymentActivationError("candidate_id_invalid")
        self.refresh()
        with self._lock:
            candidate = self._candidates.get(candidate_id)
            if candidate is None:
                raise DeploymentActivationError("candidate_unknown")
            if self._busy_candidate_id is not None:
                raise DeploymentActivationError("activation_busy")
        try:
            self._registry.unload_qualified_runtime(candidate.deployment_id)
        except Exception as exc:
            code = getattr(exc, "code", str(exc))
            if code not in {
                "deployment_unknown",
                "deployment_unload_selected",
                "deployment_unload_busy",
                "deployment_unload_failed",
            }:
                code = "deployment_unload_failed"
            raise DeploymentActivationError(code) from exc
        with self._lock:
            self._attempts.pop(candidate_id, None)
            self._generation += 1
            return self._status_locked()

    def _progress(self, candidate: PreparedDeployment, phase: str) -> None:
        if phase not in _PHASES:
            raise DeploymentActivationError("activation_phase_invalid")
        with self._lock:
            self._attempts[candidate.candidate_id] = _Attempt(
                candidate.plan_digest,
                "activating",
                phase,
                None,
            )
            self._generation += 1

    @staticmethod
    def _public_reason(exc: BaseException) -> str:
        code = getattr(exc, "code", None)
        if not isinstance(code, str):
            code = str(exc).partition(":")[0]
        return code if _SAFE_CODE.fullmatch(code) else "activation_failed"

    def _activate_worker(self, candidate: PreparedDeployment, snapshot: Path) -> None:
        runtime: QualifiedDeploymentRuntime | None = None
        registered = False
        try:
            runtime = self._runtime_loader(
                snapshot,
                lambda phase: self._progress(candidate, phase),
            )
            self._progress(candidate, "registering")
            with self._lock:
                stopping = self._stopping
            if stopping:
                raise DeploymentActivationError("activation_stopping")
            self._registry.add_qualified_runtime(runtime)
            registered = True
            with self._lock:
                self._attempts[candidate.candidate_id] = _Attempt(
                    candidate.plan_digest,
                    "qualified",
                    None,
                    None,
                )
                self._generation += 1
        except BaseException as exc:
            with self._lock:
                self._attempts[candidate.candidate_id] = _Attempt(
                    candidate.plan_digest,
                    "failed",
                    None,
                    self._public_reason(exc),
                )
                self._generation += 1
        finally:
            if runtime is not None and not registered:
                try:
                    runtime.route.close()
                except BaseException:
                    pass
            with self._lock:
                self._busy_candidate_id = None
                self._busy_candidate = None
                self._worker = None

    def close(self) -> None:
        with self._lock:
            self._stopping = True
            worker = self._worker
        if worker is not None:
            worker.join(timeout=5.0)


__all__ = [
    "ACTIVATION_PROTOCOL",
    "DeploymentActivationError",
    "PreparedDeploymentActivation",
    "validate_activation_status",
]
