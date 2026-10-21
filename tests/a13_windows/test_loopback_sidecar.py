# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows sidecar channel (authenticated loopback) end to end on a POSIX host.

The real sidecar is built with the ``loopback-test`` feature, which compiles the
Windows-only ``--loopback --bootstrap-stdin --allowed-client-pid`` channel on
POSIX.  The node launches it exactly as on Windows (``NativeSidecarProcess`` with
``loopback=True``) and the Python client dials the announced port.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from mycelium_iroh_sidecar import AuthenticationError, LoopbackEndpoint, SidecarClient
from physical_inference_node import NativeSidecarProcess, NodeCommandError

ROOT = Path(__file__).resolve().parents[2]
BINARY = Path(os.environ.get(
    "MYCELIUM_LOOPBACK_SIDECAR",
    ROOT / "native/iroh_transport/target/loopback-test/debug/mycelium-iroh-sidecar",
))

pytestmark = pytest.mark.skipif(not BINARY.is_file(), reason="loopback-test sidecar not built")


def _sidecar(tmp_path):
    return NativeSidecarProcess(
        binary=BINARY, socket_root=tmp_path / "s", local_only=True, queue_capacity=16,
        startup_timeout=30.0, loopback=True,
    )


def test_node_launches_the_loopback_sidecar_and_authenticates(tmp_path):
    sidecar = _sidecar(tmp_path)
    try:
        ready = sidecar.start()
        assert isinstance(sidecar.socket_path, LoopbackEndpoint)
        assert sidecar.socket_path.port == ready["local_port"]
        assert not (tmp_path / "s").exists()  # no socket root on this channel
        with SidecarClient(sidecar.socket_path, sidecar.bootstrap_material, timeout=10.0) as client:
            client.ping()
            assert client.endpoint_id == ready["endpoint_id"]
        wrong = SidecarClient(sidecar.socket_path, b"w" * 32, timeout=5.0)
        with pytest.raises(AuthenticationError):
            wrong.connect()
    finally:
        sidecar.close()
    assert sidecar.status()["started"] is False


def test_another_process_cannot_use_the_channel(tmp_path):
    sidecar = _sidecar(tmp_path)
    try:
        sidecar.start()
        code = (
            "import sys\n"
            "from mycelium_iroh_sidecar import LoopbackEndpoint, SidecarClient\n"
            f"c = SidecarClient(LoopbackEndpoint({sidecar.socket_path.port}), bytes.fromhex('{sidecar.bootstrap_material.hex()}'), timeout=5.0)\n"
            "try:\n    c.connect(); c.ping()\nexcept Exception as e:\n    print('refused', type(e).__name__); sys.exit(0)\n"
            "print('admitted'); sys.exit(1)\n"
        )
        child = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True,
                               env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=60)
        assert child.returncode == 0, child.stdout + child.stderr
        assert "refused" in child.stdout
    finally:
        sidecar.close()


def test_a_missing_binary_is_refused_before_launch(tmp_path):
    sidecar = NativeSidecarProcess(binary=tmp_path / "absent", socket_root=tmp_path / "s", local_only=True,
                                   queue_capacity=16, startup_timeout=1.0, loopback=True)
    with pytest.raises(NodeCommandError):
        sidecar.start()
