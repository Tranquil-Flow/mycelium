"""The operator RPC layer must tell a BUSY app from a DEAD one.

Two physical attempts were lost to getting that distinction wrong (2026-09-16,
`runs/wp8-02-useful-two-mac/ERRATA-1.md` and `RESULT-2.md`):

* 63r1: the app was **dead** (`native-control.json` unlinked on shutdown) and the chain reported
  a compile problem - so absence is now `surface_lost`, named and fail-closed.
* 63r2: the app was **alive but busy** finishing its 7B compile and did not answer the native
  session within `app_rpc.py`'s fixed 10 s handshake - so a handshake timeout is now retried,
  bounded and logged, while the API call itself is never repeated (a POST may be non-idempotent).
"""

import importlib
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

OPERATOR = Path(__file__).resolve().parents[2] / "scripts" / "a13_operator"


@pytest.fixture()
def rpc(tmp_path, monkeypatch):
    monkeypatch.setenv("A13_OWNER_ROOT", str(tmp_path / "owner"))
    monkeypatch.setenv("A13_REMOTE_ROOT", str(tmp_path / "remote"))
    monkeypatch.setenv("A13_TARGET_PREVIEW", "preview63")
    monkeypatch.setenv("A13_RPC_ROUND", "rpc-round63")
    monkeypatch.setenv("A13_HANDSHAKE_DEADLINE_S", "1")
    monkeypatch.setenv("A13_HANDSHAKE_RETRY_SLEEP_S", "0.01")
    monkeypatch.syspath_prepend(str(OPERATOR))
    sys.modules.pop("round14_rpc", None)
    module = importlib.import_module("round14_rpc")
    yield module
    sys.modules.pop("round14_rpc", None)


def _surface(root):
    path = Path(root) / "native-control.json"
    path.write_text(json.dumps({"port": 50000, "credential": "x"}))
    path.chmod(0o600)
    return path


def test_busy_app_is_retried_then_used(rpc, monkeypatch):
    _surface(rpc.OWNER)
    seen = {"n": 0}

    class Flaky:
        def __init__(self, root):
            seen["n"] += 1
            if seen["n"] < 3:
                raise TimeoutError("timed out")
            self.root = root

        def call(self, path, body=None):
            return {"ok": path}

    monkeypatch.setattr(rpc, "AppClient", Flaky)
    assert rpc.owner("/api/serving/status") == {"ok": "/api/serving/status"}
    assert seen["n"] == 3, "the handshake must be retried, not abandoned"


def test_persistently_busy_app_fails_by_name(rpc, monkeypatch):
    _surface(rpc.OWNER)

    class AlwaysBusy:
        def __init__(self, root):
            raise socket.timeout("timed out")

    monkeypatch.setattr(rpc, "AppClient", AlwaysBusy)
    with pytest.raises(rpc.HandshakeTimeout) as excinfo:
        rpc.owner("/api/serving/status")
    assert "handshake_timeout" in str(excinfo.value)
    assert str(rpc.OWNER) in str(excinfo.value)


def test_missing_surface_still_fails_closed_as_surface_lost(rpc):
    with pytest.raises(RuntimeError) as excinfo:
        rpc.owner("/api/serving/status")
    assert "surface_lost" in str(excinfo.value)


def test_remote_timeout_is_its_own_class_and_other_errors_are_not(rpc, monkeypatch):
    def run_timeout(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "TimeoutError: timed out")

    monkeypatch.setattr(rpc.subprocess, "run", run_timeout)
    with pytest.raises(rpc.HandshakeTimeout):
        rpc._target_once("/api/status")

    def run_other(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "ValueError: something_else")

    monkeypatch.setattr(rpc.subprocess, "run", run_other)
    with pytest.raises(RuntimeError) as excinfo:
        rpc._target_once("/api/status")
    assert not isinstance(excinfo.value, rpc.HandshakeTimeout), (
        "a real API failure must not be retried as if it were a handshake timeout"
    )
