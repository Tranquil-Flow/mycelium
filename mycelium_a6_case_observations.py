# SPDX-License-Identifier: AGPL-3.0-only
"""Produce the source-owned A6 case observations the physical gate reads.

``scripts/run_a6_recovery_case.py`` validates one closed
``mycelium.a6_source_owned_case_observation.v1`` envelope per case, and one
``mycelium.a6_source_owned_cleanup_observation.v1`` document, below
``<evidence_output_dir>/a6-production-observations/``. Until now nothing wrote
them. This module is the writer, owned by the product serve process:

* a case observation exists only for a case the product-mounted receiver
  actually executed on a real admitted request (``ExecutedCase``);
* every field is derived from product authorities read after that request
  settled: the router's recovery view, the gateway's delivery ledger and
  terminal notice, the M16 coordinator's reservations, and the qualifier's
  issued facts. No field is copied from a case contract or a caller;
* each envelope is validated by the reader's own validator before it is
  written, so a case whose measured facts do not satisfy its contract is
  refused here and simply has no file: the gate then fails that case;
* files are written atomically once, 0600 inside a 0700 directory, and an
  existing observation is never replaced.

The binding digests are source-owned too: the A6 source manifest's source
digest, the A6 specification bytes and the contract manifest bytes of the
serving tree, plus the exact operator-plan bytes. The operator copies the
published values into the owner-private execution plan; any drift fails the
gate's binding check.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from scripts.run_a6_recovery_case import (
    AUTHORITY,
    OBSERVATION_PROTOCOL,
    REQUIRED_CASES,
    _canonical,
    _sha256,
    validate_source_owned_observation,
)

_LOGGER = logging.getLogger(__name__)

OBSERVATIONS_DIRNAME = "a6-production-observations"
CLEANUP_PROTOCOL = "mycelium.a6_source_owned_cleanup_observation.v1"
CLEANUP_FILENAME = "cleanup.json"
POSITIVE_CASE = "ordinary_browser_source_failure_recovery"
SPEC_PATH = "docs/superpowers/specs/2026-08-18-mycelium-a6-full-context-replay-recovery.md"
SOURCE_MANIFEST_PATH = "artifacts/a6/source-manifest-v1.json"
CONTRACT_MANIFEST_PATH = "contracts/contract-manifest.v1.json"
_PRIVATE_MARKERS = (
    "encoded_prompt_token_ids", "committed_prefix_tokens", "checkpoint_path",
    "node_secret", "ssh_identity", "private_key",
)
_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})


class ObservationRefused(ValueError):
    """Nothing is written when an observation is refused."""

    def __init__(self, code: str, *, detail: Mapping[str, Any] | None = None) -> None:
        super().__init__(code)
        #: Privacy-reduced field -> value facts that explain a refusal.
        self.detail = None if detail is None else dict(detail)


#: Scalar observation and binding fields safe to echo in an operator log.
_DETAIL_SCALARS = (bool, int, str, type(None))


def refusal_detail(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Observation and binding scalars of a refused envelope, digests excluded.

    Every value is a measured scalar the envelope would have published; no
    token material, path, or secret can be present (private markers were
    already refused), and long strings are truncated.
    """

    detail: dict[str, Any] = {}
    for section in ("observation", "bindings"):
        values = envelope.get(section)
        if not isinstance(values, Mapping):
            continue
        for key, value in sorted(values.items()):
            if key.endswith("_digest") or not isinstance(value, _DETAIL_SCALARS):
                continue
            if any(marker in key for marker in _PRIVATE_MARKERS):
                continue
            detail[f"{section}.{key}"] = value[:64] if isinstance(value, str) else value
    return detail


@dataclass(frozen=True)
class ObservationAuthority:
    """Run identity and source-owned digests every envelope is bound to."""

    run_id: str
    evidence_output_dir: Path
    operator_plan_digest: str
    source_digest: str
    spec_digest: str
    contract_digest: str
    #: The exact A6 source-manifest bytes (base commit plus file set) served.
    candidate_digest: str = ""

    def status(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "operator_plan_digest": self.operator_plan_digest,
            "candidate_digest": self.candidate_digest,
            "source_digest": self.source_digest,
            "spec_digest": self.spec_digest,
            "contract_digest": self.contract_digest,
        }


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()


