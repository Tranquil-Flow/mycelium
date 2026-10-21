# SPDX-License-Identifier: AGPL-3.0-only
"""A11 target-authoritative speculative-decode execution (wave-2 source tranche).

This module is the *implemented* A11 runtime cycle.  It replaces the scaffold's
refusing execution seams (``mycelium_speculative_decode.transfer_proposal_envelope``,
``open_target_verification_transaction``'s in-process decision source, and the
``*_not_implemented`` boundary functions) with real deterministic mechanics, and it
is **wired to the A10 verified-token path**: every admitted cycle hands one bounded
multi-position candidate set to the A10 target verifier
(``mycelium_router.a10_target_verification.verify_candidate_positions``) over a real
``mycelium_router.a10_verifier_execution.TargetVerificationRuntimePort`` supplied by
this module, and commits exactly the target-authoritative result the A10 transaction
reports.

Authority boundary, as implemented here:

* the target runtime (``A10TargetKvRuntime``) owns target KV, the committed prefix,
  the correction token and the committed watermark; only it may advance them;
* the draft runtime owns draft KV and uncommitted proposals and physically cannot
  write target state;
* the private proposal envelope carries the minimum token-position binding needed by
  the verifier; the public projections carry counts, positions and digests only;
* output is released once, and only after the target commit succeeds;
* duplicate identical cycles are idempotent; conflicting duplicates, stale prefixes,
  parity failures, verifier refusals and incarnation drift fail closed to the
  previous target watermark.

Claim boundary: this is deterministic, in-process behaviour over an injected target
decision source, an in-process transport and an injected cancellation scope.  It is
**not** physical evidence: no physical draft/target route, no authenticated network
proposal transfer, no live-route paired benchmark, no browser or eight-workspace
product surface, no qualification, no benefit claim, and no A11 closure requirement
is earned here.

The three seams that used to refuse as scaffold boundaries
(``run_live_route_benchmark``, ``execute_physical_qualification``,
``project_live_workspace_state``) are implemented as deterministic in-process
behaviour.  Each one runs real :func:`run_speculative_cycle` work over the **injected**
transport, the **injected** target decision source and the **injected** cancellation
scope, and returns a document stamped
``evidence_class="deterministic_in_process"`` in which every physical, qualification,
promotion and product-surface flag is false.  Each one refuses with a closed contract
code when an injection is missing -- a seam never invents its own transport, target
authority or draft proposal -- and with a closed ``SpeculativeDecodeExecutionUnavailable``
claim boundary when a caller asks for a physical, live-route, authenticated-network or
browser evidence class.  ``EXECUTION_BOUNDARIES`` therefore names exactly the evidence
classes that remain unavailable, and no seam can return a document that reads as one.

Reused, not rebuilt: the closed plan/runtime contracts, the adaptive-deadline
arithmetic, the circuit-breaker policy and the deterministic parity oracle all come
from ``mycelium_speculative_decode`` (already merged).  The A10 verifier, its
transaction shape and its parity oracle come from ``mycelium_router`` (already
merged).
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, NoReturn, Protocol, Sequence, runtime_checkable

import mycelium_speculative_decode as sd
from mycelium_router.a10_target_verification import (
    TRANSACTION_MODES as A10_TRANSACTION_MODES,
    TargetVerificationRequest as A10VerificationRequest,
    A10ContractError,
    verify_candidate_positions as a10_verify_candidate_positions,
)
from mycelium_router.a10_verifier_execution import (
    PositionDecision,
    PositionEvaluation,
    SequentialReference,
)

PROTOCOL = "mycelium.speculative_decode_execution.v1"
EXECUTION_STAGE = "deterministic_source"

#: The merged A10 modules this tranche wires to.  Named here so a test (and a
#: reviewer) can see the dependency without reading the imports.
A10_WIRING = (
    "mycelium_router.a10_target_verification.verify_candidate_positions",
    "mycelium_router.a10_verifier_execution.TargetVerificationRuntimePort",
    "mycelium_router.a10_verifier_execution",
)

CLAIM_BOUNDARY = (
    "deterministic in-process A11 speculative-cycle execution wired to the A10 target "
    "verifier port: private bounded proposal envelope over an in-process transport, "
    "target-authoritative commit/correction/rollback arbitration, separate target/draft "
    "KV ownership, cancellation checkpoints and a privacy-reduced runtime projection. "
    "The three former scaffold seams (route benchmark, qualification gates, workspace "
    "projection) run as deterministic in-process behaviour over injected dependencies "
    "and publish counters and digests only. "
    "No physical draft/target route, no authenticated network proposal transfer, no "
    "live-route benchmark, no browser or eight-workspace product surface, no "
    "qualification, no benefit claim and no promotion authorization; A11 closure "
    "requirements are untouched."
)

PROPOSAL_BYTES_PER_TOKEN = 4

#: Checked in this exact order on the admitted path.  All six precede every
#: irreversible state change: the ledger/target commit and the output release happen
#: in one block after the last checkpoint, so a cancellation at any checkpoint leaves
#: the committed watermark and the emitted output exactly where they were.
CANCELLATION_CHECKPOINTS = (
    "before_draft_work",
    "before_transfer",
    "before_target_verification",
    "after_verifier_return",
    "before_target_commit",
    "before_output",
)

#: The evidence classes no source lane can earn.  Each maps to the claim boundary that
#: refuses it, and the union of those boundaries is exactly ``EXECUTION_BOUNDARIES``:
#: a seam asked for one of these refuses instead of returning a document, so a
#: deterministic in-process run can never be published as physical, live-route,
#: authenticated-network or browser evidence.
REFUSED_EVIDENCE_CLASSES = {
    "physical": "physical_draft_target_route_not_available",
    "live_route": "live_route_product_session_not_available",
    "authenticated_network": "authenticated_network_proposal_transfer_not_available",
    "browser": "browser_workspace_surface_not_available",
}

#: The evidence class every implemented seam publishes, and the only one it accepts.
EMITTED_EVIDENCE_CLASS = "deterministic_in_process"

#: The claim boundaries that remain: physical hardware, a live product route, the
#: authenticated private network path and the browser product surface.  Each is a real
#: callable that raises ``SpeculativeDecodeExecutionUnavailable`` with its own code and
#: never returns a document.
EXECUTION_BOUNDARIES = {
    "physical_draft_target_route_not_available": "require_physical_draft_target_route",
    "live_route_product_session_not_available": "require_live_route_product_session",
    "authenticated_network_proposal_transfer_not_available": (
        "require_authenticated_network_proposal_transfer"
    ),
    "browser_workspace_surface_not_available": "require_browser_workspace_surface",
}

#: The three former scaffold seams, now implemented as deterministic in-process
#: behaviour over injected dependencies.  The keys are the semantic seam names; the
#: values are the frozen scaffold function names that stay the public surface.
LIVE_SEAMS = {
    "route_benchmark": "run_live_route_benchmark",
    "physical_qualification": "execute_physical_qualification",
    "workspace_projection": "project_live_workspace_state",
}

#: The closed set of refusals the implemented seams may raise.  Missing injections are
#: *contract* violations (``SpeculativeDecodeExecutionError``): the seam is implemented,
#: the caller did not supply the in-process execution it must run over.  None of these
#: is a capability boundary, and none may be raised as one.
#:
#: ``router_decode`` is the injection key the router decode seam
#: (``mycelium_router.decoding.resolve_speculative_decode``) uses: it is a real consumer
#: of the same injection contract, so its refusals are registered here too.
_ROUTER_DECODE_SEAM_KEY = "router_decode"
_SEAM_INJECTIONS = (
    "requires_injected_transport",
    "requires_injected_cancellation_scope",
    "requires_injected_target_decision_source",
    "requires_injected_draft_runtime",
)
SEAM_REFUSALS = frozenset(
    [
        f"{key}_{suffix}"
        for key in tuple(sorted(LIVE_SEAMS)) + (_ROUTER_DECODE_SEAM_KEY,)
        for suffix in _SEAM_INJECTIONS
    ]
    + [
        "route_benchmark_requires_executed_cycles",
        "physical_qualification_requires_injected_negative_arm",
        "injection_type_mismatch",
        "invalid_evidence_class",
        "invalid_request_id",
        "projection_privacy_violation",
    ]
)

#: The injection key the router decode seam uses.  Exported so the router module and
#: this module cannot drift on the string.
ROUTER_DECODE_SEAM_KEY = _ROUTER_DECODE_SEAM_KEY

#: The injection refusals a *router decode seam* caller may be handed back (rather than
#: raising), because the router decode path always has the target-only fallback.
INJECTION_REFUSALS = frozenset(
    f"{_ROUTER_DECODE_SEAM_KEY}_{suffix}" for suffix in _SEAM_INJECTIONS
) | {"injection_type_mismatch"}

#: The frozen eight product workspaces, in the order ``ui/web/src/app/navigation.ts``
#: publishes them (bound by a test that reads that file, so the projection cannot drift
#: from the product surface it claims to name).
PRODUCT_WORKSPACES = (
    "inference",
    "lab",
    "network",
    "nodes",
    "plans",
    "readiness",
    "incidents",
    "settings",
)

BENCHMARK_EXECUTION_PROTOCOL = "mycelium.speculative_decode_execution_benchmark.v1"
QUALIFICATION_EXECUTION_PROTOCOL = "mycelium.speculative_decode_execution_qualification.v1"
WORKSPACE_PROJECTION_PROTOCOL = "mycelium.speculative_decode_execution_workspace.v1"

#: Key names that must never appear in a seam projection: the deterministic seams
#: publish counts, positions and digests only.
_FORBIDDEN_PROJECTION_KEYS = frozenset(
    {
        "prompt",
        "prompt_token_ids",
        "tokens",
        "token_ids",
        "candidate_token_ids",
        "logits",
        "activation",
        "activation_bytes_payload",
        "kv_bytes",
        "credential",
        "secret",
    }
)

#: Execution-level abstain/fallback reasons.  These are *not* the frozen public
#: runtime vocabulary (``sd._FALLBACK_REASONS``); they explain, at execution level,
#: why no speculative work ran.  The public projection maps them into the frozen
#: vocabulary or reports target-only mode.
ABSTAIN_REASONS = frozenset(
    {
        "no_authorized_local_draft",
        "target_verifier_unavailable",
        "incompatible_token_semantics",
        "parity_failed",
        "insufficient_acceptance",
        "draft_deadline_exceeded",
        "material_gain_not_observed",
        "confidence_bound_not_met",
        "warm_target_only_p95_tpot_unknown",
        "request_deadline_unknown",
        "request_deadline_expired",
        "revoked_requalification_required",
    }
)

#: Execution failure -> the frozen public fallback vocabulary.  Total by construction:
#: a failure that is not listed raises instead of inventing a reason.
FAILURE_FALLBACK_REASONS = {
    "parity_failed": "verifier_parity_failure",
    "a10_verification_refused": "verifier_parity_failure",
    "stale_prefix": "invalid_proposal",
    "conflicting_duplicate_cycle": "invalid_proposal",
    "idempotent_commit_position_mismatch": "invalid_proposal",
    "invalid_proposal": "invalid_proposal",
    "incarnation_drift": "compatibility_violation",
    "unsupported_generation_policy": "compatibility_violation",
    "draft_lost": "draft_lost",
    "draft_unavailable": "draft_unavailable",
    "proposal_deadline_missed": "proposal_deadline_missed",
    "target_lost_explicit": "none",
}

_SHA256_LENGTH = 71
_GENESIS_DIGEST = "sha256:" + "0" * 64
_TOKEN_CEILING = 1 << 31


class SpeculativeDecodeExecutionError(ValueError):
    """A closed A11 execution refusal. ``code`` is the closed reason."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")

    def __str__(self) -> str:  # pragma: no cover - exercised through message equality
        return self.code if not self.detail else f"{self.code}: {self.detail}"


