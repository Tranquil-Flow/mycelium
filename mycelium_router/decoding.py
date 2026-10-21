# SPDX-License-Identifier: AGPL-3.0-only
"""Framework-neutral greedy token selection.

Floating-point kernels can differ by a few ULPs across otherwise equivalent
backends. Raw argmax makes near-tied logits choose different tokens, after which
autoregressive decode diverges completely. Quantizing before argmax freezes one
portable policy while preserving ordinary lowest-token tie breaking.

This module is also the router decode path's single speculative-execution seam.
:func:`resolve_speculative_decode` dispatches one decode cycle to the authoritative
A11 execution (``mycelium_speculative_decode_execution``, the implemented cycle wired
to the A10 verified-token path), and falls back to the merged
``mycelium_speculative_decode`` helper's admission decision when the authoritative
in-process execution cannot be driven from the caller's injected dependencies.  Both
imports are lazy: this module stays a light, dependency-free decode contract for the
runtime adapters, and no adapter import changes behaviour.

Claim boundary: the seam dispatches deterministic in-process work only.  It opens no
live route, moves no proposal over an authenticated network path, drives no browser
and makes no physical, qualification, benefit or promotion claim.
"""

from __future__ import annotations

import importlib
import math
from collections.abc import Iterable

LOGIT_QUANTUM = 1e-5

#: The router decode seam's protocol tag.
SPECULATIVE_DECODE_SEAM_PROTOCOL = "mycelium.router_decode_seam.v1"

#: The seam's own name, so a dispatch record says where it came from.
ROUTER_DECODE_SEAM = "mycelium_router.decoding.resolve_speculative_decode"

#: The authoritative execution this decode path dispatches to, and the merged helper
#: that stays the fallback.  Named here so a test (and a reviewer) can see the dispatch
#: without reading the lazy imports.
AUTHORITATIVE_EXECUTION_MODULE = "mycelium_speculative_decode_execution"
AUTHORITATIVE_EXECUTION_FUNCTION = "run_speculative_cycle"
FALLBACK_HELPER_MODULE = "mycelium_speculative_decode"
FALLBACK_HELPER_FUNCTION = "integrate_request_admission"

#: Dispatch authorities a record may report.
TARGET_ONLY_AUTHORITY = "target_only"
AUTHORITATIVE_AUTHORITY = "a11_authoritative_execution"
FALLBACK_AUTHORITY = "wired_helper"

#: Closed dispatch reasons.  A reason outside this set is a bug, not a state.
SEAM_DISPATCH_REASONS = frozenset(
    {
        "no_bound_speculative_plan",
        "authoritative_execution_unavailable",
        "injected_execution_incomplete",
        "wired_helper_unavailable",
    }
)

#: The request label used when a caller binds a plan without naming a request.  The
#: record reports ``request_id_defaulted`` so the substitution is never invisible.
DEFAULT_REQUEST_ID = "router-decode-request"

SEAM_CLAIM_BOUNDARY = (
    "router decode seam: deterministic in-process dispatch only.  No live route, no "
    "authenticated network proposal transfer, no browser or product surface, no "
    "physical qualification, no benefit and no promotion claim."
)


class GreedySelectionError(ValueError):
    """Raised when logits cannot be decoded under the portable contract."""


def quantized_greedy_token_id(
    logits: Iterable[float],
    *,
    quantum: float = LOGIT_QUANTUM,
) -> int:
    """Return lowest token ID in the highest quantized logit bucket.

    The function deliberately uses only Python scalars. Runtime adapters must
    materialize their final logit row before calling it, keeping this contract
    independent of NumPy, MLX, device type, and architecture.
    """

    if (
        not isinstance(quantum, (int, float))
        or isinstance(quantum, bool)
        or not math.isfinite(float(quantum))
        or float(quantum) <= 0.0
    ):
        raise GreedySelectionError("invalid_logit_quantum")
    try:
        values = tuple(logits)
    except TypeError as exc:
        raise GreedySelectionError("invalid_logits") from exc
    if not values:
        raise GreedySelectionError("empty_logits")
    normalized: list[float] = []
    for value in values:
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
        ):
            raise GreedySelectionError("invalid_logit")
        normalized.append(float(value))
    buckets = [round(value / float(quantum)) for value in normalized]
    return max(range(len(buckets)), key=buckets.__getitem__)


