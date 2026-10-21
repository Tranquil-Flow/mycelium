# SPDX-License-Identifier: AGPL-3.0-only
"""Repeated startup reconciliation must not hide later private checkpoints."""
from mycelium_a6_gateway import freeze_replay_checkpoint
from mycelium_a6_recovery import CheckpointStore
from tests.request_gateway.test_a6_gateway import _snapshot


def test_reconciliation_journal_includes_orphans_from_later_restart(tmp_path):
    root = tmp_path / 'checkpoints'
    previous = ()
    for ordinal, identity in enumerate(('first', 'later'), start=1):
        checkpoint = freeze_replay_checkpoint(
            _snapshot(), store_identity=identity,
            created_at_unix_ms=1000, expiry_unix_ms=2000000,
        )
        CheckpointStore(root, store_identity=identity).write(checkpoint)
        records = CheckpointStore.reconcile_orphans(root)
        assert records[:len(previous)] == previous
        assert len(records) == ordinal
        assert records[-1]['checkpoint_digest'] == checkpoint.checkpoint_digest()
        assert not list(root.glob('*.checkpoint.json'))
        assert CheckpointStore.reconcile_orphans(root) == records
        previous = records


def test_reconciliation_resumes_after_journal_before_discard_crash(tmp_path, monkeypatch):
    import pytest
    root = tmp_path / 'checkpoints'
    checkpoint = freeze_replay_checkpoint(
        _snapshot(), store_identity='interrupted',
        created_at_unix_ms=1000, expiry_unix_ms=2000000,
    )
    store = CheckpointStore(root, store_identity='interrupted')
    store.write(checkpoint)
    original = CheckpointStore.discard
    def interrupted(_store):
        raise OSError('injected crash after durable journal')
    monkeypatch.setattr(CheckpointStore, 'discard', interrupted)
    with pytest.raises(OSError):
        CheckpointStore.reconcile_orphans(root)
    assert store.load() is not None
    monkeypatch.setattr(CheckpointStore, 'discard', original)
    assert len(CheckpointStore.reconcile_orphans(root)) == 1
    assert store.load() is None
