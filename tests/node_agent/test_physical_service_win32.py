# SPDX-License-Identifier: AGPL-3.0-only
"""The physical node service on a simulated win32 host fails closed.

Covers the fail-open ``_command_deadline`` fix (a real deadline where
``signal.setitimer`` does not exist), the win32 memory probes, the
handle-bound control-document and artifact walks, the owner-only capacity
root, the graceful-stop event channel and the runner lock refusal.
"""

from __future__ import annotations

import sys
import _thread
import hashlib
import os
from pathlib import Path, PurePosixPath
import signal
import threading
import time

import pytest

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError, _win32_api as w
from mycelium_physical_runner import lock as runner_lock
from mycelium_physical_runner.errors import RunnerError
import physical_inference_node as node
import runtime_loader
import stage_pack

from tests.host_platform.fake_win32 import inherited_profile_acl, simulate_win32


@pytest.fixture
def fake(monkeypatch):
    return simulate_win32(monkeypatch)


def _spin(seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        time.sleep(0.005)


# ---------------------------------------------------------- command deadline


def test_the_win32_command_deadline_fires(fake) -> None:
    started = time.monotonic()
    with pytest.raises(node.NodeCommandError) as caught:
        with node._command_deadline(0.1):
            _spin(20.0)
    assert caught.value.code == "command_timeout"
    assert time.monotonic() - started < 15.0


def test_the_win32_command_deadline_restores_the_sigint_handler(fake) -> None:
    before = signal.getsignal(signal.SIGINT)
    with node._command_deadline(5.0):
        assert signal.getsignal(signal.SIGINT) is not before
    assert signal.getsignal(signal.SIGINT) is before
    with pytest.raises(node.NodeCommandError):
        with node._command_deadline(0.05):
            _spin(20.0)
    assert signal.getsignal(signal.SIGINT) is before


def test_a_finished_block_is_never_interrupted_later(fake) -> None:
    with node._command_deadline(0.2):
        pass
    _spin(0.6)  # the cancelled timer must not fire into unrelated code


@pytest.fixture
def sigint_disposition():
    """Pin the SIGINT handler the deadline block finds, restoring it afterwards.

    A real Ctrl+C is delegated to whatever handler was in place before the
    block, so these tests must not depend on how the test process was launched:
    a runner started as a background job of a non-interactive shell inherits
    SIGINT as SIG_IGN, and Python then never installs ``default_int_handler``.
    """

    inherited = signal.getsignal(signal.SIGINT)

    def install(handler: object) -> None:
        signal.signal(signal.SIGINT, handler)

    try:
        yield install
    finally:
        signal.signal(signal.SIGINT, inherited)


def test_a_real_ctrl_c_is_not_reported_as_a_timeout(fake, sigint_disposition) -> None:
    sigint_disposition(signal.default_int_handler)
    with pytest.raises(KeyboardInterrupt):
        with node._command_deadline(30.0):
            _thread.interrupt_main(signal.SIGINT)
            _spin(20.0)


def test_an_inherited_ignored_sigint_stays_ignored(fake, sigint_disposition) -> None:
    sigint_disposition(signal.SIG_IGN)
    with node._command_deadline(30.0):
        _thread.interrupt_main(signal.SIGINT)
        _spin(0.2)
    assert signal.getsignal(signal.SIGINT) == signal.SIG_IGN


def test_the_win32_deadline_refuses_off_the_main_thread(fake) -> None:
    outcome: list[str] = []

    def worker() -> None:
        try:
            with node._command_deadline(1.0):
                outcome.append("ran")
        except node.NodeCommandError as exc:
            outcome.append(exc.code)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert outcome == ["command_deadline_unavailable"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX deadline path: this host is win32")
def test_a_host_without_setitimer_refuses_instead_of_running_unbounded(monkeypatch) -> None:
    assert mycelium_host_platform.is_win32() is False
    monkeypatch.delattr(node.signal, "setitimer")
    ran: list[bool] = []
    with pytest.raises(node.NodeCommandError) as caught:
        with node._command_deadline(1.0):
            ran.append(True)
    assert caught.value.code == "command_deadline_unavailable"
    assert ran == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX deadline path: no setitimer on win32")
def test_the_posix_deadline_still_uses_setitimer() -> None:
    assert hasattr(signal, "setitimer")
    with pytest.raises(node.NodeCommandError) as caught:
        with node._command_deadline(0.05):
            _spin(20.0)
    assert caught.value.code == "command_timeout"


# ------------------------------------------------------------ memory probes


def test_win32_memory_probes_use_the_process_and_system_counters(fake) -> None:
    assert node._process_rss_bytes() == fake.working_set_bytes
    assert node._host_available_memory_bytes() == fake.available_bytes


def test_win32_memory_probe_failures_are_not_reported_as_zero(fake) -> None:
    fake.fail_next("process_working_set_bytes", w.ERROR_ACCESS_DENIED)
    with pytest.raises(HostPlatformError):
        node._process_rss_bytes()
    fake.working_set_bytes = 0
    with pytest.raises(HostPlatformError):
        node._process_rss_bytes()


# ---------------------------------------------------- documents and roots


def _service(root: Path) -> node.PhysicalNodeService:
    service = object.__new__(node.PhysicalNodeService)
    service.artifact_root = root
    return service


def test_win32_control_documents_are_read_through_handles(tmp_path, fake) -> None:
    root = tmp_path / "artifacts"
    (root / "plans").mkdir(parents=True)
    (root / "plans" / "doc.json").write_bytes(b'{"a":1}')
    service = _service(root)
    assert service._safe_document("plans/doc.json", "invalid_doc") == {"a": 1}
    assert any(call[0] == "handle_to_fd" for call in fake.calls)

    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "doc.json").write_bytes(b'{"b":2}')
    (root / "linked").symlink_to(tmp_path / "outside", target_is_directory=True)
    for relative in ("linked/doc.json", "plans/missing.json", "../outside/doc.json"):
        with pytest.raises(node.NodeCommandError) as caught:
            service._safe_document(relative, "invalid_doc")
        assert caught.value.code == "invalid_doc"
    os.link(root / "plans" / "doc.json", root / "plans" / "second.json")
    with pytest.raises(node.NodeCommandError):
        service._safe_document("plans/doc.json", "invalid_doc")
    assert fake.handles == {}


def test_win32_capacity_root_must_be_owner_only(tmp_path, fake) -> None:
    owned = tmp_path / "owned"
    mycelium_host_platform.private_directory(owned, create=True).close()
    assert node._win32_private_root(owned) is True
    widened = tmp_path / "widened"
    widened.mkdir()
    fake.set_security(widened, inherited_profile_acl())
    assert node._win32_private_root(widened) is False
    assert node._win32_private_root(Path("relative")) is False
    assert node._win32_private_root(tmp_path / "absent") is False


def test_the_cancel_diagnostic_is_skipped_without_no_follow(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "diagnostic.log"
    monkeypatch.setenv(node._CANCEL_PHASE_DIAGNOSTIC_ENV, str(destination))
    monkeypatch.delattr(node.os, "O_NOFOLLOW", raising=False)  # absent on Windows
    node._cancel_phase_diagnostic("phase", 1.0, call_index=1)
    assert not destination.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX O_NOFOLLOW diagnostic writer: absent on win32")
def test_the_cancel_diagnostic_still_writes_on_posix(tmp_path, monkeypatch) -> None:
    destination = tmp_path / "diagnostic.log"
    monkeypatch.setenv(node._CANCEL_PHASE_DIAGNOSTIC_ENV, str(destination))
    node._cancel_phase_diagnostic("phase", 1.0, call_index=1)
    assert b"mycelium.cancel_phase_diagnostic.v1" in destination.read_bytes()


# ---------------------------------------------------- stage pack / loader


def test_stage_pack_opens_artifacts_through_the_win32_seam(tmp_path, fake) -> None:
    root = tmp_path / "pack"
    (root / "a").mkdir(parents=True)
    (root / "a" / "w.bin").write_bytes(b"weights")
    (root / "b").symlink_to(root / "a", target_is_directory=True)
    with stage_pack._open_beneath(root, PurePosixPath("a/w.bin")) as handle:
        assert handle.read() == b"weights"
    with pytest.raises(ValueError, match="symlink forbidden"):
        stage_pack._open_beneath(root, PurePosixPath("b/w.bin"))
    assert fake.handles == {}


def test_runtime_loader_verifies_artifacts_through_the_win32_seam(tmp_path, fake) -> None:
    root = tmp_path / "cache"
    (root / "m").mkdir(parents=True)
    payload = b"tensor-bytes"
    (root / "m" / "x.safetensors").write_bytes(payload)
    record = {
        "size_bytes": len(payload),
        "content_digest": "sha256:" + hashlib.sha256(payload).hexdigest(),
    }
    handle, fingerprint = runtime_loader._win32_open_verified_artifact(
        root, PurePosixPath("m/x.safetensors"), record
    )
    try:
        assert handle.read() == payload
        assert fingerprint[3] == len(payload)
    finally:
        handle.close()
    with pytest.raises(runtime_loader.RuntimeLoadError, match="digest mismatch"):
        runtime_loader._win32_open_verified_artifact(
            root,
            PurePosixPath("m/x.safetensors"),
            {**record, "content_digest": "sha256:" + "0" * 64},
        )
    (root / "n").symlink_to(root / "m", target_is_directory=True)
    with pytest.raises(runtime_loader.RuntimeLoadError, match="unable to open"):
        runtime_loader._win32_open_verified_artifact(
            root, PurePosixPath("n/x.safetensors"), record
        )
    assert fake.handles == {}


# ------------------------------------------------------- stop event / lock


def test_the_stop_event_channel_reaches_the_stop_callback(fake, monkeypatch) -> None:
    event = fake.create_named_event("Local\\mycelium-node-stop")
    stopped = threading.Event()
    watcher = mycelium_host_platform.start_stop_event_watcher(
        stopped.set, environ={"MYCELIUM_STOP_EVENT": "Local\\mycelium-node-stop"}
    )
    assert watcher is not None
    assert not stopped.wait(0.3)
    event.set()
    assert stopped.wait(10)


def test_the_stop_event_channel_is_optional_but_never_ignored(fake) -> None:
    assert mycelium_host_platform.start_stop_event_watcher(lambda: None, environ={}) is None
    with pytest.raises(HostPlatformError) as caught:
        mycelium_host_platform.start_stop_event_watcher(
            lambda: None, environ={"MYCELIUM_STOP_EVENT": "Local\\missing"}
        )
    assert caught.value.code == "host_platform_unavailable"


def test_a_cancelled_watcher_never_calls_back(fake) -> None:
    event = fake.create_named_event("Local\\cancelled")
    calls: list[int] = []
    cancel = mycelium_host_platform.start_stop_event_watcher(
        lambda: calls.append(1), environ={"MYCELIUM_STOP_EVENT": "Local\\cancelled"}
    )
    cancel.set()
    event.set()
    time.sleep(0.6)
    assert calls == []


def test_the_runner_lock_refuses_on_a_host_without_fcntl(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_lock, "fcntl", None)
    lock = runner_lock.ExclusiveLock(tmp_path / "private" / "runner.lock")
    with pytest.raises(RunnerError) as caught:
        lock.acquire()
    assert caught.value.code == "lock_unavailable_on_host"
    assert lock.held is False
    assert not (tmp_path / "private").exists()