class SpeculativeDecodeExecutionUnavailable(RuntimeError):
    """A11 execution requires hardware, a live route or a browser. Never a success."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        self.detail = detail
        super().__init__(code if not detail else f"{code}: {detail}")

    def __str__(self) -> str:  # pragma: no cover - exercised through message equality
        return self.code if not self.detail else f"{self.code}: {self.detail}"


# ---------------------------------------------------------------------------
# Small closed helpers (mirroring mycelium_speculative_decode, not rebuilding it)
# ---------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _text(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise SpeculativeDecodeExecutionError(code)
    return value


def _sha256(value: Any, code: str) -> str:
    text = _text(value, code)
    if not text.startswith("sha256:") or len(text) != _SHA256_LENGTH:
        raise SpeculativeDecodeExecutionError(code)
    try:
        int(text.removeprefix("sha256:"), 16)
    except ValueError as exc:
        raise SpeculativeDecodeExecutionError(code) from exc
    return text


def _position(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SpeculativeDecodeExecutionError(code)
    return value


def _token(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value >= _TOKEN_CEILING:
        raise SpeculativeDecodeExecutionError(code)
    return value


def _token_sequence(value: Any, code: str, *, allow_empty: bool = False) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise SpeculativeDecodeExecutionError(code)
    if not len(value) and not allow_empty:
        raise SpeculativeDecodeExecutionError(code)
    return tuple(_token(item, code) for item in value)


def _finite(value: Any, code: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SpeculativeDecodeExecutionError(code)
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise SpeculativeDecodeExecutionError(code)
    return result


def _prefix_digest(*, parent_digest: str, position: int, tokens: Sequence[int]) -> str:
    """Digest of a target-owned prefix: parent digest, position and committed tokens.

    Identical to ``mycelium_speculative_decode._prefix_digest`` so a watermark produced
    here is the same object the merged scaffold arithmetic would produce.
    """

    return _digest(
        {"parent": parent_digest, "position": int(position), "tokens": [int(item) for item in tokens]}
    )


def _genesis_prefix_digest(position: int) -> str:
    return _prefix_digest(parent_digest=_GENESIS_DIGEST, position=position, tokens=())


# ---------------------------------------------------------------------------
# Private bounded proposal envelope
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalEnvelope:
    """The minimum private token-position binding the target verifier needs.

    ``tokens`` is private-path state.  ``public_projection()`` is the only shape that
    may leave the private path: counts, positions and digests, never a token id.
    """

    cycle_ref: str
    request_id: str
    attempt: int
    generation_policy: str
    width: int
    token_count: int
    byte_count: int
    prefix_position: int
    prefix_digest: str
    draft_runtime_incarnation: str
    idempotency_digest: str
    envelope_digest: str
    tokens: tuple[int, ...] = field(repr=False, default=())

    def public_projection(self) -> dict[str, Any]:
        return {
            "cycle_ref": self.cycle_ref,
            "request_id": self.request_id,
            "attempt": self.attempt,
            "generation_policy": self.generation_policy,
            "width": self.width,
            "token_count": self.token_count,
            "byte_count": self.byte_count,
            "prefix_position": self.prefix_position,
            "prefix_digest": self.prefix_digest,
            "draft_runtime_incarnation": self.draft_runtime_incarnation,
            "idempotency_digest": self.idempotency_digest,
            "envelope_digest": self.envelope_digest,
        }


def build_proposal_envelope(
    *,
    plan: Mapping[str, Any],
    cycle_ref: str,
    request_id: str,
    attempt: int,
    tokens: Any,
    prefix_position: int,
    prefix_digest: str,
    draft_runtime_incarnation: str,
) -> ProposalEnvelope:
    """Build the bounded, cycle-bound private proposal envelope.

    Load-time bounds are the plan's ``proposal_bounds`` intersected with the verifier's
    ``qualified_width``.  Excess width, count or bytes is a closed refusal; a token
    sequence that is empty or not a sequence of token ids is the same refusal.
    """

    document = sd.validate_speculative_decode_plan(plan)
    try:
        sequence = _token_sequence(tokens, "proposal_envelope_out_of_bounds")
    except SpeculativeDecodeExecutionError as exc:
        raise SpeculativeDecodeExecutionError("proposal_envelope_out_of_bounds", "tokens") from exc
    width = len(sequence)

    bounds = document["proposal_bounds"]
    maximum_width = min(int(bounds["maximum_width"]), int(document["verifier"]["qualified_width"]))
    byte_count = width * PROPOSAL_BYTES_PER_TOKEN
    if (
        width < 1
        or width > maximum_width
        or width > int(bounds["maximum_count"])
        or byte_count > int(bounds["maximum_bytes"])
    ):
        raise SpeculativeDecodeExecutionError("proposal_envelope_out_of_bounds", f"width={width}")
    if attempt < 1:
        raise SpeculativeDecodeExecutionError("proposal_envelope_out_of_bounds", "attempt")

    reference = _text(cycle_ref, "invalid_cycle_ref")
    request = _text(request_id, "request_id_invalid")
    incarnation = _sha256(draft_runtime_incarnation, "invalid_runtime_incarnation")
    try:
        bound_prefix_digest = _sha256(prefix_digest, "invalid_committed_prefix_digest")
    except SpeculativeDecodeExecutionError as exc:
        raise SpeculativeDecodeExecutionError("invalid_committed_prefix_digest", "prefix_digest") from exc
    committed_position = _position(prefix_position, "invalid_committed_prefix_position")

    policy = document["compatibility"]["generation_policy"]
    if policy not in sd.QUALIFIED_GENERATION_POLICIES:
        raise SpeculativeDecodeExecutionError("unsupported_generation_policy", policy)

    tokens_digest = _digest({"tokens": list(sequence)})
    idempotency_digest = _digest(
        {
            "plan": document["plan_digest"],
            "cycle": reference,
            "request": request,
            "attempt": int(attempt),
            "width": width,
            "prefix": [committed_position, bound_prefix_digest],
            "tokens": tokens_digest,
        }
    )
    envelope_digest = _digest(
        {
            "protocol": PROTOCOL,
            "plan": document["plan_digest"],
            "cycle": reference,
            "request": request,
            "attempt": int(attempt),
            "policy": policy,
            "width": width,
            "span": [committed_position, bound_prefix_digest],
            "bytes": byte_count,
            "incarnation": incarnation,
            "tokens": tokens_digest,
            "idempotency": idempotency_digest,
        }
    )
    return ProposalEnvelope(
        cycle_ref=reference,
        request_id=request,
        attempt=int(attempt),
        generation_policy=policy,
        width=width,
        token_count=width,
        byte_count=byte_count,
        prefix_position=committed_position,
        prefix_digest=bound_prefix_digest,
        draft_runtime_incarnation=incarnation,
        idempotency_digest=idempotency_digest,
        envelope_digest=envelope_digest,
        tokens=sequence,
    )


class ProposalTransport:
    """Deterministic in-process stand-in for the authenticated private runtime path.

    It owns no network socket and no credential: it bounds the buffer, counts what it
    moved and reports its own transfer cost.  A physical proposal transfer is still
    unavailable (see ``EXECUTION_BOUNDARIES``).
    """

    def __init__(self, *, transfer_ms: float = 0.4, maximum_bytes: int | None = None) -> None:
        self._transfer_ms = _finite(transfer_ms, "invalid_transfer_cost")
        self._maximum_bytes = None if maximum_bytes is None else _position(maximum_bytes, "invalid_transfer_bound")
        self._delivered = 0
        self._bytes = 0

    def deliver(self, envelope: ProposalEnvelope) -> float:
        if not isinstance(envelope, ProposalEnvelope):
            raise SpeculativeDecodeExecutionError("invalid_proposal_envelope")
        if self._maximum_bytes is not None and envelope.byte_count > self._maximum_bytes:
            raise SpeculativeDecodeExecutionError(
                "proposal_transfer_buffer_exceeded", f"bytes={envelope.byte_count}"
            )
        self._delivered += 1
        self._bytes += envelope.byte_count
        return self._transfer_ms

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport": "in_process_private",
            "delivered": self._delivered,
            "bytes": self._bytes,
            "authenticated_network_transport": False,
        }


# ---------------------------------------------------------------------------
# Cancellation scope
# ---------------------------------------------------------------------------


class CancellationScope:
    """A4-style cancellation boundary: checked at each frozen checkpoint, in order."""

    def __init__(self, *, cancel_at: str | None = None) -> None:
        if cancel_at is not None and cancel_at not in CANCELLATION_CHECKPOINTS:
            raise SpeculativeDecodeExecutionError("invalid_cancellation_checkpoint", cancel_at)
        self._cancel_at = cancel_at
        self._requested = False
        self._generation = 0
        self._checked: list[str] = []
        self._cancelled_at: str | None = None

    def cancel(self) -> None:
        self._requested = True
        self._generation += 1

    def cancellation_generation(self) -> int:
        return self._generation

    def is_cancelled(self, checkpoint: str) -> bool:
        if checkpoint not in CANCELLATION_CHECKPOINTS:
            raise SpeculativeDecodeExecutionError("invalid_cancellation_checkpoint", checkpoint)
        if self._cancelled_at is not None:
            return True
        self._checked.append(checkpoint)
        if self._requested or self._cancel_at == checkpoint:
            self._requested = True
            self._cancelled_at = checkpoint
            return True
        return False

    def snapshot(self) -> dict[str, Any]:
        return {
            "checked": list(self._checked),
            "cancelled_at": self._cancelled_at,
            "cancellation_generation": self._generation,
            "checkpoints": list(CANCELLATION_CHECKPOINTS),
        }


# ---------------------------------------------------------------------------
# Draft runtime: draft KV ownership only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DraftProposal:
    """One bounded draft proposal.  Tokens never leave the private path."""

    tokens: tuple[int, ...] = field(repr=False, default=())
    draft_queue_ms: float = 0.0
    draft_compute_ms: float = 0.0
    lost: bool = False
    unavailable: bool = False


@runtime_checkable
class DraftRuntimePort(Protocol):
    """The declared draft-runtime shape. The draft owns draft KV, nothing else."""

    def propose(
        self, *, prefix_position: int, prefix_digest: str, width: int, deadline_ms: float
    ) -> DraftProposal: ...

    def commit_verified_prefix(self, *, tokens: Sequence[int], correction_token: int | None) -> bool: ...

    def rollback_suffix(self, count: int) -> None: ...


class DeterministicDraftRuntime:
    """Deterministic draft runtime with its own KV ledger and no target access."""

    def __init__(
        self,
        propose_tokens: Any,
        *,
        incarnation: str,
        kv_ref: str = "draft-kv-1",
        timing_ms: tuple[float, float] = (1.0, 2.0),
        unavailable: bool = False,
        lost: bool = False,
        reproducible: bool = True,
    ) -> None:
        if not callable(propose_tokens):
            raise SpeculativeDecodeExecutionError("invalid_draft_runtime")
        self._propose_tokens = propose_tokens
        self._incarnation = _sha256(incarnation, "invalid_runtime_incarnation")
        self._kv_ref = _text(kv_ref, "invalid_draft_kv_ref")
        queue_ms, compute_ms = timing_ms
        self._queue_ms = _finite(queue_ms, "invalid_draft_queue_ms")
        self._compute_ms = _finite(compute_ms, "invalid_draft_compute_ms")
        self._unavailable = bool(unavailable)
        self._lost = bool(lost)
        self._reproducible = bool(reproducible)
        self._committed: tuple[int, ...] = ()
        self._tentative = 0
        self._proposals = 0
        self._rolled_back = 0

    @property
    def draft_runtime_incarnation(self) -> str:
        return self._incarnation

    def propose(
        self, *, prefix_position: int, prefix_digest: str, width: int, deadline_ms: float
    ) -> DraftProposal:
        self._proposals += 1
        if self._unavailable:
            return DraftProposal(unavailable=True)
        if self._lost:
            return DraftProposal(lost=True)
        _position(prefix_position, "invalid_committed_prefix_position")
        _sha256(prefix_digest, "invalid_committed_prefix_digest")
        tokens = _token_sequence(self._propose_tokens(int(prefix_position), int(width)), "invalid_draft_proposal")
        # Draft KV advances tentatively while proposing; only the target-confirmed
        # prefix is committed later, and a rejected suffix is rolled back.
        self._tentative = len(tokens)
        return DraftProposal(
            tokens=tokens, draft_queue_ms=self._queue_ms, draft_compute_ms=self._compute_ms
        )

    def commit_verified_prefix(self, *, tokens: Sequence[int], correction_token: int | None) -> bool:
        if not self._reproducible:
            return False
        committed = list(_token_sequence(tokens, "invalid_draft_commit", allow_empty=True))
        if correction_token is not None:
            committed.append(_token(correction_token, "invalid_correction_token"))
        self._committed = tuple(committed)
        self._tentative = 0
        return True

    def rollback_suffix(self, count: int) -> None:
        discarded = _position(count, "invalid_rollback_count")
        self._tentative = max(0, self._tentative - discarded)
        self._rolled_back += discarded

    def snapshot(self) -> dict[str, Any]:
        return {
            "role": "draft",
            "kv_ref": self._kv_ref,
            "draft_runtime_incarnation": self._incarnation,
            "committed_tokens": len(self._committed),
            "tentative_tokens": self._tentative,
            "proposals": self._proposals,
            "rolled_back_positions": self._rolled_back,
            "wrote_target_state": False,
        }


# ---------------------------------------------------------------------------
# Target runtime: the A10 verification port over target-owned KV
# ---------------------------------------------------------------------------


class _A10KvTransaction:
    """One target-owned KV transaction, transaction-local until A11 applies it.

    Implements the A10 ``VerificationTransaction`` protocol.  Commit and rollback
    change only this transaction; the target runtime's committed prefix moves only
    when the cycle explicitly applies a committed result, so a parity failure or a
    refusal can never leave the watermark advanced.
    """

    def __init__(self, runtime: "A10TargetKvRuntime") -> None:
        self._runtime = runtime
        self._prefix_position = runtime._committed_position
        self._prefix_digest = runtime._committed_digest
        self._candidates: tuple[int, ...] = ()
        self._decisions: tuple[int, ...] = ()
        self._committed_position = self._prefix_position
        self._committed_digest = self._prefix_digest
        self._committed_count = 0
        self._evaluated = False
        self._cleaned = False

    # -- A10 VerificationTransaction protocol ---------------------------------
    def committed_prefix_position(self) -> int:
        # The *current* committed prefix: it starts at the transaction's opening
        # prefix and advances only when this transaction commits.
        return self._committed_position

    def committed_prefix_digest(self) -> str:
        return self._committed_digest

    def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation:
        width = _position(candidate_position_count, "invalid_candidate_position_count")
        candidates = self._runtime.staged_candidates()
        if width != len(candidates):
            raise SpeculativeDecodeExecutionError(
                "staged_candidate_width_mismatch", f"{len(candidates)}!={width}"
            )
        raw = self._runtime.evaluate(candidates)
        self._candidates = candidates
        self._decisions = raw
        self._evaluated = True
        self._runtime.note_evaluation(width)
        decisions = tuple(
            PositionDecision(offset=index, accepted=candidates[index] == raw[index]) for index in range(width)
        )
        return PositionEvaluation(
            invocation_count=1,
            prefix_position=self._prefix_position,
            prefix_digest=self._prefix_digest,
            decisions=decisions,
        )

    def commit_verified_prefix(self, committed_count: int) -> None:
        count = _position(committed_count, "invalid_commit_width")
        accepted = self.accepted_prefix_length()
        if count > accepted + 1 or count < accepted:
            # The commit may only be the accepted prefix, optionally plus the single
            # target-authoritative correction drawable from the same call.
            raise SpeculativeDecodeExecutionError("invalid_commit_width", count)
        committed_tokens = list(self._candidates[:accepted])
        if count > accepted:
            committed_tokens.append(self._decisions[accepted])
        self._committed_count = count
        self._committed_position = self._prefix_position + count
        self._committed_digest = _prefix_digest(
            parent_digest=self._prefix_digest,
            position=self._committed_position,
            tokens=committed_tokens,
        )
        self._runtime.note_commit(
            position=self._committed_position,
            digest=self._committed_digest,
            committed_count=count,
        )

    def rollback_to_prefix(self) -> None:
        self._committed_position = self._prefix_position
        self._committed_digest = self._prefix_digest
        self._committed_count = 0
        self._runtime.note_rollback()

    def cleanup(self) -> str:
        self._cleaned = True
        return self._runtime.note_cleanup()

    # -- private helpers -----------------------------------------------------
    def accepted_prefix_length(self) -> int:
        accepted = 0
        for index, decision in enumerate(self._decisions):
            if self._candidates[index] == decision:
                accepted += 1
            else:
                break
        return accepted

    def committed_digest(self) -> str:
        return self._committed_digest

    def committed_count(self) -> int:
        return self._committed_count


class A10TargetKvRuntime:
    """The exact target runtime shape the A10 verifier requires.

    Satisfies ``mycelium_router.a10_verifier_execution.TargetVerificationRuntimePort``:
    it opens target-owned KV transactions from the exact committed prefix and can
    produce the sequential target-authority oracle for the same prefix.  Candidate
    token ids are staged privately here and never enter an A10 request record.
    """

    def __init__(
        self,
        source: Any,
        *,
        target_kv_ref: str,
        target_runtime_incarnation: str,
        prefix_position: int = 0,
        prefix_digest: str | None = None,
    ) -> None:
        if not callable(getattr(source, "evaluate_bounded_positions", None)):
            raise SpeculativeDecodeExecutionError("invalid_target_decision_source")
        self._source = source
        self._kv_ref = _text(target_kv_ref, "invalid_target_kv_ref")
        self._incarnation = _sha256(target_runtime_incarnation, "invalid_runtime_incarnation")
        self._committed_position = _position(prefix_position, "invalid_committed_prefix_position")
        self._committed_digest = (
            _genesis_prefix_digest(self._committed_position)
            if prefix_digest is None
            else _sha256(prefix_digest, "invalid_committed_prefix_digest")
        )
        self._staged: tuple[int, ...] = ()
        self._decisions: tuple[int, ...] = ()
        self._commits = 0
        self._restores = 0
        self._invocations = 0
        self._open_transactions = 0
        self._cleanups = 0
        self._target_lost = False
        self._last_commit: dict[str, Any] | None = None

    # -- A10 TargetVerificationRuntimePort -----------------------------------
    def begin_verification(self) -> _A10KvTransaction:
        self._open_transactions += 1
        return _A10KvTransaction(self)

    def sequential_reference(self, candidate_position_count: int) -> SequentialReference:
        width = _position(candidate_position_count, "invalid_candidate_position_count")
        candidates = self.staged_candidates()
        if width != len(candidates):
            raise SpeculativeDecodeExecutionError("staged_candidate_width_mismatch")
        oracle = getattr(self._source, "sequential_decisions", None)
        if callable(oracle):
            raw = oracle(prefix_digest=self._committed_digest, candidates=candidates)
        else:
            raw = tuple(
                self._source.evaluate_bounded_positions(
                    prefix_digest=self._committed_digest, candidates=candidates[: index + 1]
                )[-1]
                for index in range(width)
            )
        decisions = tuple(_token(item, "invalid_sequential_decision") for item in raw)
        if len(decisions) != width:
            raise SpeculativeDecodeExecutionError("invalid_sequential_decision")
        accepted = next(
            (index for index, (candidate, decision) in enumerate(zip(candidates, decisions)) if candidate != decision),
            width,
        )
        correction = 1 if accepted < width else 0
        committed_tokens = list(candidates[:accepted])
        if correction:
            committed_tokens.append(decisions[accepted])
        position = self._committed_position + accepted + correction
        return SequentialReference(
            prefix_position=self._committed_position,
            prefix_digest=self._committed_digest,
            decisions=tuple(
                PositionDecision(offset=index, accepted=candidates[index] == decisions[index])
                for index in range(width)
            ),
            committed_position=position,
            committed_digest=_prefix_digest(
                parent_digest=self._committed_digest, position=position, tokens=committed_tokens
            ),
        )

    # -- target-owned state --------------------------------------------------
    @property
    def target_kv_ref(self) -> str:
        return self._kv_ref

    @property
    def target_runtime_incarnation(self) -> str:
        return self._incarnation

    def committed_prefix_position(self) -> int:
        return self._committed_position

    def committed_prefix_digest(self) -> str:
        return self._committed_digest

    def prepare_candidates(self, tokens: Any) -> tuple[int, ...]:
        """Stage the private candidate token ids for the next verification call."""

        self._staged = _token_sequence(tokens, "invalid_candidate_tokens")
        self._decisions = ()
        return self._staged

    def staged_candidates(self) -> tuple[int, ...]:
        if not self._staged:
            raise SpeculativeDecodeExecutionError("no_staged_candidates")
        return self._staged

    def evaluate(self, candidates: tuple[int, ...]) -> tuple[int, ...]:
        """One real bounded call into the injected target decision source."""

        self._invocations += 1
        raw = self._source.evaluate_bounded_positions(
            prefix_digest=self._committed_digest, candidates=candidates
        )
        decisions = tuple(_token(item, "target_decision_invalid") for item in raw)
        if len(decisions) != len(candidates):
            raise SpeculativeDecodeExecutionError("target_decision_invalid")
        self._decisions = decisions
        return decisions

    def verified_decisions(self) -> tuple[int, ...]:
        """The private per-position target decisions of the last bounded call."""

        return self._decisions

    def last_commit_snapshot(self) -> dict[str, Any] | None:
        return None if self._last_commit is None else dict(self._last_commit)

    def apply_commit(self, *, position: int, digest: str) -> None:
        """Apply an already-arbitrated target commit to the committed watermark."""

        self._committed_position = _position(position, "invalid_commit_position")
        self._committed_digest = _sha256(digest, "invalid_committed_prefix_digest")
        self._commits += 1

    def restore(self, *, position: int, digest: str) -> None:
        """Fail closed to an exact previously committed watermark."""

        self._committed_position = _position(position, "invalid_commit_position")
        self._committed_digest = _sha256(digest, "invalid_committed_prefix_digest")
        self._restores += 1

    def mark_target_lost(self) -> None:
        self._target_lost = True

    def target_lost(self) -> bool:
        return self._target_lost

    # -- bookkeeping ---------------------------------------------------------
    def note_evaluation(self, width: int) -> None:
        if width < 1:
            raise SpeculativeDecodeExecutionError("invalid_candidate_position_count")

    def note_commit(self, *, position: int, digest: str, committed_count: int) -> None:
        self._last_commit = {
            "position": position,
            "digest": digest,
            "committed_count": committed_count,
        }

    def note_rollback(self) -> None:
        self._last_commit = None

    def note_cleanup(self) -> str:
        self._cleanups += 1
        self._open_transactions = max(0, self._open_transactions - 1)
        return "released"

    def snapshot(self) -> dict[str, Any]:
        return {
            "role": "target",
            "kv_ref": self._kv_ref,
            "target_runtime_incarnation": self._incarnation,
            "position": self._committed_position,
            "digest": self._committed_digest,
            "commits": self._commits,
            "restores": self._restores,
            "bounded_invocations": self._invocations,
            "open_transactions": self._open_transactions,
            "cleanups": self._cleanups,
            "cleanup": "released",
            "target_lost": self._target_lost,
            "aliased_with_draft": False,
            "owns_draft_state": False,
        }


# ---------------------------------------------------------------------------
# The A11 runtime cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CycleExecution:
    """One A11 speculative cycle: the private execution plus public-shaped records."""

    request_id: str
    cycle_ref: str
    attempt: int
    admission: dict[str, Any]
    mode: str
    proposal_width: int
    deadline: dict[str, Any]
    envelope: dict[str, Any] | None
    a10_request: dict[str, Any] | None
    a10_request_object: Any
    a10_outcome: dict[str, Any] | None
    parity: dict[str, Any] | None
    commit: dict[str, Any] | None
    failure: dict[str, Any] | None
    target_kv: dict[str, Any]
    draft_kv: dict[str, Any]
    cancellation: dict[str, Any]
    circuit_breaker: dict[str, Any]
    output: dict[str, Any]
    runtime_request: dict[str, Any]
    runtime_document: dict[str, Any]
    claim: str = CLAIM_BOUNDARY

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self.__dict__)


def runtime_binding_from_plan(
    plan: Mapping[str, Any],
    *,
    deployment_id: str = "deployment-a11",
    deployment_epoch: int = 1,
    membership_generation: int = 1,
) -> dict[str, Any]:
    """The exact target/draft/verifier binding a runtime record is scoped to."""

    document = sd.validate_speculative_decode_plan(plan)
    return {
        "deployment_id": _text(deployment_id, "invalid_deployment_id"),
        "deployment_epoch": _position(deployment_epoch, "invalid_deployment_epoch") or 1,
        "graph_digest": document["target"]["representation_digest"],
        "membership_generation": _position(membership_generation, "invalid_membership_generation") or 1,
        "qualification_digest": document["verifier"]["target_runtime_binding_digest"],
        "target_runtime_incarnation": document["target"]["runtime_incarnation"],
        "draft_runtime_incarnation": document["draft"]["runtime_incarnation"],
    }


def build_runtime_document(
    *, mode: str, binding: Mapping[str, Any], requests: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Seal a ``mycelium.speculative_decode_runtime.v1`` record and validate it."""

    document = sd.seal(
        {
            "protocol": sd.RUNTIME_PROTOCOL,
            "binding": dict(binding),
            "mode": mode,
            "requests": [dict(item) for item in requests],
        }
    )
    return sd.validate_speculative_decode_runtime(document)


