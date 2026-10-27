# SPDX-License-Identifier: AGPL-3.0-only
"""R4: failure injection at actual owner cleanup boundaries, no secret text."""
from types import SimpleNamespace
import pytest
from mycelium_a13_macos import chain_compile as cc
from test_rollover_stop_budget import timed_run


def broken(*args, **kwargs):
    raise OSError('private-token-do-not-publish')


@pytest.mark.parametrize('seam,code', [
    ('cancel', 'owner_chat_cancel_failed'),
    ('announce', 'owner_stop_announce_failed'),
    ('state', 'owner_cleanup_status_failed'),
    ('tick', 'owner_cleanup_tick_failed'),
    ('request', 'owner_cleanup_request_failed'),
    ('receipt', 'owner_cleanup_receipt_failed'),
    ('release', 'owner_channel_release_failed'),
    ('artifacts', 'rollover_artifact_cleanup_failed'),
])
def test_teardown_records_each_swallowed_exception(timed_run, monkeypatch, seam, code):
    t = timed_run
    t.install(0)
    chain = t.run.chain
    if seam == 'cancel':
        t.app.chat = SimpleNamespace(cancel_all=broken)
    elif seam == 'announce':
        monkeypatch.setattr(chain, '_announce', broken)
    elif seam == 'state':
        monkeypatch.setattr(chain, '_workflow_state', broken)
    elif seam == 'tick':
        monkeypatch.setattr(chain, '_tick_local', broken)
    elif seam == 'request':
        monkeypatch.setattr(t.app.authority.control_mailbox, 'request_stage_cleanup', broken)
    elif seam == 'receipt':
        monkeypatch.setattr(t.app.authority.control_mailbox, 'cleanup_receipt', broken)
    elif seam == 'release':
        monkeypatch.setattr('mycelium_a13_macos.chain_observation.release_owner_channel', broken)
    else:
        monkeypatch.setattr('mycelium_a13_macos.compile_cache.discard_retired_artifacts', broken)
    # Synchronous teardown isolates the handler from monitor/next-run stages.
    t.old.state['rollover_requested'] = True
    chain.teardown(t.old, failed=False)
    rows = t.old.result.get('cleanup_errors', [])
    assert any(r['code'] == code and r['exception_class'] == 'OSError' for r in rows), rows
    assert 'private-token' not in str(t.old.result)


def test_recovered_stop_timeout_is_still_recorded(timed_run):
    t = timed_run
    t.install(35)
    t.ops.stop_before = lambda app, **kw: cc.stop_serving(app, timeout=30)
    assert t.rollover()['state'] == 'serving'
    assert any(r['code'] == 'rebuild_stop_timeout' and r['exception_class'] == 'ValueError'
               for r in t.old.result.get('cleanup_errors', []))


def test_owner_teardown_exception_records_class_in_status(timed_run, monkeypatch):
    t = timed_run
    monkeypatch.setattr(t.run.chain, 'teardown', broken)
    status = t.rollover()
    assert status['cleanup_unproven']['code'] == 'owner_teardown_failed'
    assert status['cleanup_unproven']['exception_class'] == 'OSError'
    assert 'private-token' not in str(status)


def test_retired_marker_failure_is_visible_in_run_result(timed_run):
    t = timed_run
    class BrokenMarker:
        _chain_epoch = 1
        _chain_workflow = t.workflow
        owner_run = t.run
        def __setattr__(self, name, value):
            if name == '_chain_retired_route':
                broken()
            object.__setattr__(self, name, value)
    cc._mark_retired_route(BrokenMarker(), t.workflow, 0, t.old.run_id)
    assert any(r['code'] == 'chain_retired_marker_failed' and r['exception_class'] == 'OSError'
               for r in t.old.result.get('cleanup_errors', []))


def test_poll_diagnostics_are_coalesced_and_redacted(timed_run):
    t = timed_run
    for _ in range(1000):
        t.old.record_exception('owner_cleanup_tick_failed', OSError('private-token'))
    row = next(r for r in t.run.status_snapshot()['cleanup_errors']
               if r['code'] == 'owner_cleanup_tick_failed')
    assert row == {'code': 'owner_cleanup_tick_failed', 'exception_class': 'OSError', 'count': 1000}


def _handler_records_or_raises(handler):
    import ast
    return any(isinstance(n, ast.Raise) or (
        isinstance(n, ast.Call) and isinstance(n.func, (ast.Name, ast.Attribute))
        and (n.func.id if isinstance(n.func, ast.Name) else n.func.attr)
        in {'record_exception', '_record_cleanup_exception', 'record_cleanup_exception'})
        for stmt in handler.body for n in ast.walk(stmt))


def test_scoped_cleanup_handlers_cannot_be_silent():
    import ast
    from pathlib import Path
    root = Path(cc.__file__).parent
    scopes = {
        'owner_run.py': {'_main', '_run_once', '_end_at_ceiling', 'status'},
        'demo_chain.py': {'teardown', '_cleanup_tick', '_target_cleanup_proven', 'monitor', '_restarted_member'},
        'workflow.py': {'run'},
        'chain_compile.py': {'stop_serving', 'await_stopped', '_mark_retired_route', '_reap_retired_route_locked', 'start_serving'},
    }
    handlers = []
    for file, names in scopes.items():
        tree = ast.parse((root / file).read_text())
        functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in names]
        assert {f.name for f in functions} == names
        for function in functions:
            handlers.extend((file, h) for h in ast.walk(function) if isinstance(h, ast.ExceptHandler))
    assert handlers
    assert not [(file, h.lineno) for file, h in handlers if not _handler_records_or_raises(h)]
    # Negative control: the lint must reject the old silent-handler pattern.
    silent = ast.parse('try:\n    cleanup()\nexcept Exception:\n    pass').body[0].handlers[0]
    assert not _handler_records_or_raises(silent)