def quantized_greedy_token_ids(
   rows: object,
   *,
   quantum: float = LOGIT_QUANTUM,
) -> tuple[int, ...]:
   """:func:`quantized_greedy_token_id` for every row of a 2-D logit array, at once.

   Exactly the scalar rule per row: each logit is widened to a binary64 value
   (``float32``/``float16``/``bfloat16`` widen exactly), divided by ``quantum`` in
   binary64, rounded half-to-even (``numpy.rint`` is Python's ``round`` on a
   binary64), and the lowest index of the highest bucket wins (``numpy.argmax``
   returns the first maximum).  It exists because the scalar rule materialises a
   Python list per position, which costs more than the stage forward on a large
   vocabulary; multi-position verification reads several positions per call.
   """

   import numpy as np

   if (
      not isinstance(quantum, (int, float))
      or isinstance(quantum, bool)
      or not math.isfinite(float(quantum))
      or float(quantum) <= 0.0
   ):
      raise GreedySelectionError("invalid_logit_quantum")
   if hasattr(rows, "astype") and not isinstance(rows, np.ndarray):
      # An MLX array: widen on device first (bfloat16 has no NumPy dtype).
      try:
         import mlx.core as mx

         if isinstance(rows, mx.array):
            rows = rows.astype(mx.float32)
      except ImportError:  # pragma: no cover - MLX-free hosts never pass MLX arrays
         pass
   try:
      array = np.asarray(rows, dtype=np.float64)
   except (TypeError, ValueError) as exc:
      raise GreedySelectionError("invalid_logits") from exc
   if array.ndim != 2 or array.shape[0] == 0:
      raise GreedySelectionError("invalid_logits")
   if array.shape[1] == 0:
      raise GreedySelectionError("empty_logits")
   if not bool(np.isfinite(array).all()):
      raise GreedySelectionError("invalid_logit")
   buckets = np.rint(array / float(quantum))
   if not bool(np.isfinite(buckets).all()):
      raise GreedySelectionError("invalid_logit")
   return tuple(int(index) for index in np.argmax(buckets, axis=1))


# ---------------------------------------------------------------------------
# The router decode seam: one decode cycle, authoritative execution first
# ---------------------------------------------------------------------------


def _load_optional(module_name: str):
    """Import an optional dispatch target, or return None when it is absent."""

    try:
        return importlib.import_module(module_name)
    except ImportError:
        return None


def _seam_record(
    *,
    dispatched: bool,
    authority: str,
    execution_module: str | None,
    reason: str | None,
    request: str,
    request_id_defaulted: bool,
    admission: dict | None,
    fallback_detail: str | None = None,
    extra: dict | None = None,
) -> dict:
    if reason is not None and reason not in SEAM_DISPATCH_REASONS:  # pragma: no cover
        raise ValueError(f"unregistered_dispatch_reason: {reason}")
    record = {
        "protocol": SPECULATIVE_DECODE_SEAM_PROTOCOL,
        "seam": ROUTER_DECODE_SEAM,
        "dispatched": bool(dispatched),
        "authority": authority,
        "execution_module": execution_module,
        "authoritative_module": AUTHORITATIVE_EXECUTION_MODULE,
        "authoritative_function": AUTHORITATIVE_EXECUTION_FUNCTION,
        "fallback_module": FALLBACK_HELPER_MODULE,
        "fallback_function": FALLBACK_HELPER_FUNCTION,
        "reason": reason,
        "fallback_detail": fallback_detail,
        "request_id": request,
        "request_id_defaulted": bool(request_id_defaulted),
        "admission": admission,
        "speculative_available": bool(
            admission is not None and admission.get("mode") == "speculative_admitted"
        ),
        "claim_boundary": SEAM_CLAIM_BOUNDARY,
    }
    if extra:
        record.update(extra)
    return record


