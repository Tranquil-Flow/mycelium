# SPDX-License-Identifier: AGPL-3.0-only
"""A10 target verification executed on the real MLX and NumPy stage runtimes.

The verifier arithmetic of ``a10_verifier_execution`` runs here over a real
``TargetVerificationRuntimePort``: ``MLXRuntimePort.begin_target_verifier`` and
``NumpyRuntimePort.begin_target_verifier`` build request-local target KV with the
runtimes' own stage kernels, evaluate every bounded candidate position in one pass
through the ordered target stages, and replay the same prefix one position at a time
for the sequential oracle.

Models are the tiny offline fixtures the runtime suites already use (a two-layer
GPT-2 on MLX, a two-layer Qwen2/Qwen3 on NumPy); nothing is downloaded.  Independent
authority: the committed tokens are also checked against a full-context replay of
the loaded stages (``runtime_loader.execute_loaded_stage`` / ``numpy_runtime``),
which shares no KV code with the verifier.
"""

from __future__ import annotations

from tests.a10_deterministic import ensure_worktree_imports

ensure_worktree_imports()

import tempfile
from pathlib import Path
from types import SimpleNamespace

import mlx.core as mx
import numpy as np
import pytest

from layer_assignment import compile_layer_assignments
from mycelium_router.a10_runtime_verifier import (
   A10RuntimeVerifierError,
   RuntimeTargetVerifier,
   committed_prefix_digest,
)
from mycelium_router.a10_target_verification import (
   TargetVerificationRequest,
   sequential_parity_reference,
   verify_candidate_positions,
)
from mycelium_router.a10_runtime_batch import A10ContractError
from mycelium_router.decoding import quantized_greedy_token_id
from mycelium_router.mlx_runtime import MLXRuntimePort
from runtime_loader import execute_loaded_stage, load_assignment_stage
from test_router_mlx_runtime import _build_graph
from two_process_runtime_qualification import (
   DEPLOYMENT_EPOCH,
   DEPLOYMENT_ID,
   LOAD_GENERATION,
   _LocalOnlyFetcher,
   _build_local_model,
   _control_plane_binding,
   _route_for_manifest,
)
from weight_provisioning import provision_assignment


DIGEST = "sha256:" + "a" * 64


@pytest.fixture(scope="module")
def mlx_case():
   temporary = tempfile.TemporaryDirectory(prefix="mycelium-a10-verifier-")
   root = Path(temporary.name)
   manifest, _ = _build_local_model(root, n_positions=16)
   route = _route_for_manifest(manifest)
   assignments = compile_layer_assignments(
      route_plan=route,
      manifest=manifest,
      deployment_id=DEPLOYMENT_ID,
      deployment_epoch=DEPLOYMENT_EPOCH,
      cache_roots={node: str(root) for node in route["node_order"]},
      runtime_by_node={
         node: {"backend": "mlx", "dtype": "float32", "quantization": "none"}
         for node in route["node_order"]
      },
      control_plane_binding=_control_plane_binding(),
   )
   fetcher = _LocalOnlyFetcher(root)
   reports = [
      provision_assignment(assignment, fetch_file=fetcher, local_files_only=True)
      for assignment in assignments
   ]
   loaded = tuple(
      load_assignment_stage(assignment, report, load_generation=LOAD_GENERATION)
      for assignment, report in zip(assignments, reports)
   )
   graph = _build_graph(assignments, loaded)
   ports = tuple(
      MLXRuntimePort(
         assignment["node_id"],
         graph,
         {graph.stages[index].placements[0].placement_id: loaded[index]},
      )
      for index, assignment in enumerate(assignments)
   )
   yield SimpleNamespace(loaded=loaded, graph=graph, ports=ports)
   temporary.cleanup()


def _mlx_verifier(case, prefix, ref="op"):
   stages = case.ports[0].a10_verifier_stages() + case.ports[1].a10_verifier_stages()
   return case.ports[0].begin_target_verifier(prefix, verification_ref=ref, stages=stages)


def _mlx_reference_next(case, tokens):
   hidden = execute_loaded_stage(
      case.loaded[0], token_ids=mx.array((tuple(tokens),), dtype=mx.uint32)
   )
   logits = execute_loaded_stage(case.loaded[1], hidden_states=hidden)
   return quantized_greedy_token_id(logits[0, -1, :].tolist())


def _request(verifier, width, mode, ref="request-0"):
   return TargetVerificationRequest(
      request_ref=ref,
      target_kv_ref="target-kv-0",
      runtime_incarnation="incarnation-0",
      committed_prefix_position=verifier.committed_prefix_position(),
      committed_prefix_digest=verifier.committed_prefix_digest(),
      candidate_position_count=width,
      candidate_bytes=4 * width,
      generation_policy="deterministic_greedy",
      transaction_mode=mode,
      deadline_at=1_000_000.0,
      cancellation_generation=0,
      idempotency_digest=DIGEST,
   )


