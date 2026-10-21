# SPDX-License-Identifier: AGPL-3.0-only
"""Budget/stop review regressions; real startup case never loads a model."""
import pytest
from test_worker import worker
from test_worker_concurrency import FakeConcurrentSession, attach_fake, configure_payload
from test_worker_replacement_readiness import _replacement_fixture
import time

from mycelium_a13_macos.worker import ManagedWorker, WorkerError


def _poll_dead_or_alive(process, *, deadline_seconds: float = 5.0, interval: float = 0.02) -> bool:
    """Return True iff `process` is reaped within `deadline_seconds`.

    The product's PhysicalNodeProcess.close() drives cleanup on a bounded deadline
    (stop_timeout bounded by response_timeout/0.5/remaining), so a deterministic
    test that asserts on poll() must also wait the bounded deadline — bare poll()
    is load-sensitive and races the OS reaper. Returns False on timeout so the
    caller can produce an actionable diagnostic.
    """
    end = time.monotonic() + deadline_seconds
    while time.monotonic() < end:
        if process.poll() is not None:
            return True
        time.sleep(interval)
    return process.poll() is not None



@pytest.mark.parametrize('outcome', ['success', 'unknown'])
def test_owner_stop_is_not_resurrected_by_replacement(tmp_path, monkeypatch, outcome):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    old._session.returncode = None
    original_send = old._session.send
    def stopped(**kwargs):
        response = original_send(**kwargs)
        if outcome == 'unknown':
            raise TimeoutError('stop_result_unknown')
        return response
    monkeypatch.setattr(old._session, 'send', stopped)
    try:
        if outcome == 'unknown':
            with pytest.raises(TimeoutError):
                old.send(command_id='owner-stop', command='stop', payload={})
        else:
            assert old.send(command_id='owner-stop', command='stop', payload={})['ok']
        old._session.returncode = 143
        runtime._replace_worker_if_dead()
        assert runtime.worker is old, 'replacement revived a stopped worker'
        assert session.calls == []
        assert runtime._worker_replacements == 0
        with pytest.raises(WorkerError, match='worker_owner_stopped'):
            old.send(command_id='late-inference', command='infer_start', payload={'request': {}})
    finally:
        old.close()
        new.close()


