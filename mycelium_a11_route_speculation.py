"""A11 Wave D: target-authoritative speculative rounds on the live route (entry side).

The entry coordinator owns the committed token list -- the target watermark -- and
the client sink.  This controller gives it three things per speculative request:

* a **draft proposal** from the request's own draft runtime (draft KV only), synced
  to the target-committed context and bounded by the admitted proposal deadline
  (``mycelium_speculative_decode.admit_proposal_deadline``);
* the **target-authoritative arbitration** of one returned VERIFY: the decisions the
  target's final stage drew for every fed position are evaluated by the unchanged
  A10 transaction arithmetic (``a10_verifier_execution.verify_candidate_positions``)
  over :class:`RouteVerificationPort`, in mode
  ``commit_verified_prefix_plus_target_correction``.  The committed tokens are the
  accepted prefix plus, on a mismatch, the target's own correction; nothing the
  draft proposed is ever committed without the target having drawn it;
* the **policy**: deadline fallback to an ordinary DECODE round, the frozen circuit
  breaker (``circuit_breaker_decision``), cooldown and revocation.

Stage KV rollback needs nothing here: the entry's next operation names the new
watermark and every stage rewinds to it (``MLXRuntimePort`` speculative window).

Claim boundary: entry-side mechanics.  Runtime counters are privacy reduced (counts
and timings, no token ids).  Nothing here qualifies a deployment or claims benefit.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import mycelium_speculative_decode as sd
from mycelium_router.a10_runtime_verifier import committed_prefix_digest
from mycelium_router.a10_target_verification import TargetVerificationRequest
from mycelium_router.a10_verifier_execution import (
    PositionDecision,
    PositionEvaluation,
    verify_candidate_positions,
)

PROTOCOL = "mycelium.a11_route_speculation.v1"
TRANSACTION_MODE = "commit_verified_prefix_plus_target_correction"
MAXIMUM_WIDTH = 16

#: Closed reasons a request runs target-only (admission) or stops speculating.
TARGET_ONLY_REASONS = frozenset(
    {
        "preference_target_only",
        "no_authorized_local_draft",
        "unsupported_generation_policy",
        "target_verifier_unavailable",
        "kv_successor_coverage_active",
        "warm_target_only_p95_tpot_unknown",
        "request_deadline_unavailable",
        "qualification_revoked",
        "cooldown_active",
    }
)
STOP_REASONS = frozenset(
    {
        "circuit_breaker_open",
        "qualification_revoked",
        "draft_not_reproducible",
        "draft_lost",
    }
)
#: Backends whose stage runtime implements the VERIFY phase.
VERIFY_BACKENDS = frozenset({"mlx"})


class RouteSpeculationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _digest(document: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# A10 port over one returned VERIFY
# ---------------------------------------------------------------------------


class _RouteTransaction:
    def __init__(self, port: "RouteVerificationPort") -> None:
        self._port = port
        self._base = port.context
        self._tokens = port.context
        self._accepted = 0
        self._evaluated = False
        self._closed = False

    def committed_prefix_position(self) -> int:
        return len(self._tokens)

    def committed_prefix_digest(self) -> str:
        return committed_prefix_digest(self._tokens, len(self._tokens) - 1)

    def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation:
        candidates, decisions = self._port.candidates, self._port.decisions
        if self._evaluated or self._closed:
            raise RouteSpeculationError("verification_already_evaluated")
        if candidate_position_count != len(candidates) or len(decisions) != len(candidates) + 1:
            raise RouteSpeculationError("staged_candidate_width_mismatch")
        accepted = 0
        flags = []
        for offset, candidate in enumerate(candidates):
            agreed = accepted == offset and candidate == decisions[offset]
            if agreed:
                accepted += 1
            flags.append(PositionDecision(offset=offset, accepted=agreed))
        self._accepted = accepted
        self._evaluated = True
        return PositionEvaluation(
            invocation_count=1,
            prefix_position=len(self._base),
            prefix_digest=committed_prefix_digest(self._base, len(self._base) - 1),
            decisions=tuple(flags),
        )

    def commit_verified_prefix(self, committed_count: int) -> None:
        if not self._evaluated or self._closed:
            raise RouteSpeculationError("verification_not_evaluated")
        if committed_count < self._accepted or committed_count > self._accepted + 1:
            raise RouteSpeculationError("excess_commit_width")
        committed = list(self._port.candidates[: self._accepted])
        if committed_count > self._accepted:
            committed.append(self._port.decisions[self._accepted])
        self._tokens = self._base + tuple(committed)

    def rollback_to_prefix(self) -> None:
        self._tokens = self._base

    def cleanup(self) -> str:
        self._closed = True
        return "released"


class RouteVerificationPort:
    """``TargetVerificationRuntimePort`` whose one backend call is the route VERIFY.

    The target stages already ran the bounded multi-position pass; the final stage's
    per-position decisions arrived in a ``VerifyEvent``.  ``sequential_reference`` is
    not available on the route: sequential parity of the route verifier is qualified
    separately (target-only output parity of every completed request).
    """

    def __init__(self, *, context: Sequence[int], candidates: Sequence[int], decisions: Sequence[int]) -> None:
        self.context = tuple(int(token) for token in context)
        self.candidates = tuple(int(token) for token in candidates)
        self.decisions = tuple(int(token) for token in decisions)
        if not self.context or not self.candidates:
            raise RouteSpeculationError("invalid_route_verification")
        self.transaction: _RouteTransaction | None = None

    def begin_verification(self) -> _RouteTransaction:
        self.transaction = _RouteTransaction(self)
        return self.transaction

    def sequential_reference(self, candidate_position_count: int):  # pragma: no cover - by design
        raise RouteSpeculationError("route_sequential_reference_unavailable")


@dataclass(frozen=True)
class RoundCommit:
    accepted: tuple[int, ...]
    correction: int | None
    committed: tuple[int, ...]
    outcome: dict[str, Any]


def arbitrate_round(
    *,
    context: Sequence[int],
    candidates: Sequence[int],
    decisions: Sequence[int],
    request_ref: str,
    runtime_incarnation: str,
    cancellation_generation: int = 0,
) -> RoundCommit:
    """Run the A10 transaction over one returned VERIFY and return what it committed."""

    port = RouteVerificationPort(context=context, candidates=candidates, decisions=decisions)
    width = len(port.candidates)
    request = TargetVerificationRequest(
        request_ref=request_ref,
        target_kv_ref=f"route-kv:{request_ref}",
        runtime_incarnation=runtime_incarnation,
        committed_prefix_position=len(port.context),
        committed_prefix_digest=committed_prefix_digest(port.context, len(port.context) - 1),
        candidate_position_count=width,
        candidate_bytes=4 * width,
        generation_policy="deterministic_greedy",
        transaction_mode=TRANSACTION_MODE,
        deadline_at=float("1e12"),
        cancellation_generation=cancellation_generation,
        idempotency_digest=_digest({"ref": request_ref, "prefix": len(port.context), "width": width}),
    )
    outcome = verify_candidate_positions(port, request)
    transaction = port.transaction
    assert transaction is not None
    committed = transaction._tokens[len(port.context):]
    accepted = int(outcome.accepted_prefix_count)
    correction = int(committed[-1]) if outcome.correction_count else None
    if tuple(committed[:accepted]) != port.candidates[:accepted]:
        raise RouteSpeculationError("committed_prefix_not_target_verified")
    return RoundCommit(
        accepted=tuple(port.candidates[:accepted]),
        correction=correction,
        committed=tuple(committed),
        outcome={
            "proposed": width,
            "verified": int(outcome.verified_count),
            "accepted": accepted,
            "corrected": int(outcome.correction_count),
            "committed": len(committed),
            "commit_state": outcome.commit_state,
        },
    )


# ---------------------------------------------------------------------------
# Policy and per-request state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RouteSpeculationPolicy:
    """Frozen per-deployment policy.  ``warm_*`` are measured target-only TPOT values."""

    width: int
    warm_target_tpot_p95_ms: float | None
    warm_target_tpot_p50_ms: float | None
    generation_policy: str = "deterministic_greedy"
    draft_identity_digest: str | None = None
    target_binding_digest: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.width, bool) or not isinstance(self.width, int) or not 1 <= self.width <= MAXIMUM_WIDTH:
            raise RouteSpeculationError("invalid_speculation_width")


@dataclass
class _RequestState:
    request_id: str
    draft: Any
    deadline: dict[str, Any]
    active: bool = True
    stop_reason: str | None = None
    rounds: int = 0
    decode_rounds: int = 0
    deadline_fallbacks: int = 0
    proposed: int = 0
    verified: int = 0
    accepted: int = 0
    rejected: int = 0
    corrected: int = 0
    committed: int = 0
    rollbacks: int = 0
    draft_ms: float = 0.0
    round_ms: float = 0.0
    cycles: list[dict[str, Any]] = field(default_factory=list)
    breaker: dict[str, Any] = field(default_factory=lambda: {"state": "closed", "reason": "within_policy", "cooldown_seconds": None})
    pending_started: float | None = None
    pending_draft_ms: float = 0.0


class RouteSpeculationController:
    """Entry-side A11 controller: one draft runtime and one policy state per request."""

    def __init__(
        self,
        *,
        policy: RouteSpeculationPolicy,
        draft_factory: Callable[[str], Any] | None,
        runtime_incarnation: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.policy = policy
        self._draft_factory = draft_factory
        self._incarnation = runtime_incarnation
        self._clock = clock
        self._lock = threading.RLock()
        self._preferences: dict[str, str] = {}
        self._states: dict[str, _RequestState] = {}
        self._admissions: dict[str, dict[str, Any]] = {}
        self._revoked = False
        self._cooldown_until = 0.0
        self.completed: list[dict[str, Any]] = []

    # -- admission ---------------------------------------------------------------
    def set_preference(self, request_id: str, preference: str) -> None:
        if preference not in {"target_only", "speculative_requested"}:
            raise RouteSpeculationError("invalid_speculation_preference")
        with self._lock:
            self._preferences[request_id] = preference

    def revoke(self) -> None:
        with self._lock:
            self._revoked = True

    def admission(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._admissions.get(request_id)
            return None if record is None else dict(record)

    def admit(
        self,
        request_id: str,
        *,
        backends: Sequence[str],
        kv_successor_covered: bool,
        remaining_deadline_ms: float | None,
    ) -> dict[str, Any]:
        with self._lock:
            existing = self._admissions.get(request_id)
            if existing is not None:
                return dict(existing)
            preference = self._preferences.get(request_id, "target_only")
            reason: str | None = None
            deadline = sd.admit_proposal_deadline(
                warm_target_only_p95_tpot_ms=self.policy.warm_target_tpot_p95_ms,
                remaining_request_deadline_ms=remaining_deadline_ms,
            )
            if preference != "speculative_requested":
                reason = "preference_target_only"
            elif self._revoked:
                reason = "qualification_revoked"
            elif self._clock() < self._cooldown_until:
                reason = "cooldown_active"
            elif self._draft_factory is None:
                reason = "no_authorized_local_draft"
            elif self.policy.generation_policy not in sd.QUALIFIED_GENERATION_POLICIES:
                reason = "unsupported_generation_policy"
            elif not backends or any(backend not in VERIFY_BACKENDS for backend in backends):
                reason = "target_verifier_unavailable"
            elif kv_successor_covered:
                reason = "kv_successor_coverage_active"
            elif not deadline["admissible"]:
                reason = (
                    "warm_target_only_p95_tpot_unknown"
                    if deadline["clamp_reason"] == sd.DEADLINE_UNKNOWN_REASON
                    else "request_deadline_unavailable"
                )
            record = {
                "request_id": request_id,
                "preference": preference,
                "mode": "target_only" if reason else "speculative_admitted",
                "fallback_reason": reason,
                "deadline": dict(deadline),
                "width": self.policy.width,
            }
            self._admissions[request_id] = record
            if reason is None:
                self._states[request_id] = _RequestState(
                    request_id=request_id,
                    draft=self._draft_factory(request_id),
                    deadline=dict(deadline),
                )
            return dict(record)

    def active(self, request_id: str) -> bool:
        with self._lock:
            state = self._states.get(request_id)
            return state is not None and state.active

    # -- one round ----------------------------------------------------------------
    def propose(self, request_id: str, context: Sequence[int], width: int) -> tuple[int, ...] | None:
        """The draft's bounded proposal, or ``None`` for an ordinary DECODE round."""

        state = self._states.get(request_id)
        if state is None or not state.active:
            return None
        width = min(int(width), self.policy.width)
        if width < 1:
            return None
        started = self._clock()
        try:
            state.draft.sync(context)
            proposal = state.draft.propose(
                prefix_position=len(context),
                prefix_digest=committed_prefix_digest(tuple(context), len(context) - 1),
                width=width,
                deadline_ms=float(state.deadline["admitted_deadline_ms"]),
            )
        except Exception:  # noqa: BLE001 - draft failure is draft loss, never target loss
            self._stop(state, "draft_lost")
            return None
        if proposal.lost:
            self._stop(state, "draft_lost")
            return None
        if proposal.unavailable or not proposal.tokens:
            state.deadline_fallbacks += 1
            self._record_cycle(state, {"deadline_missed": True, "speculative_cycle_ms": None, "equivalent_target_only_ms": None})
            return None
        state.pending_started = started
        state.pending_draft_ms = (self._clock() - started) * 1000.0
        return tuple(int(token) for token in proposal.tokens)

    def abandon(self, request_id: str, proposal_width: int) -> None:
        """A proposal was not dispatched (state moved): discard the draft's tentative KV."""

        state = self._states.get(request_id)
        if state is None:
            return
        try:
            state.draft.rollback_suffix(int(proposal_width))
        except Exception:  # noqa: BLE001
            self._stop(state, "draft_lost")
        state.pending_started = None

    def complete_round(
        self,
        request_id: str,
        *,
        context: Sequence[int],
        candidates: Sequence[int],
        decisions: Sequence[int],
        cancellation_generation: int = 0,
    ) -> RoundCommit:
        """Arbitrate one returned VERIFY.  The returned tokens are target-committed."""

        state = self._states.get(request_id)
        result = arbitrate_round(
            context=context,
            candidates=candidates,
            decisions=decisions,
            request_ref=f"{request_id}:{len(context)}",
            runtime_incarnation=self._incarnation,
            cancellation_generation=cancellation_generation,
        )
        if state is None:
            return result
        now = self._clock()
        round_ms = None if state.pending_started is None else (now - state.pending_started) * 1000.0
        state.pending_started = None
        state.rounds += 1
        state.proposed += result.outcome["proposed"]
        state.verified += result.outcome["verified"]
        state.accepted += result.outcome["accepted"]
        state.rejected += result.outcome["verified"] - result.outcome["accepted"]
        state.corrected += result.outcome["corrected"]
        state.committed += result.outcome["committed"]
        state.rollbacks += int(result.outcome["accepted"] < result.outcome["proposed"])
        state.draft_ms += state.pending_draft_ms
        if round_ms is not None:
            state.round_ms += round_ms
        tpot = self.policy.warm_target_tpot_p50_ms
        self._record_cycle(
            state,
            {
                "deadline_missed": False,
                "speculative_cycle_ms": round_ms,
                "equivalent_target_only_ms": None if tpot is None else tpot * len(result.committed),
            },
        )
        try:
            reproducible = state.draft.commit_verified_prefix(
                tokens=result.accepted, correction_token=result.correction
            )
        except Exception:  # noqa: BLE001
            reproducible = False
        if not reproducible:
            self._stop(state, "draft_not_reproducible")
        return result

    def note_decode_round(self, request_id: str) -> None:
        state = self._states.get(request_id)
        if state is not None:
            state.decode_rounds += 1

    # -- policy -------------------------------------------------------------------
    def _record_cycle(self, state: _RequestState, cycle: dict[str, Any]) -> None:
        state.cycles.append(cycle)
        del state.cycles[: -sd.CIRCUIT_BREAKER_ROLLING_WINDOW_CYCLES]
        decision = sd.circuit_breaker_decision(cycles=state.cycles)
        state.breaker = decision
        if decision["state"] != "closed":
            if decision["cooldown_seconds"]:
                with self._lock:
                    self._cooldown_until = max(self._cooldown_until, self._clock() + float(decision["cooldown_seconds"]))
            self._stop(state, "circuit_breaker_open")

    def _stop(self, state: _RequestState, reason: str) -> None:
        if state.active:
            state.active = False
            state.stop_reason = reason

    # -- lifecycle ----------------------------------------------------------------
    def release(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.pop(request_id, None)
            admission = self._admissions.pop(request_id, None)
            self._preferences.pop(request_id, None)
        if state is None:
            return None
        summary = self._summary(state, admission)
        self.completed.append(summary)
        del self.completed[:-256]
        return summary

    def snapshot(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._states.get(request_id)
            admission = self._admissions.get(request_id)
        if state is None:
            return None if admission is None else {"protocol": PROTOCOL, "admission": dict(admission)}
        return self._summary(state, admission)

    def _summary(self, state: _RequestState, admission: Mapping[str, Any] | None) -> dict[str, Any]:
        draft_snapshot = getattr(state.draft, "snapshot", None)
        draft = draft_snapshot() if callable(draft_snapshot) else {}
        return {
            "protocol": PROTOCOL,
            "admission": None if admission is None else dict(admission),
            "active": state.active,
            "stop_reason": state.stop_reason,
            "rounds": state.rounds,
            "decode_rounds": state.decode_rounds,
            "deadline_fallbacks": state.deadline_fallbacks,
            "proposed_count": state.proposed,
            "verified_count": state.verified,
            "accepted_count": state.accepted,
            "rejected_count": state.rejected,
            "target_corrected_count": state.corrected,
            "committed_count": state.committed,
            "rollback_count": state.rollbacks,
            "draft_ms_total": round(state.draft_ms, 3),
            "speculative_round_ms_total": round(state.round_ms, 3),
            "circuit_breaker": dict(state.breaker),
            "draft_kv_positions": draft.get("kv_positions"),
            "draft_resets": draft.get("resets"),
        }


__all__ = [
    "PROTOCOL",
    "RoundCommit",
    "RouteSpeculationController",
    "RouteSpeculationError",
    "RouteSpeculationPolicy",
    "RouteVerificationPort",
    "STOP_REASONS",
    "TARGET_ONLY_REASONS",
    "TRANSACTION_MODE",
    "VERIFY_BACKENDS",
    "arbitrate_round",
]
