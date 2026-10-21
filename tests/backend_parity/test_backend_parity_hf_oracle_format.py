"""PV29 L1: the standalone HF-oracle script writes exactly what the gate's loader reads (no torch needed)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from mycelium_platform_neutral import backend_parity as bp
from mycelium_platform_neutral import backend_parity_route as rt

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "backend_parity_hf_oracle.py"
CORPUS = Path(__file__).resolve().parents[2] / "tolerances" / "backend-parity-corpus-qwen2.5-0.5b-instruct.v1.json"


def _script():
    spec = importlib.util.spec_from_file_location("backend_parity_hf_oracle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_archive_written_by_the_script_loads_through_the_gate_loader(tmp_path) -> None:
    script = _script()
    corpus = bp.load_corpus(CORPUS)
    rng = np.random.default_rng(0)
    results = {
        case["case_id"]: (
            rng.normal(size=(len(bp.scored_positions(case)), 16)).astype(np.float32),
            {12: rng.normal(size=(len(case["token_ids"]) - 1, 8)).astype(np.float32)},
        )
        for case in corpus["cases"]
    }
    info = script.write_archive(tmp_path / "o.npz", script.build_archive(results), {"torch": "x"})
    loaded = rt.load_oracle(tmp_path / "o.npz", corpus, expected_digest=info["archive_digest"])
    for case_id, (logits, hidden) in results.items():
        assert np.array_equal(loaded.cases[case_id].logits, logits)
        assert np.array_equal(loaded.cases[case_id].hops[12], hidden[12])
    assert info["implementation"] == script.IMPLEMENTATION and info["dtype"] == "float32"


def test_the_scripts_scoring_rule_and_corpus_digest_match_the_package() -> None:
    script = _script()
    corpus = bp.load_corpus(CORPUS)
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    assert script._digest_bytes(script._canonical(raw)) == bp.corpus_digest(corpus)
    for case in corpus["cases"]:
        assert script.scored_positions(case) == bp.scored_positions(case)


def test_checked_in_corpus_is_the_documented_shape() -> None:
    corpus = bp.load_corpus(CORPUS)
    assert len(corpus["cases"]) == 8
    assert sum(len(bp.scored_positions(c)) for c in corpus["cases"]) == 192
    assert {c["case_id"] for c in corpus["cases"]} >= {"chat_short", "code_python", "multilingual", "repeated_token", "medium_context"}
    assert corpus["revision"] == "7ae557604adf67be50417f59c2c2f167def9a775"
