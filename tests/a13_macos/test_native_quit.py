# SPDX-License-Identifier: AGPL-3.0-only
"""Quit dispatch must request the existing cleanup, never terminate Python."""
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import mycelium_a13_macos.__main__ as launcher


@pytest.mark.parametrize('cleanup_error', [None, 'relay_stop_failed'])
def test_native_quit_enters_shared_cleanup(tmp_path, monkeypatch, capsys, cleanup_error):
    events = []

    class App:
        installation_role = 'owner'
        closed = SimpleNamespace(is_set=lambda: False)

        def __init__(self, root):
            self.root = root
            self.cleanup_error = None

        def start(self):
            events.append('start')
            return self

        def close(self):
            events.append('close')
            self.cleanup_error = cleanup_error

    class NativeQuit:
        def __init__(self, request_stop, *, headless):
            assert headless
            self.request_stop = request_stop
            events.append('install')

        def poll(self):
            events.append('appleevent')
            self.request_stop()

        def close(self):
            events.append('uninstall')

    # Insert the boundary before the module exists: baseline must fail because
    # the launcher never installs/pumps it, not because importing a stub fails.
    monkeypatch.setitem(sys.modules, 'mycelium_a13_macos.native_quit',
                        SimpleNamespace(NativeQuit=NativeQuit))
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['app', '--headless', '--state-root', str(tmp_path)])
    monkeypatch.setattr(launcher, 'preflight', lambda: {})
    monkeypatch.setattr(launcher, 'model_contract', lambda: {})
    monkeypatch.setattr(launcher, 'DemoApp', App)
    # Fail promptly on baseline; no signal, app, network or installed state used.
    import mycelium_host_platform
    class Stop:
        requested = False
        def set(self): self.requested = True
        def wait(self, timeout):
            assert 'install' in events, 'native_quit_handler_not_installed'
            return self.requested
    monkeypatch.setattr(mycelium_host_platform, 'StopRequest', Stop)
    before = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    try:
        result = launcher.main()
    finally:
        for n, handler in before.items(): signal.signal(n, handler)
    assert events == ['start', 'install', 'appleevent', 'close', 'uninstall'], capsys.readouterr().err
    assert result == (3 if cleanup_error else 0)


def test_native_bridge_installs_standard_quit_and_defers_termination():
    # A genuine shipped-source contract; dynamic native event dispatch is tested
    # separately, without sending any event to the installed application.
    source = Path(launcher.__file__).parent / 'native' / 'quit_bridge.m'
    assert source.is_file(), 'native_quit_bridge_missing'
    text = source.read_text()
    assert 'applicationShouldTerminate:' in text
    assert 'return NSTerminateCancel;' in text
    assert 'return NSTerminateLater;' not in text
    assert 'setDelegate:' in text
    assert '@selector(requestQuit:)' in text
    assert 'setEventHandler:quitDelegate' in text
    assert 'andEventID:kAEQuitApplication' in text
    assert 'nextEventMatchingMask:' in text
    assert 'exit(' not in text and '_exit(' not in text


def test_native_quit_preserves_real_app_cleanup_and_lock_release(tmp_path, monkeypatch):
    from mycelium_a13_macos.app import DemoApp
    from mycelium_a13_macos import demo_app
    from test_app import Authority
    events = []
    class TrackedAuthority(Authority):
        def stop(self):
            events.append('authority')
            super().stop()
    app = DemoApp(tmp_path, authority_factory=TrackedAuthority, check_platform=False)
    real_close_run = demo_app.close
    def close_run(value):
        events.append('run')
        return real_close_run(value)
    monkeypatch.setattr(demo_app, 'close', close_run)
    class Bridge:
        def __init__(self, callback, **kwargs): self.callback = callback
        def poll(self):
            events.append('quit')
            self.callback()
        def close(self): events.append('bridge')
    monkeypatch.setitem(sys.modules, 'mycelium_a13_macos.native_quit', SimpleNamespace(NativeQuit=Bridge))
    monkeypatch.setattr(launcher, 'DemoApp', lambda root: app)
    monkeypatch.setattr(launcher, 'preflight', lambda: {})
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'platform', 'darwin')
    monkeypatch.setattr(sys, 'argv', ['app', '--headless', '--state-root', str(tmp_path)])
    app.public_relay = SimpleNamespace(close=lambda: events.append('relay'))
    before = {n: signal.getsignal(n) for n in (signal.SIGTERM, signal.SIGINT)}
    try:
        assert launcher.main() == 0
        assert events == ['quit', 'run', 'relay', 'authority', 'bridge']
        assert app.server is None and app.lock_fd is None
        assert app.closed.is_set() and not app.running
        assert app.cleanup_error is None
        assert not app.launch_file.exists()
        assert not (tmp_path / 'native-control.json').exists()
        # Real flock reacquisition, not process-disappearance as cleanup proof.
        import fcntl
        with (tmp_path / 'application.lock').open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        app.close()
        for n, handler in before.items(): signal.signal(n, handler)
