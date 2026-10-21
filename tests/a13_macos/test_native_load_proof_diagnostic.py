# SPDX-License-Identifier: AGPL-3.0-only
"""A refused placement load proof must say which part of the proof differed.

Two-Mac run 14: the joined Mac refused ``configure`` with
``placement_load_proof_mismatch`` and the native-failure record carried only
that code; the field that differed (``probe_digest``, device float rounding)
had to be reconstructed offline by reloading the stage.  The node now prints
one fixed-shape line of component digests of the proof it observed, and the
managed worker keeps only those allowlisted ``sha256:`` values, so the next run
can diff them against the owner's compiled ``node-N-load-proof.json``.
"""
import hashlib
import json

import pytest

from mycelium_a13_macos.worker import (
    _bounded_load_proof_diagnostic,
    _bounded_native_diagnostics,
)
from physical_inference_node import _emit_load_proof_diagnostic

_SHA = "sha256:" + "a" * 64
_PROOF = {
    "assignment_id": "980cbf30-696c-524d-8937-380c6d3b2efe",
    "loaded_tensor_digest": "sha256:" + "1" * 64,
    "probe_digest": "sha256:" + "2" * 64,
    "runtime_identity": {"backend": "mlx", "backend_version": "0.31.1",
                         "device": "Device(gpu, 0)"},
    "stage_pack_digest": "sha256:" + "3" * 64,
    "stage_pack_verification_digest": "sha256:" + "4" * 64,
}


def _canonical_digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(raw.encode()).hexdigest()


def test_node_prints_component_digests_of_the_observed_proof(capsys):
    _emit_load_proof_diagnostic(_PROOF, expected_load_proof_digest=_SHA)
    line = capsys.readouterr().err.strip()
    assert line.startswith("MYCELIUM-LOAD-PROOF-DIAG ")
    result = _bounded_load_proof_diagnostic(line)
    assert result == {
        "protocol": "mycelium.a13.native_load_proof_diagnostic.v1",
        "expected_load_proof_digest": _SHA,
        "observed_load_proof_digest": _canonical_digest(_PROOF),
        "loaded_tensor_digest": _PROOF["loaded_tensor_digest"],
        "probe_digest": _PROOF["probe_digest"],
        "runtime_identity_digest": _canonical_digest(_PROOF["runtime_identity"]),
        "stage_pack_digest": _PROOF["stage_pack_digest"],
    }


def test_proof_without_a_stage_pack_reports_none(capsys):
    proof = {key: value for key, value in _PROOF.items() if not key.startswith("stage_pack")}
    _emit_load_proof_diagnostic(proof, expected_load_proof_digest=_SHA)
    result = _bounded_load_proof_diagnostic(capsys.readouterr().err)
    assert result["stage_pack_digest"] == "none"


def test_native_failure_record_carries_the_load_proof_verdict(capsys):
    _emit_load_proof_diagnostic(_PROOF, expected_load_proof_digest=_SHA)
    stderr = ("noise before\n" + capsys.readouterr().err + "NodeCommandError: x\n").encode()
    record = _bounded_native_diagnostics(stderr)
    assert record["load_proof"]["probe_digest"] == _PROOF["probe_digest"]


@pytest.mark.parametrize("line", [
    # a value that is not a sha256 reference (could carry a path or prompt)
    "MYCELIUM-LOAD-PROOF-DIAG expected_load_proof_digest=/Users/x/model.safetensors"
    f" observed_load_proof_digest={_SHA} loaded_tensor_digest={_SHA}"
    f" probe_digest={_SHA} runtime_identity_digest={_SHA} stage_pack_digest=none",
    # missing key
    f"MYCELIUM-LOAD-PROOF-DIAG expected_load_proof_digest={_SHA}",
    # extra key
    f"MYCELIUM-LOAD-PROOF-DIAG expected_load_proof_digest={_SHA}"
    f" observed_load_proof_digest={_SHA} loaded_tensor_digest={_SHA}"
    f" probe_digest={_SHA} runtime_identity_digest={_SHA} stage_pack_digest=none prompt=hi",
    # duplicate key
    f"MYCELIUM-LOAD-PROOF-DIAG expected_load_proof_digest={_SHA}"
    f" expected_load_proof_digest={_SHA} loaded_tensor_digest={_SHA}"
    f" probe_digest={_SHA} runtime_identity_digest={_SHA} stage_pack_digest=none",
])
def test_malformed_lines_yield_nothing(line):
    assert _bounded_load_proof_diagnostic(line) is None
    assert "load_proof" not in _bounded_native_diagnostics(line.encode())