def _admit(document: Mapping[str, Any], request_id: str, preference: str) -> dict[str, Any]:
    decision = sd.integrate_request_admission(
        plan=document, request_id=request_id, preference=preference
    )
    if decision["mode"] == "speculative_admitted" and document["verifier"]["status"] != "qualified":
        decision = dict(decision)
        decision.update(
            {
                "mode": "target_only",
                "admission": "target_only_fallback",
                "fallback_reason": "target_verifier_unavailable",
                "overlay_disabled_reason": "target_verifier_unavailable",
                "effective_generation_policy": None,
            }
        )
    return decision


def _admit_deadline(document: Mapping[str, Any], deadline_ms: Any) -> tuple[float | None, str, dict[str, Any]]:
    policy = document["deadline_policy"]
    remaining = policy["remaining_request_deadline_ms"]
    if deadline_ms is None:
        admitted = sd.admit_proposal_deadline(
            warm_target_only_p95_tpot_ms=policy["warm_target_only_p95_tpot_ms"],
            remaining_request_deadline_ms=remaining,
        )
        if not admitted["admissible"]:
            return None, str(admitted["clamp_reason"]), dict(admitted)
        return float(admitted["admitted_deadline_ms"]), str(admitted["clamp_reason"]), dict(admitted)

    value = _finite(deadline_ms, "verification_deadline_invalid")
    if value < sd.DEADLINE_MINIMUM_MS:
        value, reason = float(sd.DEADLINE_MINIMUM_MS), "floor_applied"
    elif value > sd.DEADLINE_MAXIMUM_MS:
        value, reason = float(sd.DEADLINE_MAXIMUM_MS), "ceiling_applied"
    else:
        value, reason = float(value), "within_bounds"
    if remaining is not None and float(remaining) < value:
        value, reason = float(remaining), "request_deadline_clamp"
    return value, reason, {"admitted_deadline_ms": value, "clamp_reason": reason, "admissible": True}


