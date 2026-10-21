# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the MLX stage port can drop its weights and bring back byte-identical ones."""
from __future__ import annotations

import gc

import mlx.core as mx
import pytest

from layer_assignment import compile_layer_assignments  # noqa: F401  (fixture module dependency)
from mycelium_router.mlx_runtime import MLXRuntimeError, MLXRuntimePort
from runtime_loader import load_assignment_stage
from test_router_mlx_runtime import (  # noqa: F401  (runtime_case is a module-scoped fixture)
    LOAD_GENERATION, _LocalOnlyFetcher, _run_two_stage, _work_item, provision_assignment, runtime_case,
)
from mycelium_router.payloads import encode_token_ids

PROMPT = (1, 2, 3)


def _fresh(case):
    """Own LoadedStage objects: the shared fixture's must never be mutated."""
    fetcher = _LocalOnlyFetcher(case.root)

    def load(index):
        report = provision_assignment(case.assignments[index], fetch_file=fetcher, local_files_only=True)
        return load_assignment_stage(case.assignments[index], report, load_generation=LOAD_GENERATION)

    loaded = tuple(load(i) for i in range(len(case.assignments)))
    ports = tuple(
        MLXRuntimePort(a["node_id"], case.graph, {case.graph.stages[i].placements[0].placement_id: loaded[i]})
        for i, a in enumerate(case.assignments))
    return loaded, ports, load


def _loader_for(case, load, index):
    placement_id = case.graph.stages[index].placements[0].placement_id
    return lambda pid: load(index) if pid == placement_id else pytest.fail("unexpected placement")


def test_release_empties_the_tensors_and_execution_fails_closed(runtime_case):
    case = runtime_case
    loaded, ports, load = _fresh(case)
    assert ports[0].weights_resident is True and ports[0].active_request_count() == 0
    before = mx.get_active_memory()
    ports[0].release_weights()
    gc.collect()
    mx.clear_cache()
    assert ports[0].weights_resident is False
    assert len(loaded[0].tensors) == 0
    assert mx.get_active_memory() < before
    result = ports[0].execute(_work_item(case, 0, encode_token_ids(PROMPT), token_span=len(PROMPT)))
    assert not result.success and result.failure_reason == "weights_unloaded"
    assert ports[0].active_request_count() == 0


def test_restore_gives_identical_output_to_a_port_that_never_unloaded(runtime_case):
    case = runtime_case
    loaded, ports, load = _fresh(case)
    _, reference_token = _run_two_stage(case, PROMPT, phase="PREFILL", path_id="ref", ports=ports,
                                        emits_token=True, terminal=True)
    ports[0].release_weights()
    ports[1].release_weights()
    ports[0].restore_weights(_loader_for(case, load, 0))
    ports[1].restore_weights(_loader_for(case, load, 1))
    assert ports[0].weights_resident and ports[1].weights_resident
    _, token = _run_two_stage(case, PROMPT, phase="PREFILL", path_id="after", ports=ports,
                                  emits_token=True, terminal=True)
    assert token == reference_token


def test_release_is_refused_while_a_request_holds_kv(runtime_case):
    case = runtime_case
    loaded, ports, load = _fresh(case)
    first = ports[0].execute(_work_item(case, 0, encode_token_ids(PROMPT), token_span=len(PROMPT),
                                        request_id="live", path_id="live"))
    assert first.success
    assert ports[0].active_request_count() >= 1
    with pytest.raises(MLXRuntimeError) as error:
        ports[0].release_weights()
    assert error.value.code == "weights_in_use"
    assert ports[0].weights_resident is True


def test_restore_refuses_weights_that_do_not_match_the_authenticated_digest(runtime_case):
    case = runtime_case
    loaded, ports, load = _fresh(case)
    ports[0].release_weights()
    wrong = load(1)  # the other stage's tensors
    with pytest.raises(MLXRuntimeError) as error:
        ports[0].restore_weights(lambda pid: wrong)
    assert error.value.code == "weights_restore_mismatch"
    assert ports[0].weights_resident is False and len(loaded[0].tensors) == 0


def test_release_and_restore_are_idempotent_and_ordered(runtime_case):
    case = runtime_case
    loaded, ports, load = _fresh(case)
    ports[0].release_weights()
    ports[0].release_weights()  # already unloaded: no-op
    ports[0].restore_weights(_loader_for(case, load, 0))
    ports[0].restore_weights(_loader_for(case, load, 0))  # already resident: loader not called
    assert ports[0].weights_resident is True
