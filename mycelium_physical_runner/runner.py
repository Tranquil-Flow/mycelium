"""Fail-closed orchestration for one physical-runner command."""
from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from threading import Lock
from typing import Any

from .adapters import Publisher
from .config import RunnerConfig
from .errors import RunnerError
from .lock import ExclusiveLock
from .state import RunStateDocument, RunnerState
from .state_store import StateStore

_CONTROLLER_PROTOCOL = "mycelium.physical_controller_result.v1"
_SEALED_FIELDS = frozenset({"run_id", "manifest_path", "manifest_digest"})
_PUBLICATION_RESULT_FIELDS = (
    "protocol",
    "qualification_id",
    "evidence_manifest_digest",
    "evidence_class",
    "route_ready",
    "reason_codes",
    "qualified_by",
)
_COMMANDS = {"prepare", "diagnose", "qualify", "cancel", "recover", "cleanup"}


@dataclass(slots=True)
class _ExecutionContext:
    published_qualification_id: str | None = None


def _as_runner_error(error: BaseException, code: str) -> RunnerError:
    if isinstance(error, RunnerError):
        return error
    normalized = RunnerError(code)
    normalized.__cause__ = error
    return normalized


class PhysicalRunner:
    def __init__(
        self,
        *,
        config: RunnerConfig,
        controller: Any | None = None,
        publisher: Publisher | None = None,
        clock_unix_ms: Callable[[], int] | None = None,
    ) -> None:
        self._config = config
        self._controller = controller
        self._publisher = publisher
        self._clock = clock_unix_ms or (lambda: int(time.time() * 1000))
        self._lock = ExclusiveLock(config.lock_path)
        self._state_store = StateStore(state_path=config.state_path)
        self._state = RunnerState.UNREADY
        self._on_state_change: Callable[[RunnerState], None] | None = None
        self._execution_guard = Lock()

    @property
    def config(self) -> RunnerConfig:
        return self._config

    @property
    def state(self) -> RunnerState:
        return self._state

    @property
    def lock_held(self) -> bool:
        return self._lock.held

    @property
    def on_state_change(self) -> Callable[[RunnerState], None] | None:
        return self._on_state_change

    @on_state_change.setter
    def on_state_change(self, callback: Callable[[RunnerState], None] | None) -> None:
        self._on_state_change = callback

    def acquire_lock(self) -> None:
        self._lock.acquire()

    def release_lock(self) -> None:
        if self._execution_guard.locked():
            raise RunnerError("runner_lock_held")
        self._lock.release()

    def execute(self, command: str) -> dict[str, Any]:
        if command not in _COMMANDS:
            raise RunnerError("runner_command_invalid")
        if not self._execution_guard.acquire(blocking=False):
            raise RunnerError("runner_lock_held")
        try:
            self.acquire_lock()
            context = _ExecutionContext()
            operation_error: BaseException | None = None
            release_error: RunnerError | None = None
            outcome: dict[str, Any] | None = None
            try:
                outcome = self._execute_locked(command, context=context)
            except BaseException as exc:
                operation_error = exc
            finally:
                try:
                    self._lock.release()
                except Exception as release_exc:
                    release_error = _as_runner_error(
                        release_exc,
                        "runner_lock_release_failed",
                    )
            if operation_error is not None:
                if release_error is not None:
                    operation_error.add_note(release_error.code)
                raise operation_error
            if release_error is not None:
                raise self._fail_closed_after_publication(
                    command=command,
                    context=context,
                    primary_error=release_error,
                )
            if outcome is None:
                raise RunnerError("runner_unexpected")
            if command == "qualify":
                published = outcome["published_qualification"]
                sealed = outcome["sealed_manifest"]
                try:
                    self._persist(
                        command=command,
                        route_ready=published["route_ready"],
                        manifest_digest=str(sealed["manifest_digest"]),
                        qualification_id=str(published["qualification_id"]),
                    )
                except BaseException as exc:
                    primary_error = _as_runner_error(exc, "state_write_failed")
                    raise self._fail_closed_after_publication(
                        command=command,
                        context=context,
                        primary_error=primary_error,
                    )
            return outcome
        finally:
            self._execution_guard.release()

    def cleanup(self) -> dict[str, Any]:
        return self.execute("cleanup")

    def _execute_locked(
        self,
        command: str,
        *,
        context: _ExecutionContext,
    ) -> dict[str, Any]:
        cleaned = command == "cleanup"
        self._transition(RunnerState.PREPARING)
        try:
            if command == "diagnose":
                self._transition(RunnerState.LOADING)
                result = self._controller_command("run")
                self._validate_result(result, "run")
                self._transition(RunnerState.UNREADY)
                cleaned = True
                self._cleanup_required()
                self._transition(RunnerState.CLEANUP_COMPLETE)
                outcome = {
                    "command": command,
                    "accepted": False,
                    "route_ready": False,
                    "release_ready": False,
                    "controller_result": result,
                }
                self._persist(command=command, route_ready=False)
                return outcome

            if command == "qualify":
                self._transition(RunnerState.LOADING)
                result = self._controller_command("seal")
                sealed = self._validate_seal_result(result)
                self._transition(RunnerState.UNREADY)
                cleaned = True
                self._cleanup_required()
                # Keep the durable projection fail-closed until lock release and
                # qualification-state publication have both completed.
                self._persist(command=command, route_ready=False)
                publisher = self._publisher
                if publisher is None:
                    raise RunnerError("authority_publisher_unavailable")
                revoke = getattr(publisher, "revoke", None)
                if not callable(revoke):
                    raise RunnerError("authority_publisher_invalid")
                try:
                    published = dict(publisher(sealed))
                except RunnerError:
                    raise
                except Exception as exc:
                    raise RunnerError("authority_publish_failed") from exc
                published_route_ready = published.get("route_ready")
                raw_qualification_id = published.get("qualification_id")
                published_qualification_id = (
                    raw_qualification_id
                    if isinstance(raw_qualification_id, str)
                    else None
                )
                context.published_qualification_id = published_qualification_id
                if (
                    published.get("protocol") != "mycelium.route_qualification.v1"
                    or published_route_ready is not True
                    or published.get("evidence_manifest_digest")
                    != sealed.get("manifest_digest")
                    or published.get("evidence_class") != "physical_qualification"
                    or published.get("qualified_by")
                    != "mycelium_qualification.qualifier:RouteQualificationV1"
                    or not isinstance(published.get("reason_codes"), list)
                    or published.get("reason_codes")
                    or not isinstance(published_qualification_id, str)
                    or not published_qualification_id
                ):
                    raise RunnerError("authority_publication_invalid")
                published_projection = {
                    field: published[field]
                    for field in _PUBLICATION_RESULT_FIELDS
                }
                self._transition(RunnerState.QUALIFIED)
                return {
                    "command": command,
                    "accepted": True,
                    "route_ready": published_route_ready,
                    "release_ready": False,
                    "sealed_manifest": sealed,
                    "published_qualification": published_projection,
                }

            result = self._controller_command(command)
            self._validate_result(result, command)
            if command != "cleanup":
                cleaned = True
                self._cleanup_required()
            else:
                cleaned = True
            self._transition(RunnerState.CLEANUP_COMPLETE)
            self._persist(command=command, route_ready=False)
            return {
                "command": command,
                "accepted": False,
                "route_ready": False,
                "release_ready": False,
                "controller_result": result,
            }
        except BaseException as exc:
            primary_error = _as_runner_error(exc, "runner_unexpected")
            revocation_error = self._revoke_published(context)
            cleanup_error: RunnerError | None = None
            if not cleaned:
                try:
                    self._cleanup_required()
                except RunnerError as cleanup_exc:
                    cleanup_error = cleanup_exc
            persistence_error = self._persist_failed(command)
            if revocation_error is not None:
                primary_error.add_note(revocation_error.code)
            if cleanup_error is not None:
                primary_error.add_note(cleanup_error.code)
            if persistence_error is not None:
                primary_error.add_note(persistence_error.code)
            raise primary_error

    def _fail_closed_after_publication(
        self,
        *,
        command: str,
        context: _ExecutionContext,
        primary_error: RunnerError,
    ) -> RunnerError:
        revocation_error = self._revoke_published(context)
        persistence_error = self._persist_failed(command)
        if revocation_error is not None:
            primary_error.add_note(revocation_error.code)
        if persistence_error is not None:
            primary_error.add_note(persistence_error.code)
        return primary_error

    def _revoke_published(
        self,
        context: _ExecutionContext,
    ) -> RunnerError | None:
        qualification_id = context.published_qualification_id
        if qualification_id is None:
            return None
        publisher = self._publisher
        revoke = getattr(publisher, "revoke", None)
        try:
            if not callable(revoke) or revoke(qualification_id) is not True:
                return RunnerError("authority_revoke_failed")
        except Exception as exc:
            error = RunnerError("authority_revoke_failed")
            error.__cause__ = exc
            return error
        context.published_qualification_id = None
        return None

    def _persist_failed(self, command: str) -> RunnerError | None:
        self._state = RunnerState.FAILED
        callback = self._on_state_change
        if callback is not None:
            try:
                callback(RunnerState.FAILED)
            except Exception:
                # Failure notification is advisory. It cannot block the
                # authority revocation or fail-closed state write.
                pass
        try:
            self._persist(command=command, route_ready=False)
        except Exception as exc:
            return _as_runner_error(exc, "state_write_failed")
        return None

    def _controller_command(self, command: str) -> Mapping[str, Any]:
        if self._controller is None or not callable(getattr(self._controller, "execute", None)):
            raise RunnerError("controller_unavailable")
        try:
            value = self._controller.execute(command)
        except RunnerError:
            raise
        except Exception as exc:
            code = "controller_cleanup_failed" if command == "cleanup" else f"controller_{command}_failed"
            raise RunnerError(code) from exc
        if not isinstance(value, Mapping):
            raise RunnerError("controller_contract_invalid")
        return value

    def _cleanup_required(self) -> Mapping[str, Any]:
        result = self._controller_command("cleanup")
        self._validate_result(result, "cleanup")
        return result

    def _validate_result(self, result: Mapping[str, Any], command: str) -> None:
        if result.get("protocol") != _CONTROLLER_PROTOCOL or result.get("command") != command:
            raise RunnerError("controller_contract_invalid", command)
        if result.get("release_ready") is not False:
            raise RunnerError("controller_contract_invalid", "release_ready")
        if command != "seal" and result.get("route_ready") is not False:
            raise RunnerError("controller_contract_invalid", "route_ready")
        if command == "cleanup":
            actions = result.get("actions")
            peers = self._config.controller.get("peers")
            if not isinstance(actions, list) or not isinstance(peers, list) or len(actions) != len(peers):
                raise RunnerError("controller_cleanup_invalid")
            expected = {
                str(peer.get("node_id")): str(peer.get("staging_root"))
                for peer in peers
                if isinstance(peer, Mapping)
            }
            observed: dict[str, str] = {}
            required_fields = {"protocol", "node_id", "staging_root", "removed"}
            for action in actions:
                if not isinstance(action, Mapping) or set(action) != required_fields:
                    raise RunnerError("controller_cleanup_invalid")
                node_id = action.get("node_id")
                staging_root = action.get("staging_root")
                if (
                    action.get("protocol") != "mycelium.controller_remote_cleanup_ack.v1"
                    or not isinstance(node_id, str)
                    or not isinstance(staging_root, str)
                    or not isinstance(action.get("removed"), bool)
                    or node_id in observed
                ):
                    raise RunnerError("controller_cleanup_invalid")
                observed[node_id] = staging_root
            if observed != expected:
                raise RunnerError("controller_cleanup_invalid")

    def _validate_seal_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        self._validate_result(result, "seal")
        qualifier_invocations = result.get("qualifier_invocations")
        if (
            type(qualifier_invocations) is not int
            or qualifier_invocations != 0
            or result.get("route_ready") is not False
            or "qualification" in result
        ):
            raise RunnerError("controller_seal_invalid")
        sealed_raw = result.get("sealed_manifest")
        if not isinstance(sealed_raw, Mapping) or set(sealed_raw) != _SEALED_FIELDS:
            raise RunnerError("controller_seal_invalid", "sealed_manifest")
        sealed = dict(sealed_raw)
        if (
            sealed.get("run_id") != self._config.run_id
            or not isinstance(sealed.get("manifest_path"), str)
            or not isinstance(sealed.get("manifest_digest"), str)
        ):
            raise RunnerError("controller_seal_invalid", "sealed_manifest")
        return sealed

    def _transition(self, state: RunnerState) -> None:
        callback = self._on_state_change
        if callback is not None:
            try:
                callback(state)
            except Exception as exc:
                raise RunnerError("state_callback_failed") from exc
        self._state = state

    def _persist(
        self,
        *,
        command: str,
        route_ready: bool,
        manifest_digest: str | None = None,
        qualification_id: str | None = None,
    ) -> None:
        plan_path = self._config.operator_plan_path or "<in-memory-plan>"
        self._state_store.write(
            RunStateDocument(
                plan_id=self._config.plan_id,
                run_id=self._config.run_id,
                operator_plan_path=plan_path,
                command=command,
                state=self._state,
                updated_at_unix_ms=self._clock(),
                route_ready=route_ready,
                manifest_digest=manifest_digest,
                qualification_id=qualification_id,
            )
        )


__all__ = ["PhysicalRunner"]
