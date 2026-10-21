# SPDX-License-Identifier: AGPL-3.0-only
"""Invoked by the native ControlCall integration test, not a mocked sidecar."""
import json
from pathlib import Path
import select
import subprocess
import sys
import tempfile

from mycelium_iroh_sidecar import SidecarClient, SidecarError


def main():
    binary, address_json = sys.argv[1:]
    address = json.loads(address_json)
    secret = b't' * 32  # Synthetic per-test local authentication, never real credentials.
    with tempfile.TemporaryDirectory(prefix='control-e2e-', dir='/tmp') as root:
        socket_path = Path(root) / 'run' / 'sidecar.sock'
        child = subprocess.Popen([binary, '--uds', str(socket_path), '--bootstrap-fd', '0', '--local-only'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert child.stdin is not None and child.stdout is not None and child.stderr is not None
        try:
            child.stdin.write(secret)
            child.stdin.close()
            assert select.select([child.stdout], [], [], 10)[0], 'sidecar readiness timeout'
            ready = json.loads(child.stdout.readline())
            assert ready['event'] == 'ready'
            with SidecarClient(socket_path, secret, timeout=3) as client:
                assert client.endpoint_id == ready['endpoint_id']
                try:
                    client.control_call(peer_endpoint_id=address['id'], request_json='{}', timeout_seconds=1)
                except SidecarError as error:
                    assert error.code == 'unknown_peer'
                else:
                    raise AssertionError('unconfigured control peer admitted')
                client.configure_peer(address['id'], address, generation=1)
                for index in range(2):
                    command = json.dumps({'command': 'health', 'command_id': str(index)}, separators=(',', ':'))
                    result = client.control_call(peer_endpoint_id=address['id'], request_json=command, timeout_seconds=2)
                    assert result == {'peer_endpoint_id': address['id'], 'response_json': '{"ok":true}'}
                client.ping()  # ControlCall did not desynchronize authenticated IPC.
                print(json.dumps({'controller_endpoint_id': client.endpoint_id, 'calls': 2}), flush=True)
                # Keep the endpoint alive until the Rust peer witnesses both
                # per-call closes. Killing the sidecar here can discard the
                # final QUIC close datagram before the peer has observed it.
                assert select.select([sys.stdin], [], [], 10)[0], 'peer cleanup witness timeout'
                assert sys.stdin.buffer.read(1) == b'd', 'peer cleanup was not witnessed'
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
            child.stdout.close()
            child.stderr.close()


if __name__ == '__main__':
    main()
