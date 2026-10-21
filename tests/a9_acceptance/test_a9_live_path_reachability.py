# SPDX-License-Identifier: AGPL-3.0-only
"""A9 platform-neutral int8 reachability, pinned as the live route needs it.

This module is the *live-path* companion to
``test_a9_platform_neutral_runtime_reachability.py``.  That module proves what the
A9 selection *does* when the tranche is importable (kernels dispatched, codec
measured, report content).  This one pins the four properties the live route
depends on, none of which the sibling module asserts:

1. the loader entry point the live composition calls
   (``mycelium_live/qwen_capture.load_bundle_stages`` and the live route builder
   both call ``load_assignment_stage(assignment, report, load_generation=...)``)
   still accepts that exact call shape, keeps the qualified default, and selects
   the A9 tranche only through the explicit keyword-only selection.  A live
   loader call cannot silently change meaning.
2. a requested ``platform-neutral-int8`` load whose kernel module is not
   importable **refuses** (``a9_platform_neutral_unavailable``) instead of
   quietly returning a qualified ``weight-quantization`` stage.  The seam can
   therefore never go dormant without a red test: no silent downgrade.
3. the same holds for the reduced-precision activation frame codec, so the
   second half of the tranche is required too (an unavailable codec must not be
   skipped while the kernel runs).
4. the qualified default is byte-identical whether or not the tranche is
   importable: the promotion is additive, and the qualified path the live route
   uses today keeps working when the A9 source tranche is absent.

Fixture provenance: the real safetensors artifact + verified artifact report come
from the sibling module's builders, so this file loads exactly the same real
qwen2 ``int8-weight-only`` assignment instead of a divergent copy.

Boundary: reachability is not qualification.  Nothing here places, qualifies or
readies anything, and the live route deliberately keeps the qualified default;
wiring the live composition to select the platform-neutral kernel is a
claim-boundary decision outside this lane (the A9 kernel host is the portable
``numpy`` correctness-only reference and no physical round has been executed).
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import runtime_loader
from runtime_loader import RuntimeLoadError, canonical_json, load_assignment_stage
from tests.a9_acceptance import (
    test_a9_platform_neutral_runtime_reachability as a9_reachability,
)

A9_INT8_EXECUTION_PROOF_KEY = a9_reachability.A9_INT8_EXECUTION_PROOF_KEY
A9_INT8_RUNTIME_EXECUTION_PROTOCOL = a9_reachability.A9_INT8_RUNTIME_EXECUTION_PROTOCOL
DEFAULT_ACTIVATION_FRAME_DTYPE = a9_reachability.DEFAULT_ACTIVATION_FRAME_DTYPE
DEFAULT_INT8_EXECUTION = a9_reachability.DEFAULT_INT8_EXECUTION
EXPECTED_A9_KERNELS = a9_reachability.EXPECTED_A9_KERNELS
PLATFORM_NEUTRAL_INT8_EXECUTION = a9_reachability.PLATFORM_NEUTRAL_INT8_EXECUTION
QUALIFIED_INT8_EXECUTION = a9_reachability.QUALIFIED_INT8_EXECUTION
QUALIFIED_PROOF_FIELDS = a9_reachability.QUALIFIED_PROOF_FIELDS

#: The two tranche modules the runtime loader imports lazily, one per half of
#: the promotion.  ``sys.modules[name] = None`` makes the next import of that
#: name raise ``ImportError``, which is how an unavailable source tranche is
#: simulated on a host that has the source tree.
A9_KERNEL_MODULE = "mycelium_platform_neutral.int8_execution"
A9_CODEC_MODULE = "mycelium_platform_neutral.activation_transport"

LIVE_LOAD_GENERATION = 31


def _case(tmp_path: Path) -> tuple[dict[str, object], Any]:
    """The sibling module's real artifact, real assignment, verified report."""

    assignment, artifact = a9_reachability._case(tmp_path)
    return assignment, a9_reachability._verified_report(assignment, artifact)


def _proof(stage: Any) -> dict[str, Any]:
    """The load proof as ordinary JSON, after the loader's canonical round-trip."""

    return json.loads(canonical_json(stage.proof))