def observation_authority(*, operator_plan: Path, repo_root: Path) -> ObservationAuthority:
    """Bind the physical-runner operator plan and the serving tree's digests."""

    from mycelium_physical_runner import load_operator_plan

    try:
        config = load_operator_plan(operator_plan)
        manifest = json.loads((repo_root / SOURCE_MANIFEST_PATH).read_text("utf-8"))
        source_digest = manifest["source_digest"]
        spec_digest = _file_digest(repo_root / SPEC_PATH)
        contract_digest = _file_digest(repo_root / CONTRACT_MANIFEST_PATH)
    except Exception as exc:
        raise ObservationRefused("a6_observation_authority_unavailable") from exc
    if not isinstance(source_digest, str) or not source_digest.startswith("sha256:"):
        raise ObservationRefused("a6_observation_authority_unavailable")
    return ObservationAuthority(
        run_id=config.run_id,
        evidence_output_dir=Path(config.evidence_output_dir),
        operator_plan_digest=_file_digest(Path(operator_plan)),
        source_digest=source_digest,
        spec_digest=spec_digest,
        contract_digest=contract_digest,
        candidate_digest=_file_digest(repo_root / SOURCE_MANIFEST_PATH),
    )


def _private_dir(path: Path) -> None:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        os.chmod(path, 0o700)
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700
            or info.st_uid != os.getuid()):
        raise ObservationRefused("a6_observation_root_invalid")