def _runtime_request(
    *,
    request_id: str,
    mode: str,
    proposal_width: int,
    proposed: int,
    verified: int,
    accepted: int,
    rejected: int,
    corrected: int,
    rollback: int,
    committed: int,
    deadline_outcome: str,
    fallback_reason: str,
    cancelled: bool,
    circuit_breaker_state: str,
    terminal_state: str,
    draft_queue_ms: float | None = None,
    draft_compute_ms: float | None = None,
    transfer_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "request_id": request_id,
        "mode": mode,
        "proposal_width": proposal_width,
        "proposed_count": proposed,
        "verified_count": verified,
        "accepted_count": accepted,
        "rejected_count": rejected,
        "target_corrected_count": corrected,
        "rollback_count": rollback,
        "committed_count": committed,
        "target_committed_output_tokens": committed,
        "deadline_outcome": deadline_outcome,
        "draft_queue_ms": draft_queue_ms,
        "draft_compute_ms": draft_compute_ms,
        "transfer_ms": transfer_ms,
        "fallback_reason": fallback_reason,
        "cancelled": cancelled,
        "circuit_breaker_state": circuit_breaker_state,
        "cleanup_complete": True,
        "terminal_state": terminal_state,
    }


def run_speculative_cycle(
    *,
    plan: Mapping[str, Any],
    request_id: str,
    cycle_ref: str,
    attempt: int,
    width: int,
    target: A10TargetKvRuntime,
    draft: Any,
    transport: ProposalTransport,
    cancellation: CancellationScope,
    preference: str = "speculative_requested",
    ledger: sd.TargetPrefixLedger | None = None,
    deadline_ms: float | None = None,
    target_only_ms: float | None = None,
    window: list[dict[str, Any]] | None = None,
) -> CycleExecution:
    """Run one real A11 cycle over the A10 verified-token path.

    The order is the specification's: admit, admit the deadline, check cancellation
    before draft work, let the draft propose within the admitted width, build the
    bounded private envelope, transfer it, check cancellation, verify the bounded
    candidates in exactly one A10 call, decide parity, check cancellation, then commit
    the target-authoritative result (or fail closed), commit the draft-confirmed
    prefix, and release output once.
    """

    document = sd.validate_speculative_decode_plan(plan)
    if not isinstance(target, A10TargetKvRuntime):
        raise SpeculativeDecodeExecutionError("invalid_target_runtime")
    if not isinstance(transport, ProposalTransport):
        raise SpeculativeDecodeExecutionError("invalid_proposal_transport")
    if not isinstance(cancellation, CancellationScope):
        raise SpeculativeDecodeExecutionError("invalid_cancellation_scope")
    reference = _text(cycle_ref, "invalid_cycle_ref")
    request = _text(request_id, "request_id_invalid")
    _position(attempt, "invalid_attempt")

    admitted_width = _position(width, "proposal_width_out_of_bounds")
    maximum_width = min(
        int(document["proposal_bounds"]["maximum_width"]),
        int(document["verifier"]["qualified_width"]),
    )
    if admitted_width < 1 or admitted_width > maximum_width:
        raise SpeculativeDecodeExecutionError("proposal_width_out_of_bounds", f"width={admitted_width}")

    binding = runtime_binding_from_plan(document)
    admission = _admit(document, request, preference)
    window = window if window is not None else []
    ledger = ledger if ledger is not None else sd.TargetPrefixLedger(
        position=target.committed_prefix_position(), digest=target.committed_prefix_digest()
    )

    def finish(
        *,
        mode: str,
        runtime_request: dict[str, Any],
        envelope: dict[str, Any] | None = None,
        a10_request: dict[str, Any] | None = None,
        a10_request_object: Any = None,
        a10_outcome: dict[str, Any] | None = None,
        parity: dict[str, Any] | None = None,
        commit: dict[str, Any] | None = None,
        failure: dict[str, Any] | None = None,
        breaker: dict[str, Any] | None = None,
        deadline: dict[str, Any] | None = None,
        released: int = 0,
        already_released: bool = False,
        rolled_back: int = 0,
    ) -> CycleExecution:
        state = breaker if breaker is not None else sd.circuit_breaker_decision(cycles=list(window))
        breaker_record = {
            "state": state["state"],
            "reason": state["reason"],
            "cooldown_seconds": state["cooldown_seconds"],
            "requalification_required": state["state"] == "revoked_requalification_required",
        }
        target_kv = target.snapshot()
        target_kv["rolled_back_positions"] = rolled_back
        draft_kv = draft.snapshot()
        document_record = build_runtime_document(
            mode="target_only" if mode == "target_only" else "speculative",
            binding=binding,
            requests=[runtime_request],
        )
        return CycleExecution(
            request_id=request,
            cycle_ref=reference,
            attempt=int(attempt),
            admission=admission,
            mode=mode,
            proposal_width=admitted_width,
            deadline=deadline if deadline is not None else {"admitted_deadline_ms": None, "clamp_reason": None, "missed": False},
            envelope=envelope,
            a10_request=a10_request,
            a10_request_object=a10_request_object,
            a10_outcome=a10_outcome,
            parity=parity,
            commit=commit,
            failure=failure,
            target_kv=target_kv,
            draft_kv=draft_kv,
            cancellation=cancellation.snapshot(),
            circuit_breaker=breaker_record,
            output={"released": released > 0, "released_tokens": released, "already_released": already_released},
            runtime_request=runtime_request,
            runtime_document=document_record,
        )

    # -- admission ----------------------------------------------------------
    if admission["mode"] != "speculative_admitted":
        reason = admission["fallback_reason"]
        runtime_request = _runtime_request(
            request_id=request,
            mode="target_only",
            proposal_width=0,
            proposed=0,
            verified=0,
            accepted=0,
            rejected=0,
            corrected=0,
            rollback=0,
            committed=0,
            deadline_outcome="none",
            fallback_reason="none",
            cancelled=False,
            circuit_breaker_state="closed",
            terminal_state="completed",
        )
        return finish(
            mode="target_only",
            runtime_request=runtime_request,
            deadline={
                "admitted_deadline_ms": None,
                "clamp_reason": reason,
                "missed": False,
                "abstain_reason": reason,
            },
        )

    admitted_ms, clamp_reason, _deadline_evidence = _admit_deadline(document, deadline_ms)
    if admitted_ms is None:
        runtime_request = _runtime_request(
            request_id=request,
            mode="target_only",
            proposal_width=0,
            proposed=0,
            verified=0,
            accepted=0,
            rejected=0,
            corrected=0,
            rollback=0,
            committed=0,
            deadline_outcome="none",
            fallback_reason="none",
            cancelled=False,
            circuit_breaker_state="closed",
            terminal_state="completed",
        )
        return finish(
            mode="target_only",
            runtime_request=runtime_request,
            deadline={
                "admitted_deadline_ms": None,
                "clamp_reason": clamp_reason,
                "missed": False,
                "abstain_reason": clamp_reason,
            },
        )

    deadline_record = {"admitted_deadline_ms": admitted_ms, "clamp_reason": clamp_reason, "missed": False}

    # -- the cycle is admitted: everything below is speculative --------------
    prefix_position = target.committed_prefix_position()
    prefix_digest = target.committed_prefix_digest()
    if ledger.position != prefix_position or ledger.digest != prefix_digest:
        # Stale prefix/generation: no draft work, no verifier call, no commit.
        runtime_request = _fallback_request(
            request, width=admitted_width, deadline_outcome="none", fallback_reason="invalid_proposal"
        )
        _note_failure(window, deadline_missed=False, speculative_ms=None, target_only_ms=target_only_ms)
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            failure={"failure_reason": "stale_prefix", "detail": "target_watermark_mismatch"},
            deadline=deadline_record,
            breaker=sd.circuit_breaker_decision(
                compatibility_violation=True, cycles=list(window)
            ),
        )

    if cancellation.is_cancelled("before_draft_work"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, 0
        )

    proposal = draft.propose(
        prefix_position=prefix_position,
        prefix_digest=prefix_digest,
        width=admitted_width,
        deadline_ms=admitted_ms,
    )
    observed_ms = float(proposal.draft_queue_ms) + float(proposal.draft_compute_ms)
    if proposal.lost:
        return _fallback(
            finish, window, request, admitted_width, "draft_lost", "none", observed_ms, target_only_ms, deadline_record,
            draft=draft, tentative=len(proposal.tokens),
        )
    if proposal.unavailable:
        return _fallback(
            finish, window, request, admitted_width, "draft_unavailable", "none", observed_ms, target_only_ms, deadline_record,
            draft=draft, tentative=len(proposal.tokens),
        )
    if not proposal.tokens:
        return _fallback(
            finish, window, request, admitted_width, "invalid_proposal", "none", observed_ms, target_only_ms, deadline_record,
            draft=draft, tentative=0,
        )
    if observed_ms > admitted_ms:
        # No valid proposal arrived by the deadline: the target performs its ordinary
        # next step and its KV is untouched.
        return _fallback(
            finish,
            window,
            request,
            admitted_width,
            "proposal_deadline_missed",
            "proposal_deadline_missed",
            observed_ms,
            target_only_ms,
            {**deadline_record, "missed": True},
            draft=draft,
            tentative=len(proposal.tokens),
        )

    envelope = build_proposal_envelope(
        plan=document,
        cycle_ref=reference,
        request_id=request,
        attempt=attempt,
        tokens=proposal.tokens,
        prefix_position=prefix_position,
        prefix_digest=prefix_digest,
        draft_runtime_incarnation=draft.draft_runtime_incarnation,
    )
    if envelope.width != admitted_width:
        return _fallback(
            finish, window, request, admitted_width, "invalid_proposal", "none", observed_ms, target_only_ms, deadline_record,
            draft=draft, tentative=len(proposal.tokens),
        )

    if cancellation.is_cancelled("before_transfer"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, 0, envelope
        )

    transfer_ms = transport.deliver(envelope)

    if cancellation.is_cancelled("before_target_verification"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, 0, envelope
        )

    if target.target_lost():
        runtime_request = _fallback_request(
            request, width=admitted_width, deadline_outcome="none", fallback_reason="none", terminal_state="target_lost_explicit"
        )
        _note_failure(window, deadline_missed=False, speculative_ms=None, target_only_ms=target_only_ms)
        draft.rollback_suffix(len(proposal.tokens))
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            envelope=envelope.public_projection(),
            failure={"failure_reason": "target_lost_explicit", "detail": "target_runtime_unavailable"},
            deadline=deadline_record,
            rolled_back=len(proposal.tokens),
        )

    # -- the A10 verified-token path ---------------------------------------
    target.prepare_candidates(proposal.tokens)
    a10_request_object = A10VerificationRequest(
        request_ref=request,
        target_kv_ref=target.target_kv_ref,
        runtime_incarnation=target.target_runtime_incarnation,
        committed_prefix_position=prefix_position,
        committed_prefix_digest=prefix_digest,
        candidate_position_count=admitted_width,
        candidate_bytes=envelope.byte_count,
        generation_policy=envelope.generation_policy,
        transaction_mode=A10_TRANSACTION_MODES[2],
        deadline_at=admitted_ms,
        cancellation_generation=cancellation.cancellation_generation(),
        idempotency_digest=envelope.idempotency_digest,
    )
    a10_projection = {
        "request_ref": request,
        "target_kv_ref": target.target_kv_ref,
        "committed_prefix_position": prefix_position,
        "committed_prefix_digest": prefix_digest,
        "candidate_position_count": admitted_width,
        "candidate_bytes": envelope.byte_count,
        "generation_policy": envelope.generation_policy,
        "transaction_mode": A10_TRANSACTION_MODES[2],
        "deadline_at": admitted_ms,
        "idempotency_digest": envelope.idempotency_digest,
    }

    try:
        outcome = a10_verify_candidate_positions(target, a10_request_object)
    except A10ContractError as exc:
        # A verifier that cannot produce one bounded, contiguous target-authoritative
        # result has failed closed: nothing is committed and the overlay is revoked.
        code = getattr(exc, "args", ("a10_verification_refused",))[0]
        runtime_request = _fallback_request(
            request,
            width=admitted_width,
            deadline_outcome="none",
            fallback_reason=FAILURE_FALLBACK_REASONS["a10_verification_refused"],
        )
        _note_failure(window, deadline_missed=False, speculative_ms=observed_ms + transfer_ms, target_only_ms=target_only_ms)
        draft.rollback_suffix(len(proposal.tokens))
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            envelope=envelope.public_projection(),
            a10_request=a10_projection,
            a10_request_object=a10_request_object,
            failure={"failure_reason": "a10_verification_refused", "a10_code": str(code)},
            deadline=deadline_record,
            breaker=sd.circuit_breaker_decision(
                parity_violation=True, cycles=list(window)
            ),
            rolled_back=len(proposal.tokens),
        )

    outcome_record = {
        "request_ref": outcome.request_ref,
        "prefix_position": outcome.prefix_position,
        "proposed_position_count": outcome.proposed_position_count,
        "verified_count": outcome.verified_count,
        "accepted_prefix_count": outcome.accepted_prefix_count,
        "correction_count": outcome.correction_count,
        "committed_position": outcome.committed_position,
        "commit_state": outcome.commit_state,
        "cleanup": outcome.cleanup,
    }
    captured = target.last_commit_snapshot()

    if cancellation.is_cancelled("after_verifier_return"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, admitted_width,
            envelope, a10_projection, outcome_record,
        )

    parity = sd.check_prefix_parity(
        target=target._source,  # noqa: SLF001 - the same target authority, in-process
        prefix_digest=prefix_digest,
        candidates=envelope.tokens,
        decisions=target.verified_decisions(),
    )
    if parity["parity"] != "matched":
        runtime_request = _fallback_request(
            request,
            width=admitted_width,
            deadline_outcome="none",
            fallback_reason=FAILURE_FALLBACK_REASONS["parity_failed"],
            verified=admitted_width,
            rejected=admitted_width,
        )
        _note_failure(window, deadline_missed=False, speculative_ms=observed_ms + transfer_ms, target_only_ms=target_only_ms)
        draft.rollback_suffix(len(proposal.tokens))
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            envelope=envelope.public_projection(),
            a10_request=a10_projection,
            a10_request_object=a10_request_object,
            a10_outcome=outcome_record,
            parity=_parity_record(parity),
            failure={"failure_reason": "parity_failed", "first_divergence": parity["first_divergence"]},
            deadline=deadline_record,
            breaker=sd.circuit_breaker_decision(
                parity_violation=True, cycles=list(window)
            ),
            rolled_back=admitted_width,
        )

    accepted = int(outcome.accepted_prefix_count)
    correction = int(outcome.correction_count)
    verified = int(outcome.verified_count)
    rejected = admitted_width - accepted
    committed_tokens_count = accepted + correction
    if captured is None or captured["position"] != outcome.committed_position:
        runtime_request = _fallback_request(
            request,
            width=admitted_width,
            deadline_outcome="none",
            fallback_reason=FAILURE_FALLBACK_REASONS["invalid_proposal"],
            verified=verified,
            rejected=verified,
        )
        draft.rollback_suffix(len(proposal.tokens))
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            envelope=envelope.public_projection(),
            a10_request=a10_projection,
            a10_request_object=a10_request_object,
            a10_outcome=outcome_record,
            parity=_parity_record(parity),
            failure={"failure_reason": "invalid_proposal", "detail": "verifier_commit_position_unbound"},
            deadline=deadline_record,
            rolled_back=verified,
        )

    if cancellation.is_cancelled("before_target_commit"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, admitted_width,
            envelope, a10_projection, outcome_record, parity,
        )
    if cancellation.is_cancelled("before_output"):
        return _cancelled(
            finish, request, admitted_width, cancellation, deadline_record, window, draft, admitted_width,
            envelope, a10_projection, outcome_record, parity,
        )

    # -- commit arbitration: ledger then target KV, exactly once ------------
    try:
        watermark = ledger.commit(
            cycle_ref=reference,
            idempotency_digest=envelope.idempotency_digest,
            position=captured["position"],
            digest=captured["digest"],
        )
    except sd.SpeculativeDecodeContractError as exc:
        code = getattr(exc, "code", "conflicting_duplicate_cycle")
        target.restore(position=ledger.position, digest=ledger.digest)
        draft.rollback_suffix(len(proposal.tokens))
        runtime_request = _fallback_request(
            request,
            width=admitted_width,
            deadline_outcome="none",
            fallback_reason=FAILURE_FALLBACK_REASONS.get(str(code), "invalid_proposal"),
            verified=verified,
            rejected=verified,
        )
        _note_failure(window, deadline_missed=False, speculative_ms=observed_ms + transfer_ms, target_only_ms=target_only_ms)
        return finish(
            mode="speculative",
            runtime_request=runtime_request,
            envelope=envelope.public_projection(),
            a10_request=a10_projection,
            a10_request_object=a10_request_object,
            a10_outcome=outcome_record,
            parity=_parity_record(parity),
            failure={"failure_reason": str(code), "detail": "fail_closed_to_watermark"},
            deadline=deadline_record,
            rolled_back=verified,
        )

    advanced = bool(watermark["advanced"])
    if advanced:
        target.apply_commit(position=captured["position"], digest=captured["digest"])
        draft.commit_verified_prefix(
            tokens=envelope.tokens[:accepted],
            correction_token=(target.verified_decisions()[accepted] if correction else None),
        )
        if rejected:
            # Draft KV commits only the accepted target-confirmed prefix; the rejected
            # suffix is rolled back explicitly (the correction position is a
            # target-owned token, not a draft position).
            draft.rollback_suffix(rejected)
        released = committed_tokens_count
        already_released = False
        rolled_back = rejected
    else:
        if (
            target.committed_prefix_position() != watermark["target_watermark_position"]
            or target.committed_prefix_digest() != watermark["target_watermark_digest"]
        ):
            target.restore(position=ledger.position, digest=ledger.digest)
            draft.rollback_suffix(len(proposal.tokens))
            runtime_request = _fallback_request(
                request,
                width=admitted_width,
                deadline_outcome="none",
                fallback_reason=FAILURE_FALLBACK_REASONS["idempotent_commit_position_mismatch"],
                verified=verified,
                rejected=verified,
            )
            return finish(
                mode="speculative",
                runtime_request=runtime_request,
                envelope=envelope.public_projection(),
                a10_request=a10_projection,
                a10_request_object=a10_request_object,
                a10_outcome=outcome_record,
                parity=_parity_record(parity),
                failure={"failure_reason": "idempotent_commit_position_mismatch"},
                deadline=deadline_record,
                rolled_back=verified,
            )
        released = 0
        already_released = True
        rolled_back = 0
        draft.commit_verified_prefix(
            tokens=envelope.tokens[:accepted],
            correction_token=(target.verified_decisions()[accepted] if correction else None),
        )

    commit_record = {
        "advanced": advanced,
        "idempotent_duplicate": bool(watermark["idempotent_duplicate"]),
        "watermark_position": int(watermark["target_watermark_position"]),
        "watermark_digest": watermark["target_watermark_digest"],
        "idempotency_digest": envelope.idempotency_digest,
        "ledger_generation": int(watermark["generation"]),
    }
    _note_failure(
        window,
        deadline_missed=False,
        speculative_ms=observed_ms + transfer_ms,
        target_only_ms=target_only_ms,
    )
    breaker = sd.circuit_breaker_decision(cycles=list(window))
    runtime_request = _runtime_request(
        request_id=request,
        mode="speculative_admitted",
        proposal_width=admitted_width,
        proposed=admitted_width,
        verified=verified,
        accepted=accepted if advanced else 0,
        rejected=(rejected if advanced else verified),
        corrected=correction if advanced else 0,
        rollback=rolled_back if advanced else verified,
        committed=committed_tokens_count if advanced else 0,
        deadline_outcome="proposal_arrived",
        fallback_reason="none",
        cancelled=False,
        circuit_breaker_state=breaker["state"],
        terminal_state="completed",
        draft_queue_ms=proposal.draft_queue_ms,
        draft_compute_ms=proposal.draft_compute_ms,
        transfer_ms=transfer_ms,
    )
    return finish(
        mode="speculative",
        runtime_request=runtime_request,
        envelope=envelope.public_projection(),
        a10_request=a10_projection,
        a10_request_object=a10_request_object,
        a10_outcome=outcome_record,
        parity=_parity_record(parity),
        commit=commit_record,
        deadline=deadline_record,
        breaker=breaker,
        released=released,
        already_released=already_released,
        rolled_back=rolled_back,
    )