def test_live_loader_call_shape_reaches_the_a9_tranche(tmp_path: Path) -> None:
    assignment, report = _case(tmp_path)

    signature = inspect.signature(load_assignment_stage)
    selection = signature.parameters["int8_execution"]
    frame_dtype = signature.parameters["activation_frame_dtype"]
    assert selection.kind is inspect.Parameter.KEYWORD_ONLY
    assert frame_dtype.kind is inspect.Parameter.KEYWORD_ONLY
    assert selection.default == DEFAULT_INT8_EXECUTION == QUALIFIED_INT8_EXECUTION
    assert frame_dtype.default == DEFAULT_ACTIVATION_FRAME_DTYPE

    # The live composition's exact call shape, plus the explicit A9 selection.
    stage = load_assignment_stage(
        assignment,
        report,
        load_generation=LIVE_LOAD_GENERATION,
        int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
    )
    proof = _proof(stage)
    assert proof[A9_INT8_EXECUTION_PROOF_KEY]["protocol"] == (
        A9_INT8_RUNTIME_EXECUTION_PROTOCOL
    )
    assert proof[A9_INT8_EXECUTION_PROOF_KEY]["kernels_executed"] == EXPECTED_A9_KERNELS

    # The same call shape with no selection is still the qualified live load.
    live_default = load_assignment_stage(
        assignment, report, load_generation=LIVE_LOAD_GENERATION
    )
    assert set(live_default.proof) == QUALIFIED_PROOF_FIELDS
    assert A9_INT8_EXECUTION_PROOF_KEY not in live_default.proof


def test_unavailable_int8_kernel_module_refuses_not_downgrades(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, report = _case(tmp_path)
    monkeypatch.setitem(sys.modules, A9_KERNEL_MODULE, None)

    with pytest.raises(RuntimeLoadError, match="a9_platform_neutral_unavailable"):
        load_assignment_stage(
            assignment,
            report,
            load_generation=LIVE_LOAD_GENERATION,
            int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
        )

    # The refusal is about the requested selection, not a broken environment:
    # the qualified default still serves in the same process and carries no
    # trace of the tranche.
    default = load_assignment_stage(
        assignment, report, load_generation=LIVE_LOAD_GENERATION
    )
    assert set(default.proof) == QUALIFIED_PROOF_FIELDS
    assert A9_INT8_EXECUTION_PROOF_KEY not in default.proof


def test_unavailable_activation_transport_refuses_the_frame_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, report = _case(tmp_path)
    monkeypatch.setitem(sys.modules, A9_CODEC_MODULE, None)

    with pytest.raises(RuntimeLoadError, match="a9_platform_neutral_unavailable"):
        load_assignment_stage(
            assignment,
            report,
            load_generation=LIVE_LOAD_GENERATION,
            int8_execution=PLATFORM_NEUTRAL_INT8_EXECUTION,
            activation_frame_dtype="bfloat16",
        )


def test_qualified_default_is_independent_of_the_a9_tranche(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assignment, report = _case(tmp_path)

    with monkeypatch.context() as unavailable:
        unavailable.setitem(sys.modules, A9_KERNEL_MODULE, None)
        unavailable.setitem(sys.modules, A9_CODEC_MODULE, None)
        without_tranche = _proof(
            load_assignment_stage(
                assignment, report, load_generation=LIVE_LOAD_GENERATION
            )
        )

    with_tranche = _proof(
        load_assignment_stage(assignment, report, load_generation=LIVE_LOAD_GENERATION)
    )
    # Byte-identical qualified load proof: the promotion is additive and the
    # live route's default path never depends on the A9 source tranche.
    assert without_tranche == with_tranche
    assert set(with_tranche) == QUALIFIED_PROOF_FIELDS
    assert A9_INT8_EXECUTION_PROOF_KEY not in with_tranche
    assert with_tranche["runtime"]["quantization"] == "int8-weight-only"
    assert runtime_loader.DEFAULT_INT8_EXECUTION == QUALIFIED_INT8_EXECUTION