def _perturb(tokens, offset, vocab):
   changed = list(tokens)
   changed[offset] = (changed[offset] + 1) % vocab
   return tuple(changed)


def test_mlx_full_acceptance_commits_the_whole_width_with_parity(mlx_case):
   verifier = _mlx_verifier(mlx_case, (1, 2, 3))
   continuation = verifier.greedy_continuation(4)
   verifier.stage_candidates(
      continuation, transaction_mode="commit_verified_prefix_plus_target_correction"
   )
   request = _request(verifier, 4, "commit_verified_prefix_plus_target_correction")

   result = sequential_parity_reference(verifier, request)

   assert result.parity is True
   assert result.mismatched_offsets == ()
   assert result.outcome.accepted_prefix_count == 4
   assert result.outcome.correction_count == 0
   assert result.outcome.commit_state == "committed"
   assert verifier.committed_prefix_position() == 7
   assert verifier.committed_kv_positions() == (6, 6)
   assert verifier.committed_prefix_digest() == committed_prefix_digest(
      (1, 2, 3, *continuation), 6
   )
   snapshot = verifier.snapshot()
   assert snapshot["bounded_invocations"] == 1
   assert snapshot["open_transactions"] == 0
   assert snapshot["cleanups"] == 1


def test_mlx_continuation_matches_independent_full_context_replay(mlx_case):
   verifier = _mlx_verifier(mlx_case, (1, 2, 3))
   continuation = verifier.greedy_continuation(5)
   tokens = [1, 2, 3]
   for expected in continuation:
      assert _mlx_reference_next(mlx_case, tokens) == expected
      tokens.append(expected)


def test_mlx_rejection_commits_the_target_correction_from_the_same_call(mlx_case):
   verifier = _mlx_verifier(mlx_case, (4, 1))
   continuation = verifier.greedy_continuation(5)
   candidates = _perturb(continuation, 2, verifier.vocab_size)
   verifier.stage_candidates(
      candidates, transaction_mode="commit_verified_prefix_plus_target_correction"
   )
   request = _request(verifier, 5, "commit_verified_prefix_plus_target_correction")

   result = sequential_parity_reference(verifier, request)

   assert result.parity is True
   assert result.outcome.accepted_prefix_count == 2
   assert result.outcome.correction_count == 1
   assert result.outcome.commit_state == "committed_with_correction"
   assert verifier.committed_prefix_position() == 2 + 3
   assert verifier.committed_kv_positions() == (4, 4)
   # The correction is the target's own token, which is the greedy continuation.
   assert verifier.committed_prefix_digest() == committed_prefix_digest(
      (4, 1, *continuation[:3]), 4
   )
   # The committed KV is live: decoding on from it agrees with a full replay.
   next_token = verifier.greedy_continuation(1)[0]
   assert next_token == _mlx_reference_next(mlx_case, (4, 1, *continuation[:3]))


def test_mlx_rollback_restores_the_exact_supplied_prefix(mlx_case):
   verifier = _mlx_verifier(mlx_case, (2, 5, 6))
   before = (verifier.committed_prefix_position(), verifier.committed_prefix_digest())
   verifier.stage_candidates(
      verifier.greedy_continuation(3), transaction_mode="rollback_to_prefix"
   )

   result = sequential_parity_reference(verifier, _request(verifier, 3, "rollback_to_prefix"))

   assert result.parity is True
   assert result.outcome.commit_state == "rolled_back"
   assert (verifier.committed_prefix_position(), verifier.committed_prefix_digest()) == before
   assert verifier.committed_kv_positions() == (2, 2)


def test_mlx_commit_verified_prefix_without_correction_commits_nothing_on_first_miss(mlx_case):
   verifier = _mlx_verifier(mlx_case, (3, 3))
   candidates = _perturb(verifier.greedy_continuation(2), 0, verifier.vocab_size)
   verifier.stage_candidates(candidates, transaction_mode="commit_verified_prefix")

   result = sequential_parity_reference(
      verifier, _request(verifier, 2, "commit_verified_prefix")
   )

   assert result.parity is True
   assert result.outcome.accepted_prefix_count == 0
   assert result.outcome.committed_position == 2
   assert verifier.committed_kv_positions() == (1, 1)


def test_mlx_stale_prefix_is_refused_and_leaves_the_prefix_untouched(mlx_case):
   verifier = _mlx_verifier(mlx_case, (1, 2))
   verifier.stage_candidates((1,), transaction_mode="commit_verified_prefix")
   request = _request(verifier, 1, "commit_verified_prefix")
   stale = TargetVerificationRequest(
      **{**request.__dict__, "committed_prefix_digest": DIGEST}
   )
   with pytest.raises(A10ContractError) as refused:
      verify_candidate_positions(verifier, stale)
   assert refused.value.code == "stale_prefix"
   assert verifier.committed_prefix_position() == 2
   assert verifier.snapshot()["open_transactions"] == 0


