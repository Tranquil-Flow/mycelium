# SPDX-License-Identifier: AGPL-3.0-only
"""The node keeps reading its native sidecar's stdout and stderr.

``NativeSidecarProcess`` opens both streams as pipes and read only the
``ready`` line.  The sidecar keeps writing after that: ``address_changed``
lines on stdout whenever its advertised address changes (relay home switches
on a flapping hotspot) and one JSON line on stderr per transport event
(``transport_connection_opened`` per reconnect, ``peer_connect_deadline_
exceeded`` per failed dial).  Nobody drained them, so after a pipe buffer
(16-64 KiB) of such lines the sidecar's next ``println!``/``eprintln!``
blocks - inside its outbound worker for the connection events - and every
outbound frame stops: the same "nothing moves" signature as A14 class D, on
any long-lived node with a flapping link.

The fix drains both streams for the sidecar's lifetime and forwards each
transport event to the node's own stderr as one allowlisted line (event name
and connection generation only - never endpoint ids or addresses), so a
peer's sidecar events reach the per-node stderr tails the route prints when
startup qualification fails.

The sidecar here is the real binary behind a wrapper that floods both streams
after ``ready`` the way a flapping link would.
"""

from __future__ import annotations

from pathlib import Path
import secrets
import shutil
import stat
import time

import pytest

from physical_inference_node import NativeSidecarProcess
from test_iroh_sidecar_cross_language import BINARY


pytestmark = pytest.mark.skipif(
    not BINARY.is_file(),
    reason="native sidecar binary not built (native/iroh_transport/target/debug)",
)

LINES = 3000  # ~300 KiB per stream: far beyond any pipe buffer


def _wrapper(root: Path) -> tuple[Path, Path, Path, Path]:
    stderr_done = root / "stderr-done"
    stdout_done = root / "stdout-done"
    go = root / "go"
    script = root / "sidecar-wrapper.sh"
    endpoint = "e" * 64
    # Both floods start only once the test saw start() return, so the ready
    # line and the start-up handshake are never what is measured.
    wait_go = f"  while [ ! -e '{go}' ]; do sleep 0.05; done\n"
    script.write_text(
        "#!/bin/sh\n"
        "(\n"
        + wait_go
        + "  i=0\n"
        f"  while [ $i -lt {LINES} ]; do\n"
        f"    echo '{{\"event\":\"peer_connect_deadline_exceeded\","
        f"\"remote_endpoint_id\":\"{endpoint}\",\"connection_generation\":3}}'\n"
        "    i=$((i+1))\n"
        "  done >&2\n"
        "  echo 'thread main panicked at secret 10.0.0.7:1234' >&2\n"
        f"  touch '{stderr_done}'\n"
        ") &\n"
        "(\n"
        + wait_go
        + "  i=0\n"
        f"  while [ $i -lt {LINES} ]; do\n"
        f"    echo '{{\"event\":\"address_changed\",\"endpoint_id\":\"{endpoint}\","
        "\"endpoint_addr\":{\"addrs\":[\"192.0.2.1:9\"]}}'\n"
        "    i=$((i+1))\n"
        "  done\n"
        f"  touch '{stdout_done}'\n"
        ") &\n"
        f"exec '{BINARY}' \"$@\"\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script, stderr_done, stdout_done, go


def _wait(path: Path, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.05)
    return path.exists()


def test_sidecar_streams_are_drained_and_events_forwarded_without_identifiers(
    capfd: pytest.CaptureFixture[str],
) -> None:
    root = Path(f"/private/tmp/mns-{secrets.token_hex(4)}")
    root.mkdir(mode=0o700)
    sidecar = None
    try:
        script, stderr_done, stdout_done, go = _wrapper(root)
        socket_root = root / "socket"
        sidecar = NativeSidecarProcess(
            binary=script,
            socket_root=socket_root,
            local_only=True,
            queue_capacity=4,
            startup_timeout=20.0,
        )
        sidecar.start()
        go.touch()

        # Both floods complete only if the node keeps reading both pipes.
        assert _wait(stderr_done, 20.0), "sidecar stderr was never drained"
        assert _wait(stdout_done, 20.0), "sidecar stdout was never drained"
        assert sidecar.status()["alive"] is True

        time.sleep(0.5)
        forwarded = [
            line
            for line in capfd.readouterr().err.splitlines()
            if line.startswith("MYCELIUM-SIDECAR-EVENT ")
        ]
        assert forwarded, "no sidecar transport event reached the node's stderr"
        assert (
            "MYCELIUM-SIDECAR-EVENT event=peer_connect_deadline_exceeded "
            "connection_generation=3"
        ) in forwarded
        assert "MYCELIUM-SIDECAR-EVENT event=address_changed" in forwarded
        text = "\n".join(forwarded)
        assert "e" * 64 not in text
        assert "192.0.2.1" not in text
        assert "10.0.0.7" not in text
        # Bounded: a flood is summarised, never copied line for line.
        assert len(forwarded) < 2 * LINES / 10
    finally:
        if sidecar is not None:
            sidecar.close()
        shutil.rmtree(root, ignore_errors=True)
