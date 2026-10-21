# SPDX-License-Identifier: AGPL-3.0-or-later
"""Cross-language golden tests for canonical Router wire framing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys

from mycelium_router.contracts import ReservationResult
from mycelium_router.wire import ROUTER_WIRE_PROTOCOL, decode_frame, encode_frame


ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = ROOT / "contracts" / "router-wire-golden"
MANIFEST = ROOT / "native" / "iroh_transport" / "Cargo.toml"
RUST_OUTPUT = ROOT / "native" / "iroh_transport" / "target" / "router-wire-rust-output"
EXPECTED_TYPES = {
   "FailureReport",
   "HopHeader",
   "ManifestDelta",
   "ManifestLocked",
   "PathCancellation",
   "PrefillChunkCompleted",
   "ProgressivePrefillMessage",
   "ReservationCommitResult",
   "ReservationRequest",
   "ReservationResult",
   "TokenEvent",
}


def _index():
   return json.loads((FIXTURE_DIR / "index.json").read_text(encoding="utf-8"))


def _sha256(value: bytes) -> str:
   return hashlib.sha256(value).hexdigest()


def _fixture_hashes() -> dict[str, str]:
   return {
      path.name: _sha256(path.read_bytes())
      for path in sorted(FIXTURE_DIR.iterdir())
      if path.is_file()
   }


def test_python_goldens_are_complete_canonical_and_self_consistent():
   index = _index()
   assert index["schema"] == "mycelium.router_wire.golden.v1"
   assert index["protocol"] == ROUTER_WIRE_PROTOCOL
   assert len(index["frames"]) == 11
   assert {entry["message_type"] for entry in index["frames"]} == EXPECTED_TYPES
   assert any(entry["payload_length"] > 0 for entry in index["frames"])

   for entry in index["frames"]:
      frame = (FIXTURE_DIR / entry["file"]).read_bytes()
      header_length = struct.unpack(">I", frame[:4])[0]
      header = frame[4 : 4 + header_length]
      envelope = json.loads(header)
      canonical = json.dumps(
         envelope,
         sort_keys=True,
         separators=(",", ":"),
         allow_nan=False,
      ).encode("utf-8")
      assert header == canonical
      assert len(frame) == entry["byte_length"]
      assert _sha256(frame) == entry["frame_sha256"]

      decoded = decode_frame(frame)
      assert type(decoded.message).__name__ == entry["message_type"]
      assert len(decoded.payload) == entry["payload_length"]
      assert _sha256(decoded.payload) == entry["payload_sha256"]
      assert encode_frame(decoded.message, decoded.payload) == frame


def test_golden_generation_is_deterministic_across_repeated_runs():
   before = _fixture_hashes()
   for _ in range(2):
      completed = subprocess.run(
         [sys.executable, "scripts/generate_router_wire_golden.py"],
         cwd=ROOT,
         check=True,
         capture_output=True,
         text=True,
      )
      assert completed.stdout == "generated 11 Router wire golden frames\n"
   assert _fixture_hashes() == before


def test_python_decodes_every_rust_produced_frame():
   subprocess.run(
      [
         "cargo",
         "test",
         "--manifest-path",
         str(MANIFEST),
         "--test",
         "router_wire_golden",
         "rust_reencoded_frames_are_emitted_for_python",
      ],
      cwd=ROOT,
      check=True,
      capture_output=True,
      text=True,
   )

   index = _index()
   expected_outputs = {entry["file"] for entry in index["frames"]}
   expected_outputs.add("rust-created-reservation-result.bin")
   assert {path.name for path in RUST_OUTPUT.glob("*.bin")} == expected_outputs
   for entry in index["frames"]:
      rust_frame = (RUST_OUTPUT / entry["file"]).read_bytes()
      decoded = decode_frame(rust_frame)
      assert type(decoded.message).__name__ == entry["message_type"]
      assert _sha256(decoded.payload) == entry["payload_sha256"]
      assert rust_frame == (FIXTURE_DIR / entry["file"]).read_bytes()

   rust_created = decode_frame(
      (RUST_OUTPUT / "rust-created-reservation-result.bin").read_bytes()
   )
   assert rust_created.message == ReservationResult(
      accepted=True,
      reservation_id="rust-created-réservation-🌙",
      reason="",
      deployment_epoch=11,
      expires_at=1e-7,
   )
   assert rust_created.payload == b"\x00rust-produced\xff"


# --- PV29 L2: C-consumable MYAC activation vectors --------------------------------

import shutil  # noqa: E402

import pytest  # noqa: E402

from mycelium_router.payloads import PayloadError, decode_activation  # noqa: E402

ACT_DIR = ROOT / "contracts" / "router-activation-golden"
ACT_SCHEMA = "mycelium.router_activation.golden.v1"
#: sha256 of the router goldens as shipped before the activation vectors were
#: added: the activation vectors must never change them.
_PINNED_ROUTER_GOLDEN_SHA256 = {
   "01-hop-header.bin": "70d053d3619fb082f6ef0247f4fcb450563fa37d86a305d3a62f129022fe0000",
   "02-manifest-delta.bin": "0a7bf4f6ffde665216e06284b26e68c33fc9cb4a081ecf57bc685bad26641acf",
   "03-manifest-locked.bin": "062a709f0629f48fd9706c11639409251592669e1faaa2f3b1782f73bfe5d00a",
   "04-progressive-prefill-message.bin": "db21d60d9c088da48b3d34af599683da8605467ddfb2169591f3855c88ba4834",
   "05-prefill-chunk-completed.bin": "26b3ff536feaeecbaddd2a0c5cc854fc225dc0f58dc29a05642b9d4adb82a5e1",
   "06-reservation-request.bin": "678cb44d94304c5d8b55a23abcf6c51ceb92e02b48d6ed80d200fd9fa02e6abf",
   "07-reservation-result.bin": "a03d01762424c9afaba54bbf46afa8e62fb4465435502b487ed20b9d6d95bad8",
   "08-reservation-commit-result.bin": "93dc488346441f57a34baed8b57623701e1c099d7e7e51532a41c891b2824e70",
   "09-token-event.bin": "2ac457091548521169eb257bdd5150cba1b0ef23f2ba00ba545b117f97a6a5c0",
   "10-failure-report.bin": "0a214200cbe621521fb5c4ba3b84793a1c4b601bdb0a5813e913a58309f493de",
   "11-path-cancellation.bin": "32f980210b77bfada8c402cd33efc5c67c2f044685307d31cc4d90c9510a15a8",
   "index.json": "ab1f0a8f57629834d1e4b039d1e62204b8d711c277f2125a3e6b374a24633e1b",
}


def _act_index():
   return json.loads((ACT_DIR / "index.json").read_text(encoding="utf-8"))


def _act_hashes() -> dict[str, str]:
   return {
      path.name: _sha256(path.read_bytes())
      for path in sorted(ACT_DIR.iterdir())
      if path.is_file()
   }


def test_existing_router_goldens_are_byte_identical_to_the_pinned_set():
   assert _fixture_hashes() == _PINNED_ROUTER_GOLDEN_SHA256


def test_activation_goldens_cover_every_dtype_rank_and_reject_class():
   index = _act_index()
   assert index["schema"] == ACT_SCHEMA
   assert index["protocol"] == "MYAC/1"
   valid = [entry for entry in index["vectors"] if entry["kind"] == "valid"]
   reject = [entry for entry in index["vectors"] if entry["kind"] == "reject"]
   assert {entry["dtype"] for entry in valid} == {"float16", "bfloat16", "float32"}
   assert {len(entry["shape"]) for entry in valid} >= {1, 2, 3, 8}
   assert {entry["expected_error"] for entry in reject} == {
      "truncated_activation_payload",
      "invalid_activation_payload_magic",
      "unknown_activation_payload_version",
      "unknown_activation_dtype_code",
      "unsupported_activation_byte_order",
      "invalid_activation_rank",
      "truncated_activation_shape",
      "activation_payload_too_large",
      "activation_payload_length_mismatch",
      "invalid_activation_dimension",
      "activation_data_length_mismatch",
   }
   assert [entry["file"] for entry in index["vectors"]] == sorted(
      entry["file"] for entry in index["vectors"]
   )
   assert {path.name for path in ACT_DIR.glob("*.bin")} == {
      entry["file"] for entry in index["vectors"]
   }


def test_activation_valid_vectors_decode_to_the_recorded_elements():
   for entry in _act_index()["vectors"]:
      if entry["kind"] != "valid":
         continue
      frame = (ACT_DIR / entry["file"]).read_bytes()
      assert len(frame) == entry["byte_length"]
      assert _sha256(frame) == entry["frame_sha256"]
      envelope = decode_activation(frame)
      assert envelope.dtype == entry["dtype"]
      assert list(envelope.shape) == entry["shape"]
      assert envelope.data.hex() == entry["data_hex"]
      assert entry["dtype_code"] == {"float16": 1, "bfloat16": 2, "float32": 3}[entry["dtype"]]
      from mycelium_router.payloads import encode_activation

      assert encode_activation(envelope) == frame


def test_activation_valid_vectors_include_format_extremes():
   by_file = {entry["file"]: entry for entry in _act_index()["vectors"]}
   f16 = bytes.fromhex(by_file["02-f16-1x2x4.bin"]["data_hex"])
   assert f16[8:10] == (0x3C00).to_bytes(2, "little")  # 1.0
   assert (0x7BFF).to_bytes(2, "little") in {f16[i : i + 2] for i in range(0, len(f16), 2)}
   bf16 = bytes.fromhex(by_file["03-bf16-1x2x4.bin"]["data_hex"])
   assert (0x7F7F).to_bytes(2, "little") in {bf16[i : i + 2] for i in range(0, len(bf16), 2)}


def test_activation_reject_vectors_fail_with_the_recorded_code():
   for entry in _act_index()["vectors"]:
      if entry["kind"] != "reject":
         continue
      frame = (ACT_DIR / entry["file"]).read_bytes()
      assert _sha256(frame) == entry["frame_sha256"]
      with pytest.raises(PayloadError) as raised:
         decode_activation(frame)
      assert raised.value.code == entry["expected_error"], entry["file"]


def test_activation_vectors_tsv_is_the_line_oriented_twin_of_the_index():
   lines = (ACT_DIR / "vectors.tsv").read_text(encoding="utf-8").splitlines()
   assert lines[0] == f"# {ACT_SCHEMA}"
   rows = [line.split("\t") for line in lines[1:] if line and not line.startswith("#")]
   expected = []
   for entry in _act_index()["vectors"]:
      if entry["kind"] == "valid":
         expected.append(
            [
               "valid",
               entry["file"],
               str(entry["dtype_code"]),
               str(len(entry["shape"])),
               ",".join(str(dim) for dim in entry["shape"]),
               entry["data_hex"],
            ]
         )
      else:
         expected.append(["reject", entry["file"], entry["expected_error"]])
   assert rows == expected


def test_activation_golden_generation_is_deterministic_and_leaves_stdout_alone():
   before = _act_hashes()
   router_before = _fixture_hashes()
   for _ in range(2):
      completed = subprocess.run(
         [sys.executable, "scripts/generate_router_wire_golden.py"],
         cwd=ROOT,
         check=True,
         capture_output=True,
         text=True,
      )
      assert completed.stdout == "generated 11 Router wire golden frames\n"
   assert _act_hashes() == before
   assert _fixture_hashes() == router_before == _PINNED_ROUTER_GOLDEN_SHA256


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler on this host")
def test_a_c99_decoder_consumes_every_activation_vector(tmp_path):
   binary = tmp_path / "myac_decode_check"
   subprocess.run(
      [
         "cc",
         "-std=c99",
         "-Wall",
         "-Wextra",
         "-Werror",
         "-O1",
         "-o",
         str(binary),
         str(ACT_DIR / "myac_decode_check.c"),
      ],
      check=True,
      capture_output=True,
      text=True,
   )
   completed = subprocess.run(
      [str(binary), str(ACT_DIR)], check=True, capture_output=True, text=True
   )
   index = _act_index()["vectors"]
   valid = sum(1 for entry in index if entry["kind"] == "valid")
   reject = len(index) - valid
   assert completed.stdout == f"ok valid={valid} reject={reject}\n"
