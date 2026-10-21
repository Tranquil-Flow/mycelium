# SPDX-License-Identifier: AGPL-3.0-only
"""Current worker incarnation survives its own cleanup, not stale bootstrap state."""
from types import SimpleNamespace
from test_parent_cause_boundaries import fenced_fixture


def test_post_cleanup_cause_retains_current_worker_pid_and_birth(tmp_path, monkeypatch):
    runtime, old, new, _, _ = fenced_fixture(tmp_path, monkeypatch)
    session = old._session
    session.returncode = None
    session.pid = 515151
    session._pre_exec_ownership = SimpleNamespace(start_token='101:12')
    # These are the per-worker fields actually latched by ManagedWorker._start_native.
    old.pid = session.pid
    old._birth_token = session._pre_exec_ownership.start_token
    try:
        old._expire()
        assert old._session is session and old._closed.is_set()
        snapshot = old.private_cause_snapshot()
        assert snapshot['previous_shutdown_actor'] == 'expiry'
        assert snapshot['previous_pid'] == 515151
        assert snapshot['previous_birth_token'] == '101:12'
        assert snapshot['previous_returncode'] is None
    finally:
        old.close()
        new.close()