def _write_once(path: Path, document: Mapping[str, Any]) -> None:
    _private_dir(path.parent)
    if path.exists() or path.is_symlink():
        raise ObservationRefused("a6_observation_exists")
    payload = json.dumps(document, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    temporary = path.parent / f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.link(temporary, path)  # never replaces an existing observation
    os.unlink(temporary)


# -- product facts --------------------------------------------------------------


def gateway_facts(service: Any, request_id: str, *, failure_sequence: int | None,
                  committed_at_failure: int | None) -> dict[str, Any] | None:
    """Privacy-reduced delivery-ledger facts for one request; no token content."""

    try:
        session = service._get_session(request_id)
    except Exception:
        return None
    with session.condition:
        events = list(session.events)
        terminal = session.terminal_event
        indices = [event.token_index for event in events if event.kind == "token"]
        late = 0
        if failure_sequence is not None and committed_at_failure is not None:
            late = sum(1 for event in events if event.kind == "token"
                       and event.sequence > failure_sequence
                       and event.token_index is not None
                       and event.token_index < committed_at_failure)
        return {
            "terminal_count": session.terminal_count,
            "terminal_kind": None if terminal is None else terminal.kind,
            "terminal_code": None if terminal is None else terminal.code,
            "committed_count": (len(session.token_digests)
                                if session.released_committed_count is None
                                else session.released_committed_count),
            "expected_token_index": session.expected_token_index,
            "duplicated_token_count": len(indices) - len(set(indices)),
            "late_source_publication_count": late,
            "subscribed": bool(session.ever_subscribed),
            "cleanup_notice_provenance": session.terminal_provenance,
            # The gateway's own receipt of the attempt its terminal belongs to.
            "terminal_attempt": (None if not isinstance(session.terminal_attempt, tuple)
                                 else session.terminal_attempt[1]),
        }


def gateway_failure_marker(service: Any, request_id: str) -> tuple[int, int] | None:
    """(latest event sequence, committed count) at the instant of a fault."""

    try:
        session = service._get_session(request_id)
    except Exception:
        return None
    with session.condition:
        return session.latest_sequence, (
            len(session.token_digests) if session.released_committed_count is None
            else session.released_committed_count)


@dataclass
class SettledFacts:
    """Everything read from product authorities once one case settled."""

    view: dict[str, Any]
    gateway: dict[str, Any] | None
    reservation_count: int | None
    coordinator_terminal: str | None
    qualification: dict[str, Any] | None
    deadline_unix_ms: int | None


def _terminal_state(case_id: str, settled: SettledFacts) -> str | None:
    view, gateway = settled.view, settled.gateway or {}
    if view.get("terminal_blocked_reason") is not None:
        return "TERMINAL_BLOCKED"
    kind = gateway.get("terminal_kind")
    if kind == "completed":
        return "completed"
    if kind == "cancelled":
        return "cancelled"
    if kind == "failed":
        if gateway.get("terminal_code") == "request_cleanup_unproven":
            return "TERMINAL_BLOCKED"
        reason = view.get("recovery_failure_reason")
        if reason == "recovery_timeout" or view.get("terminal_error_code") == "deadline_exceeded":
            return "timed_out"
        if case_id == "shutdown_during_recovery":
            return "shutdown"
        return "aborted"
    return None


def _cleanup_complete(settled: SettledFacts) -> bool:
    return settled.reservation_count == 0 and settled.coordinator_terminal is not None


def _rejection_observed(case_id: str, post: Mapping[str, Any]) -> bool:
    keys = {
        "incompatible_successor": "recovery_refused",
        "stale_qualification": "recovery_refused",
        "foreign_qualification": "recovery_refused",
        "unexecuted_qualification": "recovery_refused",
        "mismatched_path_attempt": "recovery_refused",
        "no_successor": "truthful_abort_required",
        "successor_prefill_failure": "successor_failure_injected",
        "cancellation_before_recovery": "cancellation_observed_before_recovery",
        "cancellation_during_prefill": "cancellation_observed_during_prefill",
        "timeout_during_recovery": "recovery_deadline_exceeded",
        "exhausted_recovery_attempts": "attempts_exhausted",
        "stale_source_output_after_fencing": "late_publication_rejected",
        "cleanup_authority_failure": "cleanup_authority_refused",
        "shutdown_during_recovery": "shutdown_observed",
    }
    return post.get(keys[case_id]) is True


def _cutover_identity(executed: Any, view: Mapping[str, Any]) -> dict[str, Any] | None:
    """The committed route identity, only when a successor cutover committed.

    A reserved successor command advances the command attempt before prefill;
    only the cutover compare-and-swap replaces the request's route identity.
    """

    identity = view.get("route_identity")
    if not isinstance(identity, Mapping):
        return None
    attempt = identity.get("request_attempt")
    if type(attempt) is not int or attempt <= executed.binding["source_attempt"]:
        return None
    return dict(identity)


def derive_observation(case_id: str, executed: Any, settled: SettledFacts) -> dict[str, Any]:
    """Derive the closed case observation from settled product facts only."""

    post = dict(executed.post_state)
    view, gateway = settled.view, settled.gateway or {}
    source_attempt = executed.binding["source_attempt"]
    committed = _cutover_identity(executed, view)
    final_attempt = None if committed is None else committed["request_attempt"]
    # A cutover counts only when the gateway's own terminal receipt names the
    # same attempt the router committed; a router-only fact is not enough.
    cut_over = committed is not None and gateway.get("terminal_attempt") == final_attempt
    common = {
        "case_id": case_id,
        "executed": True,
        "ordinary_product_path": settled.gateway is not None,
        "private_material_exposed": False,
        "process_cleanup_complete": _cleanup_complete(settled),
        "stale_reservation_count": settled.reservation_count,
        "terminal_event_count": gateway.get("terminal_count"),
    }
    if case_id == POSITIVE_CASE:
        facts = settled.qualification or {}
        return {
            **common,
            "browser_originated": gateway.get("subscribed") is True,
            "source_failure_observed": post.get("source_failure_observed") is True,
            "checkpoint_frozen": (view.get("recovery_attempts") or 0) >= 1,
            "successor_qualified": (
                cut_over and facts.get("controlled_fault") is None
                and facts.get("physical_challenge") == "passed"
                and facts.get("compatibility_matches_source") is True),
            "source_fenced": cut_over,
            "successor_prefill": cut_over,
            "publication_generation_cas_count": (
                final_attempt - source_attempt if cut_over else None),
            "duplicated_token_count": gateway.get("duplicated_token_count"),
            "omitted_committed_token_count": (
                None if not gateway else
                gateway["expected_token_index"] - gateway["committed_count"]),
            "late_source_publication_count": gateway.get("late_source_publication_count"),
            "logical_stream_count": 1 if settled.gateway is not None else 0,
            "terminal_state": _terminal_state(case_id, settled),
        }
    if case_id == "cleanup_reservation_release":
        return {
            **{key: value for key, value in common.items()},
            "release_observed": post.get("release_observed") is True,
            "release_count": executed.armed_reservation_count,
        }
    if case_id == "stale_source_output_after_fencing":
        # Owner ruling: the request may complete on its successor; fail-closed
        # is about the fenced source's late publication, which the authority
        # must refuse without advancing the gateway stream.
        fail_closed = (
            post.get("late_publication_attempted") is True
            and post.get("late_publication_rejected") is True
            and settled.gateway is not None
            and gateway.get("late_source_publication_count") == 0
            and gateway.get("duplicated_token_count") == 0)
    else:
        fail_closed = not cut_over or view.get("terminal_status") != "COMPLETED"
    rejection_observed = _rejection_observed(case_id, post)
    if case_id == "exhausted_recovery_attempts":
        # The router counts an attempt before reserving its successor, so the
        # counter alone can reach two with no second successor. Require two
        # distinct failed successors and the qualifier's own record of the
        # second one (M16 path attempt = source path attempt + 2).
        failed = post.get("successor_nodes_failed")
        facts = settled.qualification or {}
        rejection_observed = (
            rejection_observed
            and post.get("attempts_used") == 2
            and isinstance(failed, list) and len(failed) == 2
            and all(isinstance(nodes, list) and nodes for nodes in failed)
            and not set(failed[0]) & set(failed[1])
            and facts.get("successor_path_attempt") == executed.binding["path_attempt"] + 2)
    base = {
        **common,
        "rejection_observed": rejection_observed,
        "fail_closed": fail_closed,
        "terminal_state": _terminal_state(case_id, settled),
    }
    if case_id == "cleanup_authority_failure":
        base["unpublishable_suffix_discarded"] = post.get("unpublishable_suffix_discarded") is True
    return base


def derive_bindings(authority: ObservationAuthority, executed: Any,
                    settled: SettledFacts) -> dict[str, Any]:
    binding, view = executed.binding, settled.view
    source_attempt = binding["source_attempt"]
    committed = _cutover_identity(executed, view)
    successor = committed is not None
    final_attempt = None if committed is None else committed["request_attempt"]
    deadline = settled.deadline_unix_ms
    return {
        "source_digest": authority.source_digest,
        "spec_digest": authority.spec_digest,
        "contract_digest": authority.contract_digest,
        "operator_plan_digest": authority.operator_plan_digest,
        "run_id": authority.run_id,
        "deployment_id": binding["deployment_id"],
        "deployment_epoch": binding["deployment_epoch"],
        "request_id": executed.request_id,
        "source_path_id": binding["path_id"],
        "source_attempt": source_attempt,
        "source_path_attempt": binding["path_attempt"],
        # A path generation is the request-attempt publication generation it
        # served; A6 cutover increments both together.
        "source_path_generation": source_attempt,
        "successor_path_id": committed.get("path_id") if committed else None,
        "successor_attempt": final_attempt,
        "successor_path_attempt": committed.get("path_attempt") if committed else None,
        "successor_path_generation": final_attempt if successor else None,
        "successor_qualification_generation": (
            view.get("recovery_attempts") if successor else None),
        # The gateway-committed prefix frozen at the fault; a case without a
        # source fault binds the prefix the gateway committed by its terminal.
        "committed_token_count": (
            executed.committed_at_failure if executed.committed_at_failure is not None
            else (settled.gateway or {}).get("committed_count")),
        "source_absolute_deadline_unix_ms": deadline,
        # The successor command inherits the source command's absolute deadline.
        "successor_absolute_deadline_unix_ms": deadline if successor else None,
    }


def build_envelope(case_id: str, bindings: Mapping[str, Any],
                   observation: Mapping[str, Any]) -> dict[str, Any]:
    body = {
        "protocol": OBSERVATION_PROTOCOL,
        "authority": AUTHORITY,
        "case_id": case_id,
        "bindings": dict(bindings),
        "observation": dict(observation),
    }
    body["observation_digest"] = _sha256(_canonical(body))
    return body


class CaseObservationRecorder:
    """Watch executed cases until they settle, then write their observations."""

    def __init__(self, *, authority: ObservationAuthority, target: Any,
                 settle_seconds: float = 180.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._authority = authority
        self._target = target
        self._settle_seconds = settle_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._written: dict[str, str] = {}
        self._refused: dict[str, str] = {}

    @property
    def root(self) -> Path:
        return self._authority.evidence_output_dir / OBSERVATIONS_DIRNAME

    def observe(self, executed: Any) -> None:
        """Called by the fault target once a case executed; never blocks it."""

        thread = threading.Thread(target=self._record, args=(executed,),
                                  name=f"a6-observation-{executed.case_id}", daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _settle(self, executed: Any,
                prior_view: Mapping[str, Any] | None = None) -> SettledFacts:
        target = self._target
        deadline = self._clock() + self._settle_seconds
        view: dict[str, Any] = {}
        last_retained: dict[str, Any] = dict(prior_view or {})
        while True:
            current = target.view(executed.request_id)
            if current.get("retained") is True:
                last_retained = current
                view = current
            else:
                # The router releases a settled request; keep its final retained
                # view and add only what the release retained (a failure code).
                view = {**last_retained, **{key: value for key, value in current.items()
                                            if value is not None}, "retained": False}
            coordinator_terminal = target.coordinator_terminal(executed.request_id)
            settled = (view.get("retained") is False
                       or view.get("terminal_status") in _TERMINAL_STATUSES
                       or view.get("terminal_blocked_reason") is not None)
            gateway_done = (target.gateway_terminal_count(executed.request_id) or 0) >= 1
            if (settled and (gateway_done or view.get("terminal_blocked_reason"))) or (
                    self._clock() >= deadline):
                break
            self._sleep(0.05)
        committed_identity = getattr(target, "committed_route_identity", None)
        committed = (committed_identity(executed.request_id)
                     if callable(committed_identity) else None)
        if committed is not None:
            # The coordinator's cutover CAS is the committed identity; the
            # last polled view can predate a cutover that released at once.
            view = {**view, "route_identity": committed}
        marker = executed.failure_marker
        return SettledFacts(
            view=view,
            gateway=gateway_facts(
                target.service, executed.request_id,
                failure_sequence=None if marker is None else marker[0],
                committed_at_failure=None if marker is None else marker[1]),
            reservation_count=target.reservation_count(executed.request_id),
            coordinator_terminal=coordinator_terminal,
            qualification=(None if target.qualifier is None else
                           target.qualifier.issued_qualification_facts(executed.request_id)),
            deadline_unix_ms=executed.deadline_unix_ms,
        )

    def _record(self, executed: Any) -> None:
        case_id = executed.case_id
        if case_id == "cleanup_authority_failure":
            # The real product publishes a cleanup failure notice, so the
            # frozen v1 shape (zero terminal events) can never be satisfied;
            # the gate admits this case only under the owner-approved v2
            # contract. A v1 plan for it still fails: no v1 file is written.
            try:
                released = self._settle_release(executed, None)
                self.write_cleanup_authority_v2(
                    executed, released,
                    self._target.router.a6_command_authority(executed.request_id))
            except Exception as error:
                self._refuse(f"{case_id}.v2", executed, error)
            return
        try:
            settled = self._settle(executed)
            if (settled.view.get("terminal_blocked_reason") is not None
                    and not _cleanup_complete(settled)):
                # A blocked request keeps its reservations until a late exact
                # cleanup proof (for example once a stalled peer resumes)
                # releases them; measure cleanup after that, within the bound.
                settled = self._settle_release(executed, settled.view)
            self.write(case_id, executed, settled)
        except Exception as error:  # a refused case simply has no observation
            self._refuse(case_id, executed, error)

    def _refuse(self, key: str, executed: Any, error: BaseException) -> None:
        reason = str(error)[:96] or type(error).__name__
        with self._lock:
            self._refused[key] = reason
        record = {"event": "a6_case_observation_refused", "case": key,
                  "request_id": getattr(executed, "request_id", None),
                  "error_type": type(error).__name__, "reason": reason}
        detail = getattr(error, "detail", None)
        if isinstance(detail, Mapping):
            record["detail"] = dict(detail)
        _LOGGER.warning("%s", json.dumps(record, sort_keys=True,
                                         separators=(",", ":"), default=str))

    def _settle_release(self, executed: Any,
                        prior_view: Mapping[str, Any] | None) -> SettledFacts:
        """Wait for a blocked request's late cleanup proof to release it."""

        target = self._target
        deadline = self._clock() + self._settle_seconds
        while (self._clock() < deadline and not (
                target.coordinator_terminal(executed.request_id) is not None
                and target.reservation_count(executed.request_id) == 0)):
            self._sleep(0.05)
        return self._settle(executed, prior_view)

    def write(self, case_id: str, executed: Any, settled: SettledFacts) -> Path:
        if case_id not in REQUIRED_CASES:
            raise ObservationRefused("a6_observation_case_unknown")
        envelope = build_envelope(case_id, derive_bindings(self._authority, executed, settled),
                                  derive_observation(case_id, executed, settled))
        serialized = json.dumps(envelope, sort_keys=True)
        if any(marker in serialized for marker in _PRIVATE_MARKERS):
            raise ObservationRefused("a6_observation_private_material")
        try:
            validate_source_owned_observation(envelope, case_id=case_id)
        except ValueError as error:
            raise ObservationRefused(f"a6_observation_unsatisfied:{error}",
                                     detail=refusal_detail(envelope)) from error
        path = self.root / f"{case_id}.json"
        _private_dir(self._authority.evidence_output_dir)
        _write_once(path, envelope)
        with self._lock:
            self._written[case_id] = envelope["observation_digest"]
        return path

    def write_cleanup_authority_v2(self, executed: Any, settled: SettledFacts,
                                   command_authority: Mapping[str, Any] | None) -> Path:
        """The owner-approved v2 envelope for cleanup_authority_failure (R6)."""

        from mycelium_a6_cleanup_observation_v2 import (
            AUTHORITY as V2_AUTHORITY,
            CASE as V2_CASE,
            PROTOCOL as V2_PROTOCOL,
            reconcile_count_layers,
            validate_observation,
        )

        gateway = settled.gateway or {}
        view = settled.view
        receipt = gateway.get("cleanup_notice_provenance")
        # Owner ruling 2026-10-06: the refused successor is the registered
        # command attempt the gateway notice belongs to; its cutover is never
        # committed, so the coordinator keeps the source's route identity.
        route_identity = view.get("route_identity")
        committed_route_attempt = (route_identity.get("request_attempt")
                                   if isinstance(route_identity, Mapping) else None)
        successor_attempt = view.get("request_attempt")
        if (not isinstance(receipt, tuple) or len(receipt) != 4 or command_authority is None
                or type(committed_route_attempt) is not int
                or type(successor_attempt) is not int or successor_attempt != receipt[1]):
            raise ObservationRefused("a6_cleanup_v2_authority_unavailable", detail={
                "gateway_receipt_present": isinstance(receipt, tuple) and len(receipt) == 4,
                "command_authority_present": command_authority is not None,
                "committed_route_attempt": committed_route_attempt,
                "successor_attempt": successor_attempt,
                "gateway_terminal_kind": gateway.get("terminal_kind"),
                "gateway_terminal_code": gateway.get("terminal_code"),
                "terminal_blocked_reason": view.get("terminal_blocked_reason"),
                "cleanup_authority_refused": executed.post_state.get(
                    "cleanup_authority_refused"),
            })
        binding = executed.binding
        bindings = {
            "request_id": executed.request_id,
            "request_attempt": receipt[1],
            "source_digest": self._authority.source_digest,
            "candidate_digest": self._authority.candidate_digest,
            "spec_digest": self._authority.spec_digest,
            "contract_digest": self._authority.contract_digest,
            "operator_plan_digest": self._authority.operator_plan_digest,
            "run_id": self._authority.run_id,
            "deployment_id": binding["deployment_id"],
            "deployment_epoch": binding["deployment_epoch"],
            "source_path_id": binding["path_id"],
            "source_attempt": binding["source_attempt"],
            "source_path_attempt": binding["path_attempt"],
            "source_path_generation": binding["source_attempt"],
            "successor_path_id": view.get("path_id"),
            "successor_attempt": successor_attempt,
            "successor_path_attempt": view.get("path_attempt"),
            "successor_path_generation": successor_attempt,
        }
        post = executed.post_state
        observation = {
            "case_id": V2_CASE,
            "executed": True,
            "ordinary_product_path": settled.gateway is not None,
            "private_material_exposed": False,
            "rejection_observed": post.get("cleanup_authority_refused") is True,
            "fail_closed": gateway.get("terminal_kind") != "completed",
            "terminal_state": ("TERMINAL_BLOCKED"
                               if gateway.get("terminal_code") == "request_cleanup_unproven"
                               else _terminal_state(V2_CASE, settled)),
            "unpublishable_suffix_discarded": post.get("unpublishable_suffix_discarded") is True,
            "cutover_committed": committed_route_attempt != binding["source_attempt"],
            "committed_route_attempt": committed_route_attempt,
            "coordinator_terminal": settled.coordinator_terminal,
            "duplicated_token_count": gateway.get("duplicated_token_count"),
            "process_cleanup_complete": _cleanup_complete(settled),
            "stale_reservation_count": settled.reservation_count,
            "command_authority": dict(command_authority),
            "gateway_authority": {
                "request_id": receipt[0],
                "request_attempt": receipt[1],
                "terminal_event_count": gateway.get("terminal_count"),
                "terminal_kind": gateway.get("terminal_kind"),
                "terminal_code": gateway.get("terminal_code"),
            },
        }
        body = {"protocol": V2_PROTOCOL, "authority": V2_AUTHORITY, "case_id": V2_CASE,
                "bindings": bindings, "observation": observation}
        body["observation_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            body, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
            allow_nan=False).encode("utf-8")).hexdigest()
        try:
            validated = validate_observation(body)
            layers = reconcile_count_layers(
                command_authority=validated["command_authority"],
                gateway_authority=validated["gateway_authority"],
                cleanup_authority={
                    "request_id": bindings["request_id"],
                    "request_attempt": bindings["request_attempt"],
                    "stale_reservation_count": validated["stale_reservation_count"],
                    "process_cleanup_complete": validated["process_cleanup_complete"],
                },
                cutover_committed=validated["cutover_committed"],
            )
        except ValueError as error:
            raise ObservationRefused(f"a6_cleanup_v2_unsatisfied:{error}",
                                     detail=refusal_detail(body)) from error
        if layers["v2_envelope_authorized"] is not True:
            raise ObservationRefused("a6_cleanup_v2_unauthorized")
        path = self.root / f"{V2_CASE}.v2.json"
        _private_dir(self._authority.evidence_output_dir)
        _write_once(path, body)
        with self._lock:
            self._written[f"{V2_CASE}.v2"] = body["observation_digest"]
        return path

    def join(self, timeout: float) -> None:
        deadline = self._clock() + timeout
        with self._lock:
            threads = list(self._threads)
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - self._clock()))

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"root": str(self.root), "written": dict(self._written),
                    "refused": dict(self._refused), **self._authority.status()}


