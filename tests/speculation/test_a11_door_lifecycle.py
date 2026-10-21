# SPDX-License-Identifier: AGPL-3.0-only
"""Lifetime and ambiguous-header regressions at the actual A11 HTTP listener."""
import http.client
import socket

import pytest

from mycelium_a11_evidence_control import (
    A11EvidenceControl, EXACT_FLEET_GRANT, GRANT_HEADER, TOKEN_HEADER, RouteA11Evidence,
)


def door(tmp_path, *, port=0):
    private = tmp_path / 'private'
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / 'evidence.token'
    return A11EvidenceControl(grant=EXACT_FLEET_GRANT, source=RouteA11Evidence(object()),
                              token_file=path, product_port=8787, port=port), path


def test_bind_failure_leaves_no_token(tmp_path):
    with socket.socket() as blocker:
        blocker.bind(('127.0.0.1', 0))
        blocker.listen(1)
        control, path = door(tmp_path, port=blocker.getsockname()[1])
        try:
            with pytest.raises(OSError):
                control.start()
            assert not path.exists()
            assert not control.status()['mounted']
        finally:
            control.close()


def test_close_preserves_a_replaced_file(tmp_path):
    control, path = door(tmp_path)
    control.start()
    # rename preserves the old inode, preventing inode reuse in this regression
    path.rename(path.with_suffix('.old'))
    path.write_bytes(b'not owned by this listener')
    control.close()
    assert path.read_bytes() == b'not owned by this listener'


@pytest.mark.parametrize('header', [GRANT_HEADER, TOKEN_HEADER])
def test_duplicate_authentication_headers_are_refused(tmp_path, header):
    control, path = door(tmp_path)
    control.start()
    connection = http.client.HTTPConnection('127.0.0.1', control.port, timeout=5)
    try:
        token = path.read_text().strip()
        connection.putrequest('GET', '/a11/evidence')
        for name, value in ((GRANT_HEADER, EXACT_FLEET_GRANT), (TOKEN_HEADER, token)):
            connection.putheader(name, value)
            if name == header:
                connection.putheader(name, value)
        connection.endheaders()
        response = connection.getresponse()
        response.read()
        assert response.status == 403
        assert control.status()['reads'] == 0
    finally:
        connection.close()
        control.close()


@pytest.mark.parametrize('failure', ['thread_start', 'token_write'])
def test_startup_failure_closes_listener_and_owned_token(tmp_path, monkeypatch, failure):
    import os
    import mycelium_a11_evidence_control as module
    control, path = door(tmp_path)
    servers, descriptors = [], []
    real_server, real_open = module._Server, os.open

    def server(*args, **kwargs):
        instance = real_server(*args, **kwargs)
        servers.append(instance)
        return instance

    def open_file(*args, **kwargs):
        descriptor = real_open(*args, **kwargs)
        descriptors.append(descriptor)
        return descriptor

    def refuse(*args, **kwargs):
        raise RuntimeError('injected_startup_failure')

    monkeypatch.setattr(module, '_Server', server)
    monkeypatch.setattr(module.os, 'open', open_file)
    if failure == 'thread_start':
        monkeypatch.setattr(module.threading.Thread, 'start', refuse)
    else:
        monkeypatch.setattr(module.os, 'fdopen', refuse)
    with pytest.raises(RuntimeError, match='injected_startup_failure'):
        control.start()
    assert not path.exists()
    assert not control.status()['mounted']
    assert control._token is None
    assert len(servers) == 1 and servers[0].socket.fileno() == -1
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)
    control.close()