def _parity_record(parity: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "parity": parity["parity"],
        "positions_compared": parity["positions_compared"],
        "first_divergence": parity["first_divergence"],
        "sequential_calls": parity["sequential_calls"],
    }


def _note_failure(
    window: list[dict[str, Any]],
    *,
    deadline_missed: bool,
    speculative_ms: float | None,
    target_only_ms: float | None,
) -> None:
    window.append(
        {
            "deadline_missed": bool(deadline_missed),
            "speculative_cycle_ms": speculative_ms,
            "equivalent_target_only_ms": target_only_ms,
        }
    )


def _fallback_request(
    request: str,
    *,
    width: int,
    deadline_outcome: str,
    fallback_reason: str,
    verified: int = 0,
    rejected: int = 0,
    terminal_state: str = "completed",
) -> dict[str, Any]:
    return _runtime_request(
        request_id=request,
        mode="speculative_requested",
        proposal_width=width,
        proposed=verified,
        verified=verified,
        accepted=0,
        rejected=rejected,
        corrected=0,
        rollback=rejected,
        committed=0,
        deadline_outcome=deadline_outcome,
        fallback_reason=fallback_reason,
        cancelled=False,
        circuit_breaker_state="closed",
        terminal_state=terminal_state,
    )


def _fallback(
    finish: Any,
    window: list[dict[str, Any]],
    request: str,
    width: int,
    failure_reason: str,
    deadline_outcome: str,
    observed_ms: float,
    target_only_ms: float | None,
    deadline_record: dict[str, Any],
    draft: Any = None,
    tentative: int = 0,
) -> CycleExecution:
    if draft is not None and tentative:
        # Draft KV advances tentatively while proposing; a rejected proposal must
        # release that tentative state.  Target KV is untouched either way.
        draft.rollback_suffix(tentative)
    fallback_reason = FAILURE_FALLBACK_REASONS[failure_reason]
    runtime_request = _fallback_request(
        request, width=width, deadline_outcome=deadline_outcome, fallback_reason=fallback_reason
    )
    _note_failure(
        window,
        deadline_missed=deadline_outcome == "proposal_deadline_missed",
        speculative_ms=None if deadline_outcome == "proposal_deadline_missed" else (observed_ms or None),
        target_only_ms=target_only_ms,
    )
    return finish(
        mode="speculative",
        runtime_request=runtime_request,
        failure={"failure_reason": failure_reason, "detail": "target_only_fallback"},
        deadline=deadline_record,
        breaker=sd.circuit_breaker_decision(cycles=list(window)),
    )


