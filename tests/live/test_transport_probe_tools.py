from __future__ import annotations

import subprocess
import sys

from mycelium_router.contracts import TokenEvent
from mycelium_router.wire import decode_frame
from scripts.build_transport_probe_frame import build_probe_frame


def test_probe_frame_is_a_valid_bounded_router_token_event() -> None:
    encoded = build_probe_frame()

    decoded = decode_frame(encoded)

    assert isinstance(decoded.message, TokenEvent)
    assert decoded.message.request_id == "a2-fresh-member-transport-probe"
    assert decoded.payload == b""
    assert len(encoded) < 1_024


def test_probe_sidecar_rejects_unbounded_runtime_before_starting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/run_transport_probe_sidecar.py",
            "--binary",
            "/does/not/matter",
            "--socket-root",
            "/does/not/matter",
            "--bootstrap-secret-file",
            "/does/not/matter",
            "--endpoint-secret-file",
            "/does/not/matter",
            "--max-runtime-seconds",
            "3601",
        ],
        capture_output=True,
        check=False,
    )

    assert completed.returncode != 0
    assert b"transport_probe_max_runtime_invalid" in completed.stderr
