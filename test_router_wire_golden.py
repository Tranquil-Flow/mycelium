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