def resolve_speculative_decode(
    *,
    plan: object = None,
    request_id: str | None = None,
    preference: str = "speculative_requested",
    cycle_ref: str = "router-decode-cycle-1",
    attempt: int = 1,
    width: int | None = None,
    transport: object = None,
    target: object = None,
    cancellation: object = None,
    draft: object = None,
    draft_tokens: object = None,
    ledger: object = None,
    deadline_ms: float | None = None,
    target_only_ms: float | None = None,
    window: list | None = None,
) -> dict:
    """Dispatch one decode cycle to the authoritative A11 execution.

    The decode path calls this with whatever it holds.  Three honest outcomes:

    * **no bound plan** -- nothing speculative is bound, so the record reports
      ``authority="target_only"`` and dispatches nothing;
    * **authoritative dispatch** -- a plan is bound *and* the three in-process
      injections (``transport``, ``target`` decision source, ``cancellation`` scope,
      plus the ``width`` and a ``draft``/``draft_tokens``) are present, so the real
      ``mycelium_speculative_decode_execution.run_speculative_cycle`` runs here and
      the record carries its execution stage and full cycle document;
    * **fallback** -- the plan is bound but the authoritative execution cannot be
      driven, so the merged ``mycelium_speculative_decode`` helper supplies the
      admission decision (the mode a live request would take) and the record names the
      reason.

    A seam never invents an injection: a missing or malformed one produces the
    fallback record, not a fabricated cycle.  Contract violations raised by the plan
    itself are not swallowed -- they propagate, because a malformed plan is not a
    reason to silently decode.
    """

    request_id_defaulted = not (isinstance(request_id, str) and request_id)
    request: str = DEFAULT_REQUEST_ID if request_id_defaulted else str(request_id)

    if plan is None:
        return _seam_record(
            dispatched=False,
            authority=TARGET_ONLY_AUTHORITY,
            execution_module=None,
            reason="no_bound_speculative_plan",
            request=request,
            request_id_defaulted=request_id_defaulted,
            admission=None,
        )

    execution = _load_optional(AUTHORITATIVE_EXECUTION_MODULE)
    injection_helper = (
        None if execution is None else getattr(execution, "injected_in_process_execution", None)
    )
    cycle_runner = (
        None if execution is None else getattr(execution, AUTHORITATIVE_EXECUTION_FUNCTION, None)
    )
    injection_error = None if execution is None else getattr(execution, "SpeculativeDecodeExecutionError", None)
    injection_refusals = (
        frozenset() if execution is None else getattr(execution, "INJECTION_REFUSALS", frozenset())
    )
    fallback_detail: str | None = None

    if cycle_runner is not None and injection_helper is not None:
        if width is None:
            fallback_detail = "width"
        else:
            try:
                injections = injection_helper(
                    transport=transport,
                    decision_source=target,
                    cancellation=cancellation,
                    draft=draft,
                    draft_tokens=draft_tokens,
                    ledger=ledger,
                    seam=getattr(execution, "ROUTER_DECODE_SEAM_KEY", "router_decode"),
                )
            except Exception as exc:  # noqa: BLE001 - narrowed by class and code below
                code = str(getattr(exc, "code", ""))
                if injection_error is None or not isinstance(exc, injection_error):
                    raise
                if code not in injection_refusals:
                    # Not an injection problem: a malformed plan stays a violation.
                    raise
                fallback_detail = code
            else:
                cycle = cycle_runner(
                    plan=plan,
                    request_id=request,
                    cycle_ref=cycle_ref,
                    attempt=attempt,
                    width=width,
                    target=injections.target,
                    draft=injections.draft,
                    transport=injections.transport,
                    cancellation=injections.cancellation,
                    preference=preference,
                    ledger=injections.ledger,
                    deadline_ms=deadline_ms,
                    target_only_ms=target_only_ms,
                    window=window,
                )
                return _seam_record(
                    dispatched=True,
                    authority=AUTHORITATIVE_AUTHORITY,
                    execution_module=AUTHORITATIVE_EXECUTION_MODULE,
                    reason=None,
                    request=request,
                    request_id_defaulted=request_id_defaulted,
                    admission=cycle.admission,
                    extra={
                        "execution_stage": getattr(execution, "EXECUTION_STAGE", None),
                        "execution_protocol": getattr(execution, "PROTOCOL", None),
                        "cycle": cycle.to_dict(),
                        "claim_boundary": getattr(execution, "CLAIM_BOUNDARY", SEAM_CLAIM_BOUNDARY),
                    },
                )

    if cycle_runner is None or injection_helper is None:
        reason = "authoritative_execution_unavailable"
        detail = None if execution is None else "execution_surface_incomplete"
    else:
        reason = "injected_execution_incomplete"
        detail = fallback_detail

    helper = _load_optional(FALLBACK_HELPER_MODULE)
    admission_function = None if helper is None else getattr(helper, FALLBACK_HELPER_FUNCTION, None)
    if admission_function is None:
        return _seam_record(
            dispatched=False,
            authority=TARGET_ONLY_AUTHORITY,
            execution_module=None,
            reason="wired_helper_unavailable",
            request=request,
            request_id_defaulted=request_id_defaulted,
            admission=None,
            fallback_detail=detail,
        )

    admission = admission_function(plan=plan, request_id=request, preference=preference)
    return _seam_record(
        dispatched=False,
        authority=FALLBACK_AUTHORITY,
        execution_module=FALLBACK_HELPER_MODULE,
        reason=reason,
        request=request,
        request_id_defaulted=request_id_defaulted,
        admission=admission,
        fallback_detail=detail,
    )


__all__ = [
    "AUTHORITATIVE_AUTHORITY",
    "AUTHORITATIVE_EXECUTION_FUNCTION",
    "AUTHORITATIVE_EXECUTION_MODULE",
    "DEFAULT_REQUEST_ID",
    "FALLBACK_AUTHORITY",
    "FALLBACK_HELPER_FUNCTION",
    "FALLBACK_HELPER_MODULE",
    "GreedySelectionError",
    "LOGIT_QUANTUM",
    "ROUTER_DECODE_SEAM",
    "SEAM_CLAIM_BOUNDARY",
    "SEAM_DISPATCH_REASONS",
    "SPECULATIVE_DECODE_SEAM_PROTOCOL",
    "TARGET_ONLY_AUTHORITY",
    "quantized_greedy_token_id",
   "quantized_greedy_token_ids",
    "resolve_speculative_decode",
]