def _cancelled(
    finish: Any,
    request: str,
    width: int,
    cancellation: CancellationScope,
    deadline_record: dict[str, Any],
    window: list[dict[str, Any]],
    draft: Any,
    verified: int,
    envelope: ProposalEnvelope | None = None,
    a10_request: dict[str, Any] | None = None,
    a10_outcome: dict[str, Any] | None = None,
    parity: dict[str, Any] | None = None,
) -> CycleExecution:
    # A4 owns exactly-once cleanup: the whole tentative draft suffix is released and
    # nothing target-authoritative was committed, so the target watermark is untouched.
    tentative = int(draft.snapshot()["tentative_tokens"])
    if tentative:
        draft.rollback_suffix(tentative)
    runtime_request = _runtime_request(
        request_id=request,
        mode="speculative_requested",
        proposal_width=width,
        proposed=verified,
        verified=verified,
        accepted=0,
        rejected=verified,
        corrected=0,
        rollback=verified,
        committed=0,
        deadline_outcome="none",
        fallback_reason="none",
        cancelled=True,
        circuit_breaker_state="closed",
        terminal_state="cancelled",
    )
    snapshot = cancellation.snapshot()
    return finish(
        mode="speculative",
        runtime_request=runtime_request,
        envelope=None if envelope is None else envelope.public_projection(),
        a10_request=a10_request,
        a10_outcome=a10_outcome,
        parity=parity,
        failure={"failure_reason": "cancelled", "checkpoint": snapshot["cancelled_at"]},
        deadline=deadline_record,
        rolled_back=verified,
    )


# ---------------------------------------------------------------------------
# Honest claim boundaries: physical hardware, live route, private network, browser
# ---------------------------------------------------------------------------

_UNAVAILABLE_NOTE = (
    "A11 source tranche: this evidence class needs physical hardware, a live product "
    "route, the authenticated private network path or a browser session; no seam "
    "returns a document for it and none silently succeeds"
)


def _unavailable(code: str, **context: Any) -> NoReturn:
    detail = _UNAVAILABLE_NOTE
    if context:
        detail = f"{detail} ({', '.join(sorted(context))})"
    raise SpeculativeDecodeExecutionUnavailable(code, detail)


def _refuse(code: str, detail: str | None = None) -> NoReturn:
    """Raise a registered seam refusal.  An unregistered code is itself a bug."""

    if code not in SEAM_REFUSALS:  # pragma: no cover - a programming error, not a path
        raise SpeculativeDecodeExecutionError(
            "injection_type_mismatch", f"unregistered_seam_refusal:{code}"
        )
    raise SpeculativeDecodeExecutionError(code, detail)


def require_physical_draft_target_route(**context: Any) -> NoReturn:
    """Boundary: physical draft/target-route evidence needs qualified hardware."""

    _unavailable("physical_draft_target_route_not_available", **context)


def require_live_route_product_session(**context: Any) -> NoReturn:
    """Boundary: the ordinary same-route paired product session needs a live route."""

    _unavailable("live_route_product_session_not_available", **context)


def require_authenticated_network_proposal_transfer(**context: Any) -> NoReturn:
    """Boundary: an authenticated network proposal transfer needs the private path."""

    _unavailable("authenticated_network_proposal_transfer_not_available", **context)


def require_browser_workspace_surface(**context: Any) -> NoReturn:
    """Boundary: the eight live browser workspaces need the product surface."""

    _unavailable("browser_workspace_surface_not_available", **context)


# ---------------------------------------------------------------------------
# The implemented seams: deterministic in-process comparison, gates, projection
# ---------------------------------------------------------------------------


def _evidence_class(evidence_class: Any, *, seam: str) -> str:
    """Return the single supported evidence class, or refuse the requested one."""

    if evidence_class == EMITTED_EVIDENCE_CLASS:
        return EMITTED_EVIDENCE_CLASS
    if isinstance(evidence_class, str) and evidence_class in REFUSED_EVIDENCE_CLASSES:
        _unavailable(REFUSED_EVIDENCE_CLASSES[evidence_class], seam=seam, requested=evidence_class)
    _refuse("invalid_evidence_class", f"{seam}:{evidence_class!r}")


@dataclass(frozen=True)
class InProcessExecution:
    """The injected dependencies one implemented seam runs over.

    ``transport``, ``target`` and ``cancellation`` are the three injections the A11
    cycle requires.  ``draft`` is the *separate* draft authority: it is never derived
    from the target, because a seam that invented its own proposal could "verify" a
    proposal no draft ever made.
    """

    transport: ProposalTransport
    target: A10TargetKvRuntime
    cancellation: CancellationScope
    draft: Any
    ledger: Any = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport": self.transport.snapshot(),
            "target": self.target.snapshot(),
            "draft": self.draft.snapshot(),
        }


def injected_in_process_execution(
    *,
    transport: Any = None,
    decision_source: Any = None,
    cancellation: Any = None,
    draft: Any = None,
    draft_tokens: Any = None,
    ledger: Any = None,
    seam: str = "seam",
    target_kv_ref: str = "target-kv-1",
    target_runtime_incarnation: str = "sha256:" + "3" * 64,
    draft_incarnation: str = "sha256:" + "6" * 64,
    draft_kv_ref: str = "draft-kv-1",
    draft_timing_ms: tuple[float, float] = (1.0, 2.0),
) -> InProcessExecution:
    """Build the in-process execution the seams run over, or refuse by code.

    ``decision_source`` is either an object exposing ``evaluate_bounded_positions``
    (wrapped in this module's :class:`A10TargetKvRuntime`) or an already-built
    ``A10TargetKvRuntime``.  ``draft`` is a ``DraftRuntimePort``; ``draft_tokens`` is
    a ``(position, width) -> tokens`` callable or a plain token sequence used as the
    deterministic proposal.
    """

    if transport is None:
        _refuse(f"{seam}_requires_injected_transport")
    if not isinstance(transport, ProposalTransport):
        _refuse("injection_type_mismatch", "transport")
    if cancellation is None:
        _refuse(f"{seam}_requires_injected_cancellation_scope")
    if not isinstance(cancellation, CancellationScope):
        _refuse("injection_type_mismatch", "cancellation")
    if decision_source is None:
        _refuse(f"{seam}_requires_injected_target_decision_source")
    if isinstance(decision_source, A10TargetKvRuntime):
        target = decision_source
    elif callable(getattr(decision_source, "evaluate_bounded_positions", None)):
        target = A10TargetKvRuntime(
            decision_source,
            target_kv_ref=_text(target_kv_ref, "invalid_target_kv_ref"),
            target_runtime_incarnation=target_runtime_incarnation,
        )
    else:
        _refuse("injection_type_mismatch", "target_decision_source")
    if draft is None:
        if draft_tokens is None:
            _refuse(f"{seam}_requires_injected_draft_runtime")
        if callable(draft_tokens):
            propose = draft_tokens
        elif isinstance(draft_tokens, Sequence) and not isinstance(draft_tokens, (str, bytes)):
            fixed = _token_sequence(draft_tokens, "invalid_draft_proposal")
            propose = lambda _position, _width, _tokens=fixed: _tokens
        else:
            _refuse("injection_type_mismatch", "draft_tokens")
        draft = DeterministicDraftRuntime(
            propose,
            incarnation=draft_incarnation,
            kv_ref=draft_kv_ref,
            timing_ms=draft_timing_ms,
        )
    return InProcessExecution(
        transport=transport, target=target, cancellation=cancellation, draft=draft, ledger=ledger
    )