def test_replacement_commands_use_remaining_original_allowance(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    try:
        runtime._replace_worker_if_dead()
        # Two original hello commands remain charged; no automatic model replay.
        for index in range(6):
            runtime.worker.send(command_id=f'health-{index}', command='hello', payload={})
        with pytest.raises(WorkerError, match='worker_command_limit_exceeded'):
            runtime.worker.send(command_id='one-too-many', command='hello', payload={})
        assert runtime.worker.status()['command_count'] == old.limits.max_commands
    finally:
        old.close()
        new.close()


def test_expired_wall_budget_refuses_before_spawning_or_burning_attempt(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    assert old._started_at is not None
    old._started_at -= old.limits.max_wall_seconds
    spawned = []
    monkeypatch.setattr(new, 'start', lambda: spawned.append(True))
    try:
        runtime._replace_worker_if_dead()
        assert spawned == [], 'an expired authority must not spawn a fresh process'
        assert runtime._worker_replacements == 0
        assert runtime.worker is old
    finally:
        old.close()
        new.close()


def test_real_replacement_start_preserves_original_wall_deadline(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    # Do not configure/load a real model; exercise real hello/process/timer/monitor.
    old._load_admitted = False

    assert old._started_at is not None
    old._started_at -= 5
    original_start = old._started_at
    monkeypatch.setattr(new, 'start', lambda: ManagedWorker.start(new))
    try:
        before_replacement = time.monotonic()
        runtime._replace_worker_if_dead()
        assert runtime.worker is new
        assert new.pid is not None and new._monitor is not None
        assert new._started_at == original_start
        assert new._deadline_timer is not None
        assert new._deadline_timer.interval <= original_start + old.limits.max_wall_seconds - 10 - before_replacement
    finally:
        old.close()
        new.close()
    assert not new.process_group_present()


def test_configure_dispatch_uncertainty_requires_cold_restart(tmp_path, monkeypatch):
    w = worker(tmp_path, authorize_load=lambda _: True)
    session = FakeConcurrentSession()
    attach_fake(w, session)
    def unknown(**kwargs):
        raise TimeoutError('configure_result_unknown')
    monkeypatch.setattr(session, 'send', unknown)
    try:
        with pytest.raises(TimeoutError):
            w.send(command_id='configure', command='configure', payload=configure_payload())
        assert not w.status()['model_load_authorized']
        assert w.requires_cold_restart()
    finally:
        w.close()


def test_unproven_previous_cleanup_never_spawns_replacement(tmp_path, monkeypatch):
    runtime, old, new, session, _ = _replacement_fixture(tmp_path, monkeypatch)
    original_close = old.close
    monkeypatch.setattr(old, 'close', lambda: None)
    monkeypatch.setattr(old, 'process_group_present', lambda: True)
    from mycelium_a13_macos.managed_runtime import RuntimeErrorCode
    try:
        with pytest.raises(RuntimeErrorCode, match='runtime_replacement_cleanup_unproven'):
            runtime._replace_worker_if_dead()
        assert runtime.worker is old
        assert runtime._stop.is_set()
        assert not session.calls
        assert new._session is None
    finally:
        original_close()
        new.close()


def test_failed_start_consumption_survives_the_next_attempt(tmp_path, monkeypatch):
    runtime, old, failed, first_session, _ = _replacement_fixture(tmp_path, monkeypatch)
    def failed_start():
        attach_fake(failed, first_session)
        failed.send(command_id='failed-probe', command='hello', payload={})
        raise WorkerError('worker_native_start_failed')
    monkeypatch.setattr(failed, 'start', failed_start)
    next_root = tmp_path / 'third'
    next_root.mkdir()
    third = worker(next_root, authorize_load=lambda _: True)
    third_session = FakeConcurrentSession()
    monkeypatch.setattr(third, 'start', lambda: attach_fake(third, third_session))
    try:
        runtime._replace_worker_if_dead()
        assert runtime.worker is old
        monkeypatch.setattr(runtime, '_make_worker', lambda: third)
        runtime._replace_worker_if_dead()
        assert runtime.worker is third
        # Two original commands and the failed attempt's probe stay charged.
        assert third.status()['command_count'] == 3
        assert old._started_at is not None
        assert third_session.calls == []
        for index in range(5):
            third.send(command_id=f'last-{index}', command='hello', payload={})
        with pytest.raises(WorkerError, match='worker_command_limit_exceeded'):
            third.send(command_id='over-budget', command='hello', payload={})
    finally:
        old.close()
        failed.close()
        third.close()


def test_real_prehello_cleanup_failure_remains_owned(tmp_path, monkeypatch):
    from mycelium_a13_macos import worker as worker_module
    from mycelium_a13_macos.managed_runtime import RuntimeErrorCode
    runtime, old, new, _, _ = _replacement_fixture(tmp_path, monkeypatch)
    original_send = worker_module._ConcurrentNativeSession.send
    def refuse_hello(self, **kwargs):
        if kwargs['command_id'] == 'managed-hello':
            raise WorkerError('worker_native_start_failed')
        return original_send(self, **kwargs)
    def fail_teardown(self):
        raise WorkerError('worker_cleanup_failed')
    try:
        with monkeypatch.context() as patch:
            patch.setattr(new, 'start', lambda: ManagedWorker.start(new))
            patch.setattr(worker_module._ConcurrentNativeSession, 'send', refuse_hello)
            patch.setattr(worker_module._ConcurrentNativeSession, 'close', fail_teardown)
            caught = None
            try:
                runtime._replace_worker_if_dead()
            except RuntimeErrorCode as exc:
                caught = exc
            # Real no-model child exists although the managed hello ownership gate failed.
            assert new.pid is not None and new._session.returncode is None
            assert not new._group_owned
            assert caught is not None and str(caught) == 'runtime_replacement_cleanup_unproven'
            assert runtime._stop.is_set()
            assert runtime._replacement_worker is new
    finally:
        # Restore real process-bound cleanup even when the RED assertion fails.
        old.close()
        new.close()
        assert new._session is not None and new._session.returncode is not None


def test_constructor_cleanup_failure_is_not_reported_clean(tmp_path, monkeypatch):
    import mycelium_node.process as native
    from mycelium_a13_macos.managed_runtime import RuntimeErrorCode
    runtime, old, new, _, _ = _replacement_fixture(tmp_path, monkeypatch)
    original_await = native._await_target_exec
    owned = []

    def fail_after_exec(*args, **kwargs):
        original_await(*args, **kwargs)
        raise native.NodeProcessError('injected_post_exec_failure')

    def refuse_cleanup(self, *args, **kwargs):
        if self not in owned:
            owned.append(self)
        return False

    try:
        with monkeypatch.context() as patch:
            patch.setattr(new, 'start', lambda: ManagedWorker.start(new))
            patch.setattr(native, '_await_target_exec', fail_after_exec)
            patch.setattr(native.PhysicalNodeProcess, '_cleanup_resources', refuse_cleanup)
            caught = None
            try:
                runtime._replace_worker_if_dead()
            except RuntimeErrorCode as exc:
                caught = exc
            assert len(owned) == 1
            assert _poll_dead_or_alive(owned[0]._process) is False, \
                'real no-model child must still be live (deadline-bounded poll)'
            assert new._session is None, 'constructor raised before session assignment'
            assert not new.cleanup_proven(), 'failed constructor cleanup was reported clean'
            assert caught is not None and str(caught) == 'runtime_replacement_cleanup_unproven'
            assert runtime._stop.is_set()
            assert runtime._replacement_worker is new
        # Retry through the managed owner, not through the test's rescue handle.
        new.close()
        assert _poll_dead_or_alive(owned[0]._process), \
            f"child still alive after deadline: pid={owned[0]._process.pid}"
        assert new.cleanup_proven()
    finally:
        # Retain deterministic rescue even when testing an unfixed baseline.
        for process in owned:
            process.close()
            assert _poll_dead_or_alive(process._process), \
                f"child still alive after deadline: pid={process._process.pid}"
        old.close()
        new.close()


def test_never_started_worker_can_prove_no_child_cleanup(tmp_path):
    instance = worker(tmp_path)
    instance.close()
    assert instance.cleanup_proven()


@pytest.mark.parametrize('code,proven', [
    ('node_process_cleanup_failed', False),
    ('node_process_start_failed', True),
])
def test_constructor_failure_without_handle_preserves_native_cleanup_verdict(
    tmp_path, monkeypatch, code, proven,
):
    import mycelium_node.process as native
    from mycelium_a13_macos import worker as worker_module
    instance = worker(tmp_path)
    def refuse(**kwargs):
        raise native.NodeProcessError(code)
    monkeypatch.setattr(worker_module, '_ConcurrentNativeSession', refuse)
    with pytest.raises(native.NodeProcessError, match=code):
        instance.start()
    instance.close()
    assert instance.cleanup_proven() is proven


def test_close_during_constructor_never_proves_no_child(tmp_path, monkeypatch):
    import threading
    from mycelium_a13_macos import worker as module
    instance = worker(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    session = FakeConcurrentSession()
    session.pid = 12345
    errors = []
    def constructor(**kwargs):
        entered.set()
        assert release.wait(2)
        return session
    monkeypatch.setattr(module, '_ConcurrentNativeSession', constructor)
    def start():
        try:
            instance.start()
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=start)
    try:
        thread.start()
        assert entered.wait(1)
        instance.close()
        assert not instance.cleanup_proven(), 'constructor may still own an unpublished child'
        release.set()
        thread.join(2)
        assert not thread.is_alive()
        assert errors and str(errors[0]) == 'worker_not_running'
        assert session.returncode is not None
        assert session.calls == [], 'closed startup must not send hello'
        assert instance.cleanup_proven()
    finally:
        release.set()
        thread.join(2)
        instance.close()
