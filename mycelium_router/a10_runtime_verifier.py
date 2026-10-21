# SPDX-License-Identifier: AGPL-3.0-only
"""A10 target verification on a real stage runtime (MLX or NumPy).

``mycelium_router.a10_verifier_execution`` decides the transaction arithmetic over any
``TargetVerificationRuntimePort``: it needs ``begin_verification()`` and
``sequential_reference()`` (``a10_verifier_execution.py`` ``_run_verification`` and
``sequential_parity_reference``).  This module supplies that port over the loaded
target stages of a real ``MLXRuntimePort`` or ``NumpyRuntimePort``:

- the committed prefix owns real target KV built by the runtime's own stage kernels
  (``_execute_stage_with_kv``) -- the same kernels the incumbent decode path uses;
- ``evaluate_positions`` evaluates every bounded candidate position with causal
  target semantics in ONE forward pass through the ordered target stages (each stage
  is invoked exactly once for the whole width) and returns the target's per-position
  decisions, drawn with the incumbent greedy rule (``quantized_greedy_token_id``);
- commit keeps exactly the committed positions of the tentative KV; rollback restores
  the exact supplied prefix; cleanup releases the tentative KV;
- ``sequential_reference`` replays the target one position at a time from the
  identical committed prefix on an isolated copy of its KV, so parity is judged
  against a real sequential target authority, never against a relabelled call.

KV convention: a committed prefix of ``p`` tokens retains KV for positions
``0..p-2`` and holds token ``p-1`` pending, exactly like incremental decode (the
last emitted token is the next decode input).  A width-``W`` evaluation feeds
``(pending, c_0 .. c_{W-1})`` at positions ``p-1 .. p+W-1``; the target decision
for candidate ``c_i`` is the greedy token after position ``p-1+i``.  Committing
``n`` positions keeps KV for ``p+n-1`` positions and makes the last committed token
pending; a correction token is therefore never fed before it is committed.

Decisions are prefix-acceptance flags: position ``i`` is accepted only when every
earlier position was accepted and ``c_i`` equals the target's decision (a verified
position can never follow a rejected one).

Privacy: candidate token IDs, target decisions, logits and KV arrays stay inside
this object.  The committed-prefix digest is a SHA-256 over the committed token
sequence and retained KV position count; it travels only on the private request.

Claim boundary: an in-process, single-host verifier over locally loaded stages.  A
target split across hosts needs the same one-pass chain over the authenticated
transport, which this module does not provide.  Nothing here advertises a verifier
capability or qualifies one; ``qualify_target_verifier`` decides that from an
executed matrix.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn

from mycelium_router.a10_runtime_batch import A10ContractError
from mycelium_router.a10_target_verification import (
   SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS,
   TRANSACTION_MODES,
)
from mycelium_router.a10_verifier_execution import (
   PositionDecision,
   PositionEvaluation,
   SequentialReference,
)
from mycelium_router.decoding import quantized_greedy_token_id


PROTOCOL = "mycelium.a10_runtime_verifier.v1"
PREFIX_DIGEST_PROTOCOL = "mycelium.a10_committed_prefix.v1"
CLEANUP_RELEASED = "released"


class A10RuntimeVerifierError(A10ContractError):
   """A coded, fail-closed runtime-verifier refusal."""


def _reject(code: str) -> NoReturn:
   raise A10RuntimeVerifierError(code)


def committed_prefix_digest(tokens: Sequence[int], kv_positions: int) -> str:
   """Digest of one committed prefix: its token sequence and retained KV length."""

   document = {
      "protocol": PREFIX_DIGEST_PROTOCOL,
      "tokens": [int(token) for token in tokens],
      "kv_positions": int(kv_positions),
   }
   encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
   return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _token_sequence(value: object, *, code: str, vocab_size: int) -> tuple[int, ...]:
   if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
      _reject(code)
   tokens = tuple(value)
   if any(
      isinstance(token, bool)
      or not isinstance(token, int)
      or token < 0
      or token >= vocab_size
      for token in tokens
   ):
      _reject(code)
   return tokens


class PortVerifierStage:
   """One locally loaded target stage of a real runtime port, as a verifier stage.

   ``forward`` runs the port's own stage kernel under the port's stage-state lock.
   Entry stages take token IDs; the final stage returns the greedy target decision
   for every input position (all positions, not only the last).
   """

   def __init__(
      self,
      port: Any,
      placement_id: str,
      *,
      token_array: Callable[[tuple[int, ...]], Any],
      logits_row: Callable[[Any, int], Sequence[float]],
   ) -> None:
      bound = getattr(port, "_bound", None)
      if not isinstance(bound, Mapping) or placement_id not in bound:
         _reject("verifier_stage_not_local")
      stage, placement, loaded, runtime = bound[placement_id]
      self._port = port
      self._stage = stage
      self._loaded = loaded
      self._runtime = runtime
      self._token_array = token_array
      self._logits_row = logits_row
      self.placement_id = placement_id
      self.stage_id = stage.stage_id
      self.start_layer = stage.layer_range.start_layer
      self.end_layer_exclusive = stage.layer_range.end_layer_exclusive
      self.is_entry = stage.stage_id == port.graph.entry_stage_id
      self.is_final = stage.stage_id == port.graph.final_stage_id
      config = runtime["model_config"]
      self.n_positions = int(config["n_positions"])
      self.vocab_size = int(config["vocab_size"])
      self.backend = str(getattr(port, "backend", placement.runtime_backend))
      self.forward_count = 0

   def forward(
      self,
      inputs: Any,
      *,
      position: int,
      past: Mapping[int, tuple[Any, Any]],
      path_id: str,
   ) -> tuple[Any, dict[int, tuple[Any, Any]]]:
      if getattr(self._port, "_closed", False):
         _reject("runtime_closed")
      if self.is_entry:
         token_ids = self._token_array(tuple(inputs))
         hidden = None
         sequence = len(tuple(inputs))
      else:
         token_ids = None
         hidden = inputs
         sequence = int(inputs.shape[1])
      with self._port._state_lock:
         output, layers = self._port._execute_stage_with_kv(
            stage=self._stage,
            loaded=self._loaded,
            runtime=self._runtime,
            token_ids=token_ids,
            hidden_states=hidden,
            position=position,
            past_layers=past,
            path_id=path_id,
            produce_logits=True,
            all_positions=True,
         )
      self.forward_count += 1
      if not self.is_final:
         return output, layers
      if int(output.shape[1]) != sequence or int(output.shape[2]) != self.vocab_size:
         _reject("invalid_final_stage_output")
      decisions = tuple(
         quantized_greedy_token_id(self._logits_row(output, index))
         for index in range(sequence)
      )
      return decisions, layers

   @staticmethod
   def truncate(layers: Mapping[int, tuple[Any, Any]], length: int) -> dict[int, tuple[Any, Any]]:
      return {
         layer: (key[:, :, :length, :], value[:, :, :length, :])
         for layer, (key, value) in layers.items()
      }

   @staticmethod
   def kv_length(layers: Mapping[int, tuple[Any, Any]]) -> int:
      if not layers:
         return 0
      lengths = {int(key.shape[2]) for key, _ in layers.values()}
      if len(lengths) != 1:
         _reject("inconsistent_stage_kv_length")
      return lengths.pop()


@dataclass(frozen=True)
class _PrefixState:
   tokens: tuple[int, ...]
   layers: tuple[dict[int, tuple[Any, Any]], ...]

   @property
   def position(self) -> int:
      return len(self.tokens)

   @property
   def kv_positions(self) -> int:
      return len(self.tokens) - 1

   def digest(self) -> str:
      return committed_prefix_digest(self.tokens, self.kv_positions)


def _validate_chain(stages: Sequence[Any]) -> tuple[Any, ...]:
   chain = tuple(stages)
   if not chain:
      _reject("verifier_stage_chain_empty")
   if not chain[0].is_entry or not chain[-1].is_final:
      _reject("verifier_stage_chain_incomplete")
   for previous, current in zip(chain, chain[1:]):
      if previous.is_final or current.is_entry:
         _reject("verifier_stage_chain_order_invalid")
      if previous.end_layer_exclusive != current.start_layer:
         _reject("verifier_stage_chain_not_contiguous")
   if len({stage.n_positions for stage in chain}) != 1:
      _reject("verifier_stage_chain_model_mismatch")
   if len({stage.vocab_size for stage in chain}) != 1:
      _reject("verifier_stage_chain_model_mismatch")
   return chain


class _RuntimeVerificationTransaction:
   """One target-owned KV transaction, open until commit/rollback and cleanup."""

   def __init__(self, verifier: "RuntimeTargetVerifier") -> None:
      self._verifier = verifier
      self._base = verifier._state
      self._tentative: tuple[dict[int, tuple[Any, Any]], ...] | None = None
      self._decisions: tuple[int, ...] = ()
      self._accepted = 0
      self._evaluated_width = 0
      self._closed = False

   def committed_prefix_position(self) -> int:
      return self._verifier._state.position

   def committed_prefix_digest(self) -> str:
      return self._verifier._state.digest()

   def evaluate_positions(self, candidate_position_count: int) -> PositionEvaluation:
      if self._closed:
         _reject("verification_transaction_closed")
      if self._tentative is not None:
         _reject("verification_already_evaluated")
      candidates = self._verifier._staged
      width = candidate_position_count
      if (
         isinstance(width, bool)
         or not isinstance(width, int)
         or width < 1
         or width > SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS
      ):
         _reject("invalid_candidate_position_count")
      if len(candidates) != width:
         _reject("staged_candidate_width_mismatch")
      base = self._base
      if base.position + width > self._verifier.n_positions:
         _reject("position_bounds_exceeded")
      inputs: Any = (base.tokens[-1], *candidates)
      start = base.kv_positions
      path_id = self._verifier._path_id
      layers_out: list[dict[int, tuple[Any, Any]]] = []
      forwards_before = [stage.forward_count for stage in self._verifier._stages]
      for stage, past in zip(self._verifier._stages, base.layers):
         inputs, layers = stage.forward(inputs, position=start, past=past, path_id=path_id)
         if stage.kv_length(layers) != start + width + 1:
            _reject("verifier_kv_length_mismatch")
         layers_out.append(layers)
      forwards = [
         stage.forward_count - before
         for stage, before in zip(self._verifier._stages, forwards_before)
      ]
      if any(count != 1 for count in forwards):
         _reject("verification_requires_one_backend_call")
      decisions = tuple(inputs)
      if len(decisions) != width + 1:
         _reject("wrong_verified_position_count")
      accepted = 0
      flags: list[PositionDecision] = []
      for offset, candidate in enumerate(candidates):
         agreed = accepted == offset and candidate == decisions[offset]
         if agreed:
            accepted += 1
         flags.append(PositionDecision(offset=offset, accepted=agreed))
      self._tentative = tuple(layers_out)
      self._decisions = decisions
      self._accepted = accepted
      self._evaluated_width = width
      self._verifier._invocations += 1
      return PositionEvaluation(
         invocation_count=1,
         prefix_position=base.position,
         prefix_digest=base.digest(),
         decisions=tuple(flags),
      )

   def commit_verified_prefix(self, committed_count: int) -> None:
      if self._closed or self._tentative is None:
         _reject("verification_not_evaluated")
      if (
         isinstance(committed_count, bool)
         or not isinstance(committed_count, int)
         or committed_count < 0
         or committed_count > self._evaluated_width
         or committed_count > self._accepted + 1
      ):
         _reject("excess_commit_width")
      candidates = self._verifier._staged
      committed = list(candidates[: min(committed_count, self._accepted)])
      if committed_count > self._accepted:
         # The single correction position: the target's own decision after the last
         # accepted position, drawn from the same backend call.
         committed.append(self._decisions[self._accepted])
      base = self._base
      tokens = base.tokens + tuple(committed)
      keep = len(tokens) - 1
      layers = tuple(
         stage.truncate(stage_layers, keep)
         for stage, stage_layers in zip(self._verifier._stages, self._tentative)
      )
      self._verifier._state = _PrefixState(tokens=tokens, layers=layers)
      self._verifier._commits += 1
      self._tentative = None

   def rollback_to_prefix(self) -> None:
      self._verifier._state = self._base
      self._verifier._rollbacks += 1
      self._tentative = None

   def cleanup(self) -> str:
      if not self._closed:
         self._closed = True
         self._tentative = None
         self._verifier._open_transactions -= 1
         self._verifier._cleanups += 1
      return CLEANUP_RELEASED


class RuntimeTargetVerifier:
   """A ``TargetVerificationRuntimePort`` over the ordered stages of one real target.

   One instance owns one request-local target KV.  ``stage_candidates`` places the
   private candidate token IDs and the transaction mode the next operation will use;
   neither ever enters an A10 request or outcome record.
   """

   def __init__(
      self,
      stages: Sequence[Any],
      *,
      prefix_tokens: Sequence[int],
      verification_ref: str,
   ) -> None:
      self._stages = _validate_chain(stages)
      self.n_positions = self._stages[0].n_positions
      self.vocab_size = self._stages[0].vocab_size
      if not isinstance(verification_ref, str) or not verification_ref.strip():
         _reject("invalid_verification_ref")
      self.verification_ref = verification_ref
      # One runtime incarnation for every verifier over the same loaded stages: the
      # stage identities, not the per-operation reference.
      self.runtime_incarnation = "incarnation:" + hashlib.sha256(
         json.dumps(
            [[stage.backend, stage.placement_id, stage.stage_id, id(getattr(stage, "_port", None))] for stage in self._stages]
         ).encode()
      ).hexdigest()[:32]
      self._path_id = f"a10-verification:{verification_ref}"
      tokens = _token_sequence(
         prefix_tokens, code="invalid_prefix_tokens", vocab_size=self.vocab_size
      )
      if len(tokens) >= self.n_positions:
         _reject("position_bounds_exceeded")
      layers: list[dict[int, tuple[Any, Any]]] = []
      if len(tokens) > 1:
         inputs: Any = tokens[:-1]
         for stage in self._stages:
            inputs, stage_layers = stage.forward(
               inputs, position=0, past={}, path_id=self._path_id
            )
            layers.append(stage_layers)
      else:
         layers = [{} for _ in self._stages]
      self._state = _PrefixState(tokens=tokens, layers=tuple(layers))
      self._staged: tuple[int, ...] = ()
      self._mode = "rollback_to_prefix"
      self._last_base: _PrefixState | None = None
      self._invocations = 0
      self._sequential_invocations = 0
      self._commits = 0
      self._rollbacks = 0
      self._cleanups = 0
      self._open_transactions = 0

   @property
   def path_id(self) -> str:
      """The runtime path this verifier's stage work checkpoints against."""

      return self._path_id

   # -- private staging ----------------------------------------------------
   def stage_candidates(self, tokens: Sequence[int], *, transaction_mode: str) -> int:
      """Stage the private candidate tokens and the mode of the next operation."""

      if transaction_mode not in TRANSACTION_MODES:
         _reject("invalid_transaction_mode")
      candidates = _token_sequence(
         tokens, code="invalid_candidate_tokens", vocab_size=self.vocab_size
      )
      if len(candidates) > SCAFFOLD_MAXIMUM_VERIFICATION_POSITIONS:
         _reject("invalid_candidate_position_count")
      self._staged = candidates
      self._mode = transaction_mode
      return len(candidates)

   def greedy_continuation(self, width: int) -> tuple[int, ...]:
      """The target's own greedy continuation of the committed prefix (isolated).

      Used by drivers to build candidate sets that the target accepts in part; it
      never mutates the committed prefix.
      """

      if isinstance(width, bool) or not isinstance(width, int) or width < 1:
         _reject("invalid_candidate_position_count")
      state = self._state
      if state.position + width > self.n_positions:
         _reject("position_bounds_exceeded")
      layers = list(state.layers)
      pending = state.tokens[-1]
      position = state.kv_positions
      produced: list[int] = []
      for _ in range(width):
         decision, layers = self._single_step(pending, position, layers)
         produced.append(decision)
         pending = decision
         position += 1
      return tuple(produced)

   def _single_step(
      self,
      token: int,
      position: int,
      layers: Sequence[dict[int, tuple[Any, Any]]],
   ) -> tuple[int, list[dict[int, tuple[Any, Any]]]]:
      inputs: Any = (token,)
      next_layers: list[dict[int, tuple[Any, Any]]] = []
      for stage, past in zip(self._stages, layers):
         inputs, stage_layers = stage.forward(
            inputs, position=position, past=past, path_id=self._path_id
         )
         next_layers.append(stage_layers)
      decisions = tuple(inputs)
      if len(decisions) != 1:
         _reject("wrong_verified_position_count")
      return decisions[0], next_layers

   # -- A10 TargetVerificationRuntimePort ------------------------------------
   def begin_verification(self) -> _RuntimeVerificationTransaction:
      if not self._staged:
         _reject("no_staged_candidates")
      transaction = _RuntimeVerificationTransaction(self)
      self._last_base = transaction._base
      self._open_transactions += 1
      return transaction

   def sequential_reference(self, candidate_position_count: int) -> SequentialReference:
      width = candidate_position_count
      candidates = self._staged
      if len(candidates) != width:
         _reject("staged_candidate_width_mismatch")
      base = self._last_base if self._last_base is not None else self._state
      if base.position + width > self.n_positions:
         _reject("position_bounds_exceeded")
      layers = list(base.layers)
      pending = base.tokens[-1]
      position = base.kv_positions
      decisions: list[int] = []
      for offset in range(width):
         decision, layers = self._single_step(pending, position, layers)
         decisions.append(decision)
         pending = candidates[offset]
         position += 1
         self._sequential_invocations += 1
      accepted = 0
      flags: list[PositionDecision] = []
      for offset, candidate in enumerate(candidates):
         agreed = accepted == offset and candidate == decisions[offset]
         if agreed:
            accepted += 1
         flags.append(PositionDecision(offset=offset, accepted=agreed))
      if self._mode == "rollback_to_prefix":
         tokens = base.tokens
      else:
         committed = list(candidates[:accepted])
         if (
            self._mode == "commit_verified_prefix_plus_target_correction"
            and accepted < width
         ):
            committed.append(decisions[accepted])
         tokens = base.tokens + tuple(committed)
      return SequentialReference(
         prefix_position=base.position,
         prefix_digest=base.digest(),
         decisions=tuple(flags),
         committed_position=len(tokens),
         committed_digest=committed_prefix_digest(tokens, len(tokens) - 1),
      )

   # -- target-owned state --------------------------------------------------
   def committed_prefix_position(self) -> int:
      return self._state.position

   def committed_prefix_digest(self) -> str:
      return self._state.digest()

   def committed_kv_positions(self) -> tuple[int, ...]:
      """The retained KV length of every stage (equal by construction)."""

      return tuple(
         stage.kv_length(layers) for stage, layers in zip(self._stages, self._state.layers)
      )

   def snapshot(self) -> dict[str, Any]:
      """Privacy-reduced counters: no tokens, decisions, logits or KV bytes."""

      return {
         "protocol": PROTOCOL,
         "verification_ref": self.verification_ref,
         "backend": self._stages[0].backend,
         "stage_count": len(self._stages),
         "committed_position": self._state.position,
         "bounded_invocations": self._invocations,
         "sequential_invocations": self._sequential_invocations,
         "commits": self._commits,
         "rollbacks": self._rollbacks,
         "cleanups": self._cleanups,
         "open_transactions": self._open_transactions,
      }


def local_verifier_stages(port: Any, stage_factory: Callable[[str], Any]) -> tuple[Any, ...]:
   """Every locally loaded stage of ``port`` in graph order, as verifier stages."""

   bound = getattr(port, "_bound", None)
   if not isinstance(bound, Mapping):
      _reject("verifier_stage_not_local")
   order = {stage.stage_id: index for index, stage in enumerate(port.graph.stages)}
   placements = sorted(bound, key=lambda placement_id: order[bound[placement_id][0].stage_id])
   return tuple(stage_factory(placement_id) for placement_id in placements)


__all__ = [
   "A10RuntimeVerifierError",
   "CLEANUP_RELEASED",
   "PREFIX_DIGEST_PROTOCOL",
   "PROTOCOL",
   "PortVerifierStage",
   "RuntimeTargetVerifier",
   "committed_prefix_digest",
   "local_verifier_stages",
]