def _seam_width(document: Mapping[str, Any], width: Any) -> int:
    maximum = min(
        int(document["proposal_bounds"]["maximum_width"]),
        int(document["verifier"]["qualified_width"]),
    )
    if width is None:
        return maximum
    admitted = _position(width, "proposal_width_out_of_bounds")
    if admitted < 1 or admitted > maximum:
        raise SpeculativeDecodeExecutionError("proposal_width_out_of_bounds", f"width={admitted}")
    return admitted


def _request_ids(request_ids: Any) -> tuple[str, ...]:
    if isinstance(request_ids, (str, bytes)) or not isinstance(request_ids, Sequence) or not len(request_ids):
        _refuse("invalid_request_id", repr(request_ids)[:64])
    return tuple(_text(item, "invalid_request_id") for item in request_ids)


def _run_seam_cycle(
    *,
    execution: InProcessExecution,
    document: Mapping[str, Any],
    request_id: str,
    cycle_ref: str,
    width: int,
    preference: str,
    window: list[dict[str, Any]],
    deadline_ms: float | None,
    target_only_ms: float | None,
) -> tuple[CycleExecution, dict[str, Any]]:
    """Run one real cycle and return it with the pre-cycle counters."""

    before = execution.snapshot()
    cycle = run_speculative_cycle(
        plan=document,
        request_id=request_id,
        cycle_ref=cycle_ref,
        attempt=1,
        width=width,
        target=execution.target,
        draft=execution.draft,
        transport=execution.transport,
        cancellation=execution.cancellation,
        preference=preference,
        ledger=execution.ledger,
        deadline_ms=deadline_ms,
        target_only_ms=target_only_ms,
        window=window,
    )
    return cycle, before


def _arm_record(
    cycle: CycleExecution,
    *,
    arm: str,
    before: Mapping[str, Any],
) -> dict[str, Any]:
    """One arm's record: measured counters and positions only, never a token."""

    request = cycle.runtime_request
    target_before = before["target"]
    draft_before = before["draft"]
    transport_before = before["transport"]
    return {
        "arm": arm,
        "request_id": cycle.request_id,
        "cycle_ref": cycle.cycle_ref,
        "mode": cycle.mode,
        "admitted_mode": cycle.admission["mode"],
        "proposal_width": cycle.proposal_width,
        "verification_calls": int(cycle.target_kv["bounded_invocations"])
        - int(target_before["bounded_invocations"]),
        "target_commits": int(cycle.target_kv["commits"]) - int(target_before["commits"]),
        "target_watermark_position": int(cycle.target_kv["position"]),
        "draft_proposals": int(cycle.draft_kv["proposals"]) - int(draft_before["proposals"]),
        "proposal_bytes": _transport_bytes(cycle),
        "proposed_positions": int(request["proposed_count"]),
        "verified_positions": int(request["verified_count"]),
        "accepted_positions": int(request["accepted_count"]),
        "rejected_positions": int(request["rejected_count"]),
        "corrected_positions": int(request["target_corrected_count"]),
        "rollback_positions": int(request["rollback_count"]),
        "committed_positions": int(request["committed_count"]),
        "deadline_outcome": request["deadline_outcome"],
        "fallback_reason": request["fallback_reason"],
        "terminal_state": request["terminal_state"],
        "parity": None if cycle.parity is None else cycle.parity["parity"],
        "failure_reason": None if cycle.failure is None else cycle.failure["failure_reason"],
        "released_tokens": int(cycle.output["released_tokens"]),
        "rolled_back_positions": int(cycle.target_kv["rolled_back_positions"]),
        "open_transactions": int(cycle.target_kv["open_transactions"]),
    }


def _transport_bytes(cycle: CycleExecution) -> int:
    """Bytes this cycle moved, read from the envelope it actually transferred.

    A cycle that never built an envelope moved nothing, and reports zero.
    """

    envelope = cycle.envelope
    if envelope is None:
        return 0
    return int(envelope["byte_count"])


# ---------------------------------------------------------------------------
# Seam 1: the paired target-only / speculative comparison, in process
# ---------------------------------------------------------------------------


def run_live_route_benchmark(
    *,
    plan: Mapping[str, Any],
    target: Any = None,
    transport: Any = None,
    cancellation: Any = None,
    draft: Any = None,
    draft_tokens: Any = None,
    request_ids: Sequence[str] = ("req-1",),
    width: int | None = None,
    deadline_ms: float | None = None,
    target_only_ms: float | None = None,
    claim_window: list[dict[str, Any]] | None = None,
    evidence_class: Any = EMITTED_EVIDENCE_CLASS,
) -> dict[str, Any]:
    """The paired target-only / speculative comparison, run **in process**.

    There is no live route here despite the scaffold name.  Both arms drive the real
    :func:`run_speculative_cycle` over the *injected* transport, target decision source
    and cancellation scope, and the document publishes a deterministic counter
    comparison (``verification_calls``, ``committed_positions``, ``proposal_bytes``) --
    never a latency, throughput or benefit measurement.  A caller that asks for
    ``evidence_class="live_route"`` is refused by the claim boundary, and a run in which
    the speculative arm never reached the verifier refuses rather than publishing an
    empty comparison.
    """

    seam = "route_benchmark"
    _evidence_class(evidence_class, seam=seam)
    document = sd.validate_speculative_decode_plan(plan)
    execution = injected_in_process_execution(
        transport=transport,
        decision_source=target,
        cancellation=cancellation,
        draft=draft,
        draft_tokens=draft_tokens,
        seam=seam,
    )
    admitted_width = _seam_width(document, width)
    requests = _request_ids(request_ids)
    window = [] if claim_window is None else claim_window

    pairs: list[dict[str, Any]] = []
    for request in requests:
        arms = []
        for arm, preference in (
            ("target_only", "target_only_requested"),
            ("speculative", "speculative_requested"),
        ):
            cycle, before = _run_seam_cycle(
                execution=execution,
                document=document,
                request_id=request,
                cycle_ref=f"{request}-{arm}",
                width=admitted_width,
                preference=preference,
                window=window,
                deadline_ms=deadline_ms,
                target_only_ms=target_only_ms,
            )
            arms.append(_arm_record(cycle, arm=arm, before=before))
        pairs.append({"request_id": request, "arms": arms})

    totals = {
        "requests": len(pairs),
        "target_only_verification_calls": sum(
            pair["arms"][0]["verification_calls"] for pair in pairs
        ),
        "speculative_verification_calls": sum(
            pair["arms"][1]["verification_calls"] for pair in pairs
        ),
        "target_only_committed_positions": sum(
            pair["arms"][0]["committed_positions"] for pair in pairs
        ),
        "speculative_committed_positions": sum(
            pair["arms"][1]["committed_positions"] for pair in pairs
        ),
        "speculative_rejected_positions": sum(
            pair["arms"][1]["rejected_positions"] for pair in pairs
        ),
        "speculative_corrected_positions": sum(
            pair["arms"][1]["corrected_positions"] for pair in pairs
        ),
        "draft_proposals": sum(pair["arms"][1]["draft_proposals"] for pair in pairs),
        "proposal_bytes": sum(pair["arms"][1]["proposal_bytes"] for pair in pairs),
    }
    if totals["speculative_verification_calls"] < len(pairs):
        # No verified speculative cycle ran: refuse instead of publishing zeros that a
        # reader could mistake for a measured comparison.
        _refuse(
            "route_benchmark_requires_executed_cycles",
            f"requests={len(pairs)} speculative_verifier_calls={totals['speculative_verification_calls']}",
        )

    return {
        "protocol": BENCHMARK_EXECUTION_PROTOCOL,
        "seam": seam,
        "evidence_class": EMITTED_EVIDENCE_CLASS,
        "plan_digest": document["plan_digest"],
        "paired_arms": ["target_only", "speculative"],
        "proposal_width": admitted_width,
        "requests": pairs,
        "totals": totals,
        "deterministic_step_comparison": {
            "target_only_verification_calls": totals["target_only_verification_calls"],
            "speculative_verification_calls": totals["speculative_verification_calls"],
            "speculative_committed_positions": totals["speculative_committed_positions"],
            "modelled_target_only_steps_for_the_same_positions": totals[
                "speculative_committed_positions"
            ],
            "note": (
                "counters only: this is not a latency, throughput or benefit measurement"
            ),
        },
        "wall_clock_latency_measured": False,
        "material_benefit_claim": False,
        "qualification_claim": False,
        "promotion_authorized": False,
        "enabled_decision": False,
        "live_product_session": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }


# ---------------------------------------------------------------------------
# Seam 2: the positive/negative deterministic gates, in process
# ---------------------------------------------------------------------------