def local_listener_count(port: int) -> int:
    """1 when something still accepts on 127.0.0.1:port, else 0."""

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return 1
    except OSError:
        return 0


def write_cleanup_observation(*, authority: ObservationAuthority, product_port: int,
                              physical_process_count: int, reservation_count: int) -> Path:
    """Written by the product after its listener, route and peers are closed."""

    for value in (physical_process_count, reservation_count):
        if type(value) is not int or value < 0:
            raise ObservationRefused("a6_cleanup_observation_unmeasured")
    document = {
        "protocol": CLEANUP_PROTOCOL,
        "authority": AUTHORITY,
        "run_id": authority.run_id,
        "local_product_port_listener_count": local_listener_count(product_port),
        "physical_process_count": physical_process_count,
        "reservation_count": reservation_count,
    }
    path = authority.evidence_output_dir / OBSERVATIONS_DIRNAME / CLEANUP_FILENAME
    _private_dir(authority.evidence_output_dir)
    _write_once(path, document)
    return path


__all__ = [
    "CLEANUP_PROTOCOL",
    "CaseObservationRecorder",
    "ObservationAuthority",
    "ObservationRefused",
    "SettledFacts",
    "build_envelope",
    "derive_bindings",
    "derive_observation",
    "gateway_facts",
    "gateway_failure_marker",
    "local_listener_count",
    "observation_authority",
    "refusal_detail",
    "write_cleanup_observation",
]
