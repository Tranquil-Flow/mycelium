# SPDX-License-Identifier: AGPL-3.0-only
"""Cross-host parity challenge runner and its closed document helper."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mycelium_a7_standby_qualification import (
   PARITY_CHALLENGE_PROTOCOL,
   StandbyQualificationError,
   physical_parity_challenge,
   validate_physical_parity_challenge,
)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "a7_physical_parity.py"
VENV_PY = Path(
   os.path.expanduser("~/Documents/playground/mycelium-v2-m0-test-ladder"
   "/.venv/bin/python")
)

_COMMIT = "a" * 40


def _identity(*, dtype: str = "float32") -> dict:
   return {
      "model_id": "model-0",
      "resolved_commit": _COMMIT,
      "manifest_digest": "sha256:" + "b" * 64,
      "deployment_id": "dep-1",
      "deployment_epoch": 1,
      "layer_range": [0, 2],
      "component_roles": ["attention", "mlp"],
      "decode_mode": "stage_local_kv",
      "runtime.backend": "mlx",
      "runtime.backend_version": "0.31.1",
      "runtime.device": "gpu:0",
      "runtime.dtype": dtype,
      "runtime.quantization": "none",
      "runtime.architecture": "gpt2",
      "placement.stage_id": "1",
      "placement.stage_signature": "sha256:" + "c" * 64,
      "placement.runtime_backend": "mlx",
      "load_proof_digest": "sha256:" + "d" * 64,
      "placement_id": "placement-001",
      "node_id": "node-b",
      "lifecycle_state": "ACTIVE",
   }


def _challenge(**overrides) -> dict:
   body = dict(
      challenge_id="a7-physical-test",
      source_identity=_identity(),
      standby_identity=_identity(),
      source_output_digest="sha256:" + "e" * 64,
      standby_output_digest="sha256:" + "e" * 64,
      candidate_commit=_COMMIT,
      measured_at_unix_ms=1_790_000_000_000,
   )
   body.update(overrides)
   return physical_parity_challenge(**body)


class _Env:
   def __init__(self, tmp_path: Path, *, grant: str | None) -> None:
      self.records = tmp_path / "records"
      self.records.mkdir(parents=True, exist_ok=True)
      # Build the digest the same way the gate driver does.
      sys.path.insert(0, str(ROOT / "scripts"))
      import run_a7_kv_successor_gate as gate

      plan = gate.build_plan(_COMMIT)
      self.plan_path = tmp_path / "plan.json"
      self.plan_path.write_bytes(gate._canonical(plan) + b"\n")
      self.env = {
         k: v
         for k, v in os.environ.items()
         if k not in {"MYCELIUM_A7_FLEET_GRANT", "MYCELIUM_A7_PARITY_CONFIG"}
      }
      if grant is not None:
         self.env["MYCELIUM_A7_FLEET_GRANT"] = grant
      self.env["PYTHONDONTWRITEBYTECODE"] = "1"

   def run_case(self, case: str) -> subprocess.CompletedProcess:
      return subprocess.run(
         [
            str(VENV_PY),
            "-B",
            str(SCRIPT),
            "--plan",
            str(self.plan_path),
            "--records",
            str(self.records),
            "--case",
            case,
         ],
         env=self.env,
         capture_output=True,
         text=True,
         timeout=60,
      )


def _run_script(args: list[str], env: dict) -> subprocess.CompletedProcess:
   return subprocess.run(
      [str(VENV_PY), "-B", str(SCRIPT), *args],
      env=env,
      capture_output=True,
      text=True,
      timeout=60,
   )


# --- qualification helper ----------------------------------------------------


def test_physical_parity_challenge_seals_matching_pair() -> None:
   record = _challenge()
   assert record["protocol"] == PARITY_CHALLENGE_PROTOCOL
   assert record["source_identity_digest"] == record["standby_identity_digest"]
   assert record["source_output_digest"] == record["standby_output_digest"]
   assert record["candidate_commit"] == _COMMIT
   body = {k: v for k, v in record.items() if k != "evidence_digest"}
   import hashlib

   expected = "sha256:" + hashlib.sha256(
      json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
   ).hexdigest()
   assert record["evidence_digest"] == expected
   assert validate_physical_parity_challenge(record) == record


def test_physical_parity_challenge_refuses_identity_mismatch() -> None:
   with pytest.raises(StandbyQualificationError) as exc:
      _challenge(standby_identity=_identity(dtype="float16"))
   assert exc.value.code == "physical_parity_identity_mismatch"


def test_physical_parity_challenge_refuses_output_mismatch() -> None:
   with pytest.raises(StandbyQualificationError) as exc:
      _challenge(standby_output_digest="sha256:" + "f" * 64)
   assert exc.value.code == "physical_parity_outputs_differ"


def test_physical_parity_challenge_refuses_malformed_digest() -> None:
   with pytest.raises(StandbyQualificationError) as exc:
      _challenge(source_output_digest="not-a-digest")
   assert exc.value.code == "physical_parity_output_digest_invalid"


def test_validate_physical_parity_challenge_refuses_tampered_body() -> None:
   record = _challenge()
   record["measured_at_unix_ms"] = 1
   with pytest.raises(StandbyQualificationError) as exc:
      validate_physical_parity_challenge(record)
   assert exc.value.code == "physical_parity_digest_mismatch"


# --- script CLI contract ------------------------------------------------------


def test_unknown_case_exits_2(tmp_path: Path) -> None:
   env = _Env(tmp_path, grant="FLEET GRANTED TO A7")
   result = env.run_case("no_such_case")
   assert result.returncode == 2
   assert "a7_parity_case_unknown" in result.stderr


def test_missing_fleet_grant_exits_2_and_writes_nothing(tmp_path: Path) -> None:
   env = _Env(tmp_path, grant=None)
   result = env.run_case("ordinary_browser_source_kill_kv_successor")
   assert result.returncode == 2
   assert "a7_parity_fleet_grant_missing" in result.stderr
   parity_dir = env.records / "parity"
   assert not parity_dir.exists() or not any(parity_dir.iterdir())


def test_grant_present_without_config_refuses_measurement(tmp_path: Path) -> None:
   env = _Env(tmp_path, grant="FLEET GRANTED TO A7")
   result = env.run_case("ordinary_browser_source_kill_kv_successor")
   assert result.returncode == 2
   assert "physical_parity_config_missing" in result.stderr
   parity_dir = env.records / "parity"
   assert not parity_dir.exists() or not any(parity_dir.iterdir())


def test_missing_plan_exits_2(tmp_path: Path) -> None:
   env = _Env(tmp_path, grant="FLEET GRANTED TO A7")
   result = _run_script(
      [
         "--plan",
         str(tmp_path / "nope.json"),
         "--records",
         str(env.records),
         "--case",
         "ordinary_browser_source_kill_kv_successor",
      ],
      env.env,
   )
   assert result.returncode == 2
   assert "a7_parity_plan_invalid" in result.stderr


def test_preexisting_record_exits_2(tmp_path: Path) -> None:
   env = _Env(tmp_path, grant="FLEET GRANTED TO A7")
   parity_dir = env.records / "parity"
   parity_dir.mkdir(parents=True, exist_ok=True)
   (parity_dir / "node-b-node-0-ordinary_browser_source_kill_kv_successor.json").write_text(
      "{}", encoding="utf-8"
   )
   result = env.run_case("ordinary_browser_source_kill_kv_successor")
   assert result.returncode == 2
   assert "a7_parity_record_exists" in result.stderr
