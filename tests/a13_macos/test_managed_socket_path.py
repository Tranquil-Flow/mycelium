# SPDX-License-Identifier: AGPL-3.0-only
"""Long installed state paths must still permit a real macOS Unix socket."""
import os
import socket
import stat
from typing import Any
from test_control_pump import _world
from test_managed_runtime import runtime as make_runtime


def test_managed_runtime_uses_short_private_socket_path(tmp_path):
    long_root=tmp_path/('long-app-state-'*9)
    with _world(tmp_path/'world') as w:
        runtime=make_runtime(long_root,w)
        parent=runtime.worker.socket_root.parent
        assert runtime.worker.capacity_root==runtime.root
        try:
            path=runtime.worker.socket_root/'i.sock'
            assert stat.S_IMODE(parent.stat().st_mode)==0o700
            runtime.worker.socket_root.mkdir()
            sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
            try:
                sock.bind(str(path))
                assert len(os.fsencode(path))<100
            finally:
                sock.close();path.unlink(missing_ok=True);runtime.worker.socket_root.rmdir()
        finally:
            runtime.close()
        assert not parent.exists()
        assert runtime.root.exists(), 'durable control state must survive socket cleanup'


def test_explicit_capacity_root_is_private_and_separate(tmp_path):
    import pytest
    from physical_inference_node import PhysicalNodeService, NodeCommandError
    with _world(tmp_path/'world') as w:
        runtime=make_runtime(tmp_path/'runtime',w)
        try:
            kwargs: dict[str, Any]=dict(run_id=runtime.run_id,deployment_id=runtime.deployment_id,
                node_id=runtime.worker.node_id,artifact_root=runtime.worker.artifact_root,
                socket_root=runtime.worker.socket_root,sidecar_binary=runtime.worker.sidecar_binary,
                sidecar_local_only=True,command_timeout=5,capacity_root=runtime.root)
            node=PhysicalNodeService(**kwargs)
            try: assert node.capacity_root==runtime.root
            finally: node.close()
            runtime.root.chmod(0o755)
            try:
                with pytest.raises(NodeCommandError,match='invalid_capacity_root'):
                    PhysicalNodeService(**kwargs)
            finally: runtime.root.chmod(0o700)
        finally: runtime.close()