def test_mlx_width_beyond_the_position_bound_fails_closed(mlx_case):
   verifier = _mlx_verifier(mlx_case, tuple(range(1, 7)) + (1, 2, 3, 4, 5, 6))
   verifier.stage_candidates((1, 2, 3, 4, 5), transaction_mode="commit_verified_prefix")
   with pytest.raises(A10RuntimeVerifierError) as refused:
      verify_candidate_positions(verifier, _request(verifier, 5, "commit_verified_prefix"))
   assert refused.value.code == "position_bounds_exceeded"
   assert verifier.committed_prefix_position() == 12


def test_mlx_incomplete_or_disordered_stage_chain_is_refused(mlx_case):
   entry = mlx_case.ports[0].a10_verifier_stages()
   final = mlx_case.ports[1].a10_verifier_stages()
   with pytest.raises(A10RuntimeVerifierError) as refused:
      RuntimeTargetVerifier(entry, prefix_tokens=(1, 2), verification_ref="x")
   assert refused.value.code == "verifier_stage_chain_incomplete"
   with pytest.raises(A10RuntimeVerifierError):
      RuntimeTargetVerifier(final + entry, prefix_tokens=(1, 2), verification_ref="x")


@pytest.mark.parametrize("prefix", [(1, 2), (6, 5, 4, 3, 2)])
@pytest.mark.parametrize("width", [1, 3, 6])
@pytest.mark.parametrize("miss", [None, 0, 2])
def test_mlx_verifier_matches_sequential_target_across_widths_and_buckets(
   mlx_case, prefix, width, miss
):
   verifier = _mlx_verifier(mlx_case, prefix)
   candidates = verifier.greedy_continuation(width)
   if miss is not None and miss < width:
      candidates = _perturb(candidates, miss, verifier.vocab_size)
   mode = "commit_verified_prefix_plus_target_correction"
   verifier.stage_candidates(candidates, transaction_mode=mode)

   result = sequential_parity_reference(verifier, _request(verifier, width, mode))

   assert result.parity is True
   expected_accepted = width if miss is None or miss >= width else miss
   assert result.outcome.accepted_prefix_count == expected_accepted


def test_mlx_multi_position_chunk_after_retained_kv_is_causal(mlx_case):
   """Regression: a GPT-2 chunk extending retained KV may not see its own future."""

   stages = mlx_case.ports[0].a10_verifier_stages() + mlx_case.ports[1].a10_verifier_stages()
   tokens = (1, 2, 3, 4, 5)
   hidden, first = stages[0].forward(tokens[:2], position=0, past={}, path_id="causal")
   chunked, _ = stages[0].forward(tokens[2:], position=2, past=first, path_id="causal")
   whole, _ = stages[0].forward(tokens, position=0, past={}, path_id="causal-whole")
   assert np.allclose(np.array(chunked), np.array(whole[:, 2:, :]), atol=1e-5)


def _numpy_verifier(tmp_path, architecture, prefix):
   from test_router_numpy_qwen_kv import _case

   case = _case(tmp_path, architecture)
   stages = case.ports[0].a10_verifier_stages() + case.ports[1].a10_verifier_stages()
   return case, case.ports[0].begin_target_verifier(
      prefix, verification_ref="numpy-op", stages=stages
   )


@pytest.mark.parametrize("architecture", ["qwen2", "qwen3"])
def test_numpy_verifier_matches_sequential_target_and_full_replay(tmp_path, architecture):
   from numpy_runtime import execute_loaded_stage as numpy_execute_loaded_stage

   case, verifier = _numpy_verifier(tmp_path, architecture, (1, 2, 3))
   continuation = verifier.greedy_continuation(4)
   tokens = [1, 2, 3]
   for expected in continuation:
      hidden = numpy_execute_loaded_stage(
         case.loaded[0], token_ids=np.array((tuple(tokens),), dtype=np.int64)
      )
      logits = numpy_execute_loaded_stage(case.loaded[1], hidden_states=hidden)
      assert quantized_greedy_token_id(logits[0, -1].tolist()) == expected
      tokens.append(expected)

   mode = "commit_verified_prefix_plus_target_correction"
   for width, miss in ((4, None), (4, 1), (1, 0)):
      candidates = verifier.greedy_continuation(width)
      if miss is not None:
         candidates = _perturb(candidates, miss, verifier.vocab_size)
      verifier.stage_candidates(candidates, transaction_mode=mode)
      result = sequential_parity_reference(verifier, _request(verifier, width, mode))
      assert result.parity is True
      assert result.outcome.accepted_prefix_count == (width if miss is None else miss)
   assert verifier.snapshot()["open_transactions"] == 0
   assert len(set(verifier.committed_kv_positions())) == 1
