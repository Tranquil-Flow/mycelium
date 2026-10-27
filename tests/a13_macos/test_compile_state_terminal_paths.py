# SPDX-License-Identifier: AGPL-3.0-only
"""V1.2 D2: a run that closes with ``request_cleanup_unproven`` must not wedge the compiler.

A13 app-flow gate (V1.1 candidate): the route's close raised ``router_port_shutdown_cleanup_unproven`` after the owner
run's teardown had already given up waiting for the serving thread (``rebuild_stop_timeout``) and forgotten the
observation context.  The serving thread's error handler then dereferenced ``app._observation_context`` (now ``None``)
and raised ``AttributeError``; ``_chain_compile_state`` stayed set, so every later start answered
``compiler_already_started`` until the app was restarted.
"""
import threading
import time
from types import SimpleNamespace

import pytest

from mycelium_a13_macos import chain_compile

PROPOSAL = {'max_combined_rss_bytes': 1, 'max_additional_disk_bytes': 1, 'max_wall_seconds': 1}


class BlockingWorkflow:
    """``run`` blocks until released, then fails the way the registry close does with an unproven cleanup."""

    def __init__(self, error=RuntimeError('router_port_shutdown_cleanup_unproven')):
        self.release = threading.Event()
        self.entered = threading.Event()
        self.error = error
        self.stop_event = threading.Event()

    def run(self, **_kwargs):
        self.entered.set()
        self.release.wait(10)
        if self.error is not None:
            raise self.error

    def status(self):
        return {"state": "failed" if self.error else "stopped",
                "error_code": str(self.error) if self.error else None}

    def stop(self):
        self.stop_event.set()


def _app(tmp_path, workflow):
    return SimpleNamespace(
        installation_role='owner', running=True, guard=threading.RLock(), root=tmp_path, closed=threading.Event(),
        _chain_epoch=0, _chain_workflow=workflow, _chain_approval=object(), _chain_serving_thread=None,
        _chain_compile_thread=None, _chain_activation_event=None,
        _chain_compile_state={'state': 'compiled', 'route_ready': False, 'epoch': 0, 'execution_proposal': PROPOSAL},
        _observation_context=SimpleNamespace(run_id='run-1', deployment_id='dep-1',
                                             facts={'a': {}, 'b': {}},
                                             observations={'observations': [{'host_id': 'h1'}, {'host_id': 'h2'}]}),
        _resource_check=lambda: 'denied')


@pytest.fixture
def thread_errors(monkeypatch):
    errors = []
    monkeypatch.setattr(threading, 'excepthook', lambda args: errors.append(args))
    return errors


def _drive_close_with_unproven_cleanup(tmp_path):
    workflow = BlockingWorkflow()
    app = _app(tmp_path, workflow)
    chain_compile.start_serving(app, proposal=PROPOSAL, acknowledged=True)
    assert workflow.entered.wait(5)
    thread = app._chain_serving_thread
    # The owner run's teardown: stop times out because the route close is still running, then it forgets the context.
    with pytest.raises(ValueError, match='rebuild_stop_timeout'):
        chain_compile.stop_serving(app, timeout=0.05)
    app._observation_context = None
    workflow.release.set()      # the close finishes, unproven
    thread.join(5)
    assert not thread.is_alive()
    return app


def test_serving_thread_error_handler_does_not_raise_after_the_context_was_forgotten(tmp_path, thread_errors):
    _drive_close_with_unproven_cleanup(tmp_path)
    assert thread_errors == [], [repr(e.exc_value) for e in thread_errors]


def test_a_start_after_a_cleanup_unproven_close_is_not_refused_as_compiler_already_started(tmp_path, thread_errors):
    app = _drive_close_with_unproven_cleanup(tmp_path)
    # The serving worker never takes app.guard, so it only marks the retired run; the next start reaps it.
    assert app._chain_retired_route is not None
    # A fresh observation context for the next run; the compiler guard must not see the old run's state.
    app._observation_context = SimpleNamespace(
        run_id='run-2', deployment_id='dep-2', facts={'a': {}, 'b': {}},
        observations={'observations': [{'host_id': 'h1'}, {'host_id': 'h2'}]})
    with pytest.raises(ValueError) as refused:
        chain_compile.start_compile(app, acknowledged=True)
    assert str(refused.value) == 'compiler_resource_policy_denied'   # got past the compiler_already_started guard
    assert app._chain_compile_state is None
    assert app._chain_serving_thread is None and app._chain_workflow is None
    assert app._chain_retired_route is None


def test_the_private_serve_error_record_still_names_the_run(tmp_path, thread_errors):
    _drive_close_with_unproven_cleanup(tmp_path)
    record = tmp_path / 'chain-runs' / 'run-1' / 'serve-error.private.json'
    assert record.is_file()
    assert 'router_port_shutdown_cleanup_unproven' in record.read_text()


def test_a_serving_thread_that_ends_while_its_run_is_current_leaves_state_to_the_owner_teardown(tmp_path, thread_errors):
    workflow = BlockingWorkflow(error=None)
    app = _app(tmp_path, workflow)
    chain_compile.start_serving(app, proposal=PROPOSAL, acknowledged=True)
    assert workflow.entered.wait(5)
    thread = app._chain_serving_thread
    workflow.release.set()
    thread.join(5)
    # not retired: the owner run's own teardown (stop_serving) forgets it, so status keeps reporting until then
    assert app._chain_compile_state is not None and app._chain_workflow is workflow
    chain_compile.stop_serving(app, timeout=1.0)
    assert app._chain_compile_state is None and app._chain_serving_thread is None


def test_stop_serving_with_a_stuck_thread_still_refuses_and_keeps_the_state(tmp_path, thread_errors):
    workflow = BlockingWorkflow(error=None)
    app = _app(tmp_path, workflow)
    chain_compile.start_serving(app, proposal=PROPOSAL, acknowledged=True)
    assert workflow.entered.wait(5)
    with pytest.raises(ValueError, match='rebuild_stop_timeout'):
        chain_compile.stop_serving(app, timeout=0.05)
    assert app._chain_compile_state is not None       # a live route is never forgotten while its thread runs
    with pytest.raises(ValueError, match='compiler_already_started'):
        chain_compile.start_compile(app, acknowledged=True)
    workflow.release.set()
    app._chain_serving_thread.join(5)


# -- a new Start while the previous route is still closing (V1.2 D1/D2) -------------------------------------------

def test_start_waits_for_the_previous_route_to_finish_closing_instead_of_failing(tmp_path):
    import threading as _t
    from test_demo_chain import make, wait
    app, ops, members, run = make(tmp_path)
    release = _t.Event()
    closing = _t.Thread(target=lambda: release.wait(10), daemon=True)
    closing.start()
    app._chain_serving_thread = closing          # the last route's close is still running (unproven cleanup)
    run.chain.budgets['previous_close'] = 5.0
    run.start()
    time.sleep(0.2)
    assert run.status()['state'] == 'running' and run.status()['step'] == 'model'
    assert 'previous route' in (run.status()['steps'][0]['detail'] or '')
    release.set()
    assert wait(run, lambda s: s['state'] in {'serving', 'failed'})['state'] == 'serving'


def test_start_names_a_route_that_never_finishes_closing(tmp_path):
    import threading as _t
    from test_demo_chain import make, wait
    app, ops, members, run = make(tmp_path)
    release = _t.Event()
    closing = _t.Thread(target=lambda: release.wait(10), daemon=True)
    closing.start()
    app._chain_serving_thread = closing
    run.chain.budgets['previous_close'] = 0.1
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('model', 'previous_run_still_closing')
    release.set()
