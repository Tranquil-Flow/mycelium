# SPDX-License-Identifier: AGPL-3.0-only
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
  draft proposed is ever committed without the target having drawn it.  When
  every candidate matched, the target's own decision after the last candidate
  (the **target bonus**, owner-approved 2026-10-07) is committed as well, unless
  it would complete the request: the last token stays an ordinary DECODE;
* the **policy**: deadline fallback to an ordinary DECODE round, the frozen circuit
  breaker (``circuit_breaker_decision``), cooldown and revocation.

Stage KV rollback needs nothing here: the entry's next operation names the new
watermark and every stage rewinds to it (``MLXRuntimePort`` speculative window).

Claim boundary: entry-side mechanics.  Runtime counters are privacy reduced (counts
and timings, no token ids).  Nothing here qualifies a deployment or claims benefit.
"""

from __future__ import annotations

import collections
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
#: Completed requests kept for gate evidence (the R9 run is 8 x 12 plus a smoke).
HISTORY_LIMIT = 512
_HISTORY_COUNTERS = (
    "rounds", "decode_rounds", "deadline_fallbacks", "proposed_count", "verified_count",
    "accepted_count", "rejected_count", "target_corrected_count", "target_bonus_count",
    "committed_count", "rollback_count",
)

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
    #: The target's own decision after an all-match span (``None`` if not taken).
    bonus: int | None = None


def arbitrate_round(
    *,
    context: Sequence[int],
    candidates: Sequence[int],
    decisions: Sequence[int],
    request_ref: str,
    runtime_incarnation: str,
    cancellation_generation: int = 0,
    bonus_allowed: bool = False,
) -> RoundCommit:
    """Run the A10 transaction over one returned VERIFY and return what it committed.

    ``bonus_allowed``: after an all-match span, also commit the target's own decision
    at the position after the last candidate (``decisions[width]``).  It is drawn by
    the target exactly as an ordinary DECODE would draw it, so output parity holds.
    """

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
    bonus = None
    if bonus_allowed and accepted == width and correction is None:
        bonus = int(port.decisions[width])
        committed = tuple(committed) + (bonus,)
    return RoundCommit(
        accepted=tuple(port.candidates[:accepted]),
        correction=correction,
        committed=tuple(committed),
        outcome={
            "proposed": width,
            "verified": int(outcome.verified_count),
            "accepted": accepted,
            "corrected": int(outcome.correction_count),
            "bonus": 0 if bonus is None else 1,
            "committed": len(committed),
            "commit_state": outcome.commit_state,
        },
        bonus=bonus,
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
    #: Commit the target's own decision after an all-match span (owner-approved).
    target_bonus: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.width, bool) or not isinstance(self.width, int) or not 1 <= self.width <= MAXIMUM_WIDTH:
            raise RouteSpeculationError("invalid_speculation_width")


@dataclass
class _RequestState:
    request_id: str
    draft: Any
    deadline: dict[str, Any]
    warm_target_tpot_p50_ms: float | None = None
    active: bool = True
    draft_closed: bool = False
    stop_reason: str | None = None
    rounds: int = 0
    decode_rounds: int = 0
    deadline_fallbacks: int = 0
    proposed: int = 0
    verified: int = 0
    accepted: int = 0
    rejected: int = 0
    corrected: int = 0
    bonus: int = 0
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
        baseline: Callable[[], tuple[float, float] | None] | None = None,
    ) -> None:
        self.policy = policy
        self._baseline = baseline
        self._draft_factory = draft_factory
        self._incarnation = runtime_incarnation
        self._clock = clock
        self._lock = threading.RLock()
        self._preferences: dict[str, str] = {}
        self._states: dict[str, _RequestState] = {}
        self._admissions: dict[str, dict[str, Any]] = {}
        # Empty unless an explicitly granted node control armed this request.
        self._draft_loss_after_accepted: dict[str, int] = {}
        self._revoked = False
        self._cooldown_until = 0.0
        self.completed: list[dict[str, Any]] = []
        #: Every released request, speculative or not, in completion order: counts
        #: and closed reasons only (no token ids).  Read by the node's signed
        #: ``a11_snapshot`` for the R9 coverage and window evidence.
        self.history: collections.deque[dict[str, Any]] = collections.deque(maxlen=HISTORY_LIMIT)
        self._history_sequence = 0

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
            warm_p50, warm_p95 = self.policy.warm_target_tpot_p50_ms, self.policy.warm_target_tpot_p95_ms
            if warm_p95 is None and self._baseline is not None:
                # The baseline measured on this exact route (target-only rounds),
                # frozen for this request at admission.
                measured = self._baseline()
                if measured is not None:
                    warm_p50, warm_p95 = measured
            deadline = sd.admit_proposal_deadline(
                warm_target_only_p95_tpot_ms=warm_p95,
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
                "warm_target_tpot_ms": None if warm_p95 is None else {"p50": warm_p50, "p95": warm_p95},
            }
            self._admissions[request_id] = record
            if reason is None:
                self._states[request_id] = _RequestState(
                    request_id=request_id,
                    draft=self._draft_factory(request_id),
                    deadline=dict(deadline),
                    warm_target_tpot_p50_ms=warm_p50,
                )
            return dict(record)

    def active(self, request_id: str) -> bool:
        with self._lock:
            state = self._states.get(request_id)
            return state is not None and state.active

    def arm_draft_loss(self, request_id: str, after_accepted: int) -> None:
        """One-shot request-bound loss at the next proposal after the threshold.

        This is deliberately not an authorization surface. Only the node's
        grant-checked control command may call it; normal routes never arm it.
        The check and consumption share the controller lock with release.
        """
        with self._lock:
            state = self._states.get(request_id)
            if (state is None or not state.active or request_id in self._draft_loss_after_accepted
                    or type(after_accepted) is not int or not 1 <= after_accepted <= 512
                    or state.accepted >= after_accepted):
                raise RouteSpeculationError("a11_draft_loss_request_unavailable")
            self._draft_loss_after_accepted[request_id] = after_accepted

    # -- one round ----------------------------------------------------------------
    def propose(self, request_id: str, context: Sequence[int], width: int) -> tuple[int, ...] | None:
        """The draft's bounded proposal, or ``None`` for an ordinary DECODE round."""

        with self._lock:
            state = self._states.get(request_id)
            if state is None or not state.active:
                return None
            threshold = self._draft_loss_after_accepted.get(request_id)
            if threshold is not None and state.accepted >= threshold:
                del self._draft_loss_after_accepted[request_id]
                self._stop(state, "draft_lost")
                return None
            # Do not overshoot the requested accepted-token trigger in a
            # multi-token VERIFY: cap only this armed request's next proposal.
            width = min(int(width), self.policy.width,
                        threshold - state.accepted if threshold is not None else self.policy.width)
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
        remaining: int | None = None,
    ) -> RoundCommit:
        """Arbitrate one returned VERIFY.  The returned tokens are target-committed.

        ``remaining`` is the request's output budget left before this round.  The
        target bonus is taken only when the request still has a token left after
        it, so the last token of every request remains an ordinary terminal DECODE.
        """

        state = self._states.get(request_id)
        result = arbitrate_round(
            context=context,
            candidates=candidates,
            decisions=decisions,
            request_ref=f"{request_id}:{len(context)}",
            runtime_incarnation=self._incarnation,
            cancellation_generation=cancellation_generation,
            bonus_allowed=(
                self.policy.target_bonus
                and remaining is not None
                and len(tuple(candidates)) + 1 < int(remaining)
            ),
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
        state.bonus += result.outcome["bonus"]
        state.committed += result.outcome["committed"]
        state.rollbacks += int(result.outcome["accepted"] < result.outcome["proposed"])
        state.draft_ms += state.pending_draft_ms
        if round_ms is not None:
            state.round_ms += round_ms
        tpot = state.warm_target_tpot_p50_ms
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
                tokens=result.accepted,
                correction_token=result.correction if result.bonus is None else result.bonus,
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
            if reason in {"draft_lost", "draft_not_reproducible"}:
                self._close_draft(state)

    @staticmethod
    def _close_draft(state: _RequestState) -> None:
        if state.draft_closed:
            return
        state.draft_closed = True
        if state.stop_reason == "draft_lost":
            mark_lost = getattr(state.draft, "mark_lost", None)
            if callable(mark_lost):
                try:
                    mark_lost()
                except Exception:  # noqa: BLE001 - target owns continuation
                    pass
        close = getattr(state.draft, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # noqa: BLE001 - draft loss must not abort target DECODE
                pass

    # -- lifecycle ----------------------------------------------------------------
    def release(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._draft_loss_after_accepted.pop(request_id, None)
            state = self._states.pop(request_id, None)
            admission = self._admissions.pop(request_id, None)
            preference = self._preferences.pop(request_id, None)
        if state is not None:
            self._close_draft(state)
        summary = None if state is None else self._summary(state, admission)
        with self._lock:
            self._history_sequence += 1
            self.history.append(
                {
                    "sequence": self._history_sequence,
                    "request_id": request_id,
                    "preference": preference if admission is None else admission["preference"],
                    "mode": "not_evaluated" if admission is None else admission["mode"],
                    "fallback_reason": None if admission is None else admission["fallback_reason"],
                    "stop_reason": None if state is None else state.stop_reason,
                    **{key: 0 if summary is None else summary[key] for key in _HISTORY_COUNTERS},
                }
            )
        if summary is None:
            return None
        self.completed.append(summary)
        del self.completed[:-256]
        return summary

    def evidence(self) -> dict[str, Any]:
        """The ordered request history (counts and closed reasons only)."""

        with self._lock:
            return {
                "protocol": PROTOCOL,
                "history_sequence": self._history_sequence,
                "requests": [dict(record) for record in self.history],
            }

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
            "target_bonus_count": state.bonus,
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