def execute_physical_qualification(
    *,
    plan: Mapping[str, Any],
    target: Any = None,
    transport: Any = None,
    cancellation: Any = None,
    draft: Any = None,
    draft_tokens: Any = None,
    negative_target: Any = None,
    negative_draft_tokens: Any = None,
    width: int | None = None,
    deadline_ms: float | None = None,
    claim_window: list[dict[str, Any]] | None = None,
    evidence_class: Any = EMITTED_EVIDENCE_CLASS,
) -> dict[str, Any]:
    """The positive and negative gates, evaluated **in process**.

    No physical hardware, no browser and no qualification are involved.  The positive
    arm must really commit a verified target-authoritative prefix (release > 0); the
    negative arm must be an *injected* second decision source that really fails parity
    and commits nothing.  Both arms are required: a gate with no negative arm would be
    unable to fail, so a missing ``negative_target`` refuses.  The verdict is reported,
    never asserted as qualification.
    """

    seam = "physical_qualification"
    _evidence_class(evidence_class, seam=seam)
    document = sd.validate_speculative_decode_plan(plan)
    if negative_target is None:
        _refuse("physical_qualification_requires_injected_negative_arm", "negative_target")
    admitted_width = _seam_width(document, width)
    window = [] if claim_window is None else claim_window

    positive = injected_in_process_execution(
        transport=transport,
        decision_source=target,
        cancellation=cancellation,
        draft=draft,
        draft_tokens=draft_tokens,
        seam=seam,
    )
    negative = injected_in_process_execution(
        transport=transport,
        decision_source=negative_target,
        cancellation=cancellation,
        draft=draft,
        draft_tokens=draft_tokens if negative_draft_tokens is None else negative_draft_tokens,
        seam=seam,
    )

    positive_cycle, positive_before = _run_seam_cycle(
        execution=positive,
        document=document,
        request_id="qualification-positive",
        cycle_ref="qualification-positive",
        width=admitted_width,
        preference="speculative_requested",
        window=window,
        deadline_ms=deadline_ms,
        target_only_ms=None,
    )
    negative_cycle, negative_before = _run_seam_cycle(
        execution=negative,
        document=document,
        request_id="qualification-negative",
        cycle_ref="qualification-negative",
        width=admitted_width,
        preference="speculative_requested",
        window=[],
        deadline_ms=deadline_ms,
        target_only_ms=None,
    )

    positive_met = bool(
        positive_cycle.mode == "speculative"
        and positive_cycle.commit is not None
        and positive_cycle.commit["advanced"]
        # The strict positive: every admitted position was verified and committed, so
        # the gate cannot be met by a target correction alone (acceptance zero).
        and int(positive_cycle.runtime_request["accepted_count"]) == admitted_width
        and int(positive_cycle.runtime_request["committed_count"]) == admitted_width
        and int(positive_cycle.output["released_tokens"]) == admitted_width
        and positive_cycle.parity is not None
        and positive_cycle.parity["parity"] == "matched"
    )
    negative_met = bool(
        negative_cycle.mode == "speculative"
        and negative_cycle.failure is not None
        and negative_cycle.failure["failure_reason"]
        in {"parity_failed", "a10_verification_refused"}
        and int(negative_cycle.runtime_request["committed_count"]) == 0
        and int(negative_cycle.output["released_tokens"]) == 0
        and int(negative_cycle.runtime_request["rollback_count"]) > 0
        and int(negative_cycle.target_kv["position"]) == int(negative_before["target"]["position"])
    )
    unmet = [
        name
        for name, met in (("positive", positive_met), ("negative", negative_met))
        if not met
    ]

    return {
        "protocol": QUALIFICATION_EXECUTION_PROTOCOL,
        "seam": seam,
        "evidence_class": EMITTED_EVIDENCE_CLASS,
        "plan_digest": document["plan_digest"],
        "proposal_width": admitted_width,
        "result": "deterministic_gates_met" if not unmet else "gate_not_met",
        "positive_gate": positive_met,
        "negative_gate": negative_met,
        "unmet": unmet,
        "arms": {
            "positive": _arm_record(positive_cycle, arm="positive", before=positive_before),
            "negative": _arm_record(negative_cycle, arm="negative", before=negative_before),
        },
        "blocked_on": sorted(EXECUTION_BOUNDARIES),
        "hardware_touched": False,
        "browser_touched": False,
        "physical_claim": False,
        "qualification_claim": False,
        "promotion_authorized": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }


# ---------------------------------------------------------------------------
# Seam 3: the eight-workspace projection, built from an in-process cycle
# ---------------------------------------------------------------------------


def _projection_privacy_scan(value: Any, *, path: str = "") -> None:
    """Refuse any projection that carries private material or a forbidden key."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key) in _FORBIDDEN_PROJECTION_KEYS:
                _refuse("projection_privacy_violation", f"{path}.{key}")
            _projection_privacy_scan(item, path=f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _projection_privacy_scan(item, path=f"{path}[{index}]")


def project_live_workspace_state(
    *,
    plan: Mapping[str, Any],
    target: Any = None,
    transport: Any = None,
    cancellation: Any = None,
    draft: Any = None,
    draft_tokens: Any = None,
    width: int | None = None,
    deadline_ms: float | None = None,
    claim_window: list[dict[str, Any]] | None = None,
    evidence_class: Any = EMITTED_EVIDENCE_CLASS,
) -> dict[str, Any]:
    """Project the eight product workspaces from one **in-process** cycle.

    No browser is opened and no product surface is driven: the projection is built from
    the frozen runtime record and the cycle's own counters, positions and digests, in
    the order ``ui/web/src/app/navigation.ts`` publishes (a test binds the two).  Every
    workspace therefore reports what the deterministic execution actually produced, with
    ``live_route``, ``browser_rendered`` and ``product_surface_claim`` all false.
    """

    seam = "workspace_projection"
    _evidence_class(evidence_class, seam=seam)
    document = sd.validate_speculative_decode_plan(plan)
    execution = injected_in_process_execution(
        transport=transport,
        decision_source=target,
        cancellation=cancellation,
        draft=draft,
        draft_tokens=draft_tokens,
        seam=seam,
    )
    admitted_width = _seam_width(document, width)
    window = [] if claim_window is None else claim_window
    cycle, before = _run_seam_cycle(
        execution=execution,
        document=document,
        request_id="workspace-projection",
        cycle_ref="workspace-projection",
        width=admitted_width,
        preference="speculative_requested",
        window=window,
        deadline_ms=deadline_ms,
        target_only_ms=None,
    )

    arm = _arm_record(cycle, arm="projection", before=before)
    commit = cycle.commit or {}
    request = cycle.runtime_request
    admitted_mode = cycle.admission["mode"]
    workspaces = {
        "inference": {
            "admitted_mode": admitted_mode,
            "mode": cycle.mode,
            "proposal_width": cycle.proposal_width,
            "proposed_positions": arm["proposed_positions"],
            "verified_positions": arm["verified_positions"],
            "accepted_positions": arm["accepted_positions"],
            "rejected_positions": arm["rejected_positions"],
            "rollback_positions": arm["rollback_positions"],
            "committed_positions": arm["committed_positions"],
            "deadline_outcome": request["deadline_outcome"],
            "fallback_reason": request["fallback_reason"],
            "terminal_state": request["terminal_state"],
            "cancelled": bool(request["cancelled"]),
        },
        "lab": {
            "draft_role_qualified": False,
            "synthetic_workers_qualify": False,
            "maximum_proposal_width": int(document["proposal_bounds"]["maximum_width"]),
            "qualified_verifier_width": int(document["verifier"]["qualified_width"]),
            "verifier_status": document["verifier"]["status"],
            "lower_precision_qualified": False,
            "platform": "unknown",
        },
        "network": {
            "proposal_transfer": execution.transport.snapshot()["transport"],
            "proposal_bytes": arm["proposal_bytes"],
            "authenticated_network_transport": False,
            "direct_or_relay_path": "unknown",
            "target_verification_spans": arm["verification_calls"],
        },
        "nodes": {
            "target_kv_ref": cycle.target_kv["kv_ref"],
            "draft_kv_ref": cycle.draft_kv["kv_ref"],
            "target_runtime_incarnation": cycle.target_kv["target_runtime_incarnation"],
            "draft_runtime_incarnation": cycle.draft_kv["draft_runtime_incarnation"],
            "target_owns_target_state": True,
            "draft_wrote_target_state": bool(cycle.draft_kv["wrote_target_state"]),
            "kv_owner_separate": bool(
                cycle.target_kv["kv_ref"] != cycle.draft_kv["kv_ref"]
                and not cycle.target_kv["aliased_with_draft"]
            ),
            "open_transactions": arm["open_transactions"],
            "cleanup": cycle.target_kv["cleanup"],
        },
        "plans": {
            "plan_digest": document["plan_digest"],
            "generation_policy": document["compatibility"]["generation_policy"],
            "proposal_width": admitted_width,
            "material_gain_threshold": document["decision"]["material_gain_threshold"],
            "decision_state": document["decision"]["state"],
            "target_only_fallback": bool(document["decision"]["target_only_fallback"]),
        },
        "readiness": {
            "parity": arm["parity"],
            "rollback_verified": bool(arm["rollback_positions"] == arm["rejected_positions"]),
            "cleanup_complete": bool(request["cleanup_complete"]),
            "circuit_breaker_state": request["circuit_breaker_state"],
            "closure_requirement_earned": False,
            "physical_qualification_claim": False,
            "promotion_authorized": False,
        },
        "incidents": {
            "failure_reason": arm["failure_reason"],
            "fallback_reason": request["fallback_reason"],
            "cancelled": bool(request["cancelled"]),
            "circuit_breaker_state": request["circuit_breaker_state"],
            "target_lost": bool(cycle.target_kv["target_lost"]),
            "terminal_state": request["terminal_state"],
        },
        "settings": {
            "target_only_default": True,
            "acceleration_off_by_default": True,
            "applies_to": "future_requests_only",
            "bounded_width": admitted_width,
            "maximum_deadline_ms": float(sd.DEADLINE_MAXIMUM_MS),
        },
    }
    if set(workspaces) != set(PRODUCT_WORKSPACES):
        _refuse("projection_privacy_violation", "workspace_set")
    _projection_privacy_scan(workspaces)

    projection = {
        "protocol": WORKSPACE_PROJECTION_PROTOCOL,
        "seam": seam,
        "evidence_class": EMITTED_EVIDENCE_CLASS,
        "plan_digest": document["plan_digest"],
        "generation": int(commit.get("ledger_generation", 0)),
        "workspaces": workspaces,
        "workspace_order": list(PRODUCT_WORKSPACES),
        "runtime": cycle.runtime_document,
        "cycle": {
            "request_id": cycle.request_id,
            "cycle_ref": cycle.cycle_ref,
            "mode": cycle.mode,
            "commit_advanced": bool(commit.get("advanced", False)),
            "target_watermark_position": int(cycle.target_kv["position"]),
            "released_tokens": int(cycle.output["released_tokens"]),
            "a10_transaction_mode": None
            if cycle.a10_request is None
            else cycle.a10_request["transaction_mode"],
            "a10_verified_positions": None if cycle.a10_outcome is None else int(cycle.a10_outcome["verified_count"]),
        },
        "live_product_session": False,
        "browser_rendered": False,
        "product_surface_claim": False,
        "qualification_claim": False,
        "promotion_authorized": False,
        "claim_boundary": CLAIM_BOUNDARY,
    }
    _projection_privacy_scan(projection)
    return projection


__all__ = [
    "A10_WIRING",
    "ABSTAIN_REASONS",
    "A10TargetKvRuntime",
    "BENCHMARK_EXECUTION_PROTOCOL",
    "CANCELLATION_CHECKPOINTS",
    "CLAIM_BOUNDARY",
    "CycleExecution",
    "DeterministicDraftRuntime",
    "DraftProposal",
    "DraftRuntimePort",
    "EMITTED_EVIDENCE_CLASS",
    "EXECUTION_BOUNDARIES",
    "EXECUTION_STAGE",
    "FAILURE_FALLBACK_REASONS",
    "INJECTION_REFUSALS",
    "InProcessExecution",
    "LIVE_SEAMS",
    "PRODUCT_WORKSPACES",
    "PROPOSAL_BYTES_PER_TOKEN",
    "PROTOCOL",
    "ProposalEnvelope",
    "ProposalTransport",
    "QUALIFICATION_EXECUTION_PROTOCOL",
    "ROUTER_DECODE_SEAM_KEY",
    "CancellationScope",
    "REFUSED_EVIDENCE_CLASSES",
    "SEAM_REFUSALS",
    "SpeculativeDecodeExecutionError",
    "SpeculativeDecodeExecutionUnavailable",
    "WORKSPACE_PROJECTION_PROTOCOL",
    "build_proposal_envelope",
    "build_runtime_document",
    "execute_physical_qualification",
    "injected_in_process_execution",
    "project_live_workspace_state",
    "require_authenticated_network_proposal_transfer",
    "require_browser_workspace_surface",
    "require_live_route_product_session",
    "require_physical_draft_target_route",
    "run_live_route_benchmark",
    "run_speculative_cycle",
    "runtime_binding_from_plan",
]
