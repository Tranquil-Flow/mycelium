# SPDX-License-Identifier: AGPL-3.0-only
"""V1.1 item c: after an app relaunch a snapshot verified on disk counts as prepared for ``/api/demo/start``.

Live demo (2026-10-21): after the owner relaunch the acquisition state read ``closed`` and Start answered
``owner_model_not_prepared`` although the 999,604,126 verified bytes were on disk; only re-running ``/api/model/download``
(about 10 s, nothing fetched) fixed it.  The owner's Start now re-verifies the snapshot itself.  Hash verification is
never skipped: the same ``_execute`` path re-hashes every file, a damaged file is refused, and the grant can fetch nothing.
"""
import threading
import time
from pathlib import Path

import pytest

from mycelium_a13_macos.acquisition import AcquisitionError, SelectedModelAcquisition
from mycelium_a13_macos.demo_chain import ACQUISITION_ACTIVE_STATES
from test_acquisition import (CANDIDATE_MODEL, _BombTransport, _consent, _injected_manifest, _repository_files,
                              _seed_stage)
from test_demo_chain import make, wait


def _prepared_then_relaunched(tmp_path, files):
    manifest = _injected_manifest(files)
    total = sum(map(len, files.values()))
    _seed_stage(tmp_path, files)
    first = SelectedModelAcquisition(tmp_path / 'state', _transport=_BombTransport(), _manifest=manifest)
    assert first._run_blocking(_consent(total))['state'] == 'prepared'
    first.close()
    transport = _BombTransport()
    second = SelectedModelAcquisition(tmp_path / 'state', _transport=transport, _manifest=manifest)
    return second, total, transport


def _settle(acquisition, timeout=30):
    deadline = time.monotonic() + timeout
    while acquisition.status()['state'] in ACQUISITION_ACTIVE_STATES and time.monotonic() < deadline:
        time.sleep(0.02)
    return acquisition.status()


def test_a_closed_snapshot_is_reverified_and_prepared_without_fetching_anything(tmp_path):
    files = _repository_files()
    acquisition, total, transport = _prepared_then_relaunched(tmp_path, files)
    assert acquisition.status()['state'] == 'closed'
    acquisition.reverify_closed_snapshot(storage_limit_bytes=total)
    status = _settle(acquisition)
    assert status['state'] == 'prepared' and status['error_code'] is None
    assert acquisition.prepared_source().source_bytes == len(files['model.safetensors'])
    assert transport.calls == 0
    acquisition.close()


def test_a_damaged_file_is_refused_because_every_hash_is_still_checked(tmp_path):
    files = _repository_files()
    acquisition, total, _ = _prepared_then_relaunched(tmp_path, files)
    snapshot = (tmp_path / 'state' / ('models--' + CANDIDATE_MODEL.model_id.replace('/', '--')) / 'snapshots'
                / CANDIDATE_MODEL.resolved_commit)
    victim = snapshot / 'model.safetensors'
    data = bytearray(victim.read_bytes()); data[-1] ^= 0xFF; victim.write_bytes(bytes(data))
    acquisition.reverify_closed_snapshot(storage_limit_bytes=total)
    status = _settle(acquisition)
    assert status['state'] == 'failed' and status['error_code'] == 'artifact_digest_mismatch'
    with pytest.raises(AcquisitionError):
        acquisition.prepared_source()
    acquisition.close()


def test_without_a_snapshot_on_disk_nothing_starts(tmp_path):
    acquisition = SelectedModelAcquisition(tmp_path / 'state', _transport=_BombTransport())
    with pytest.raises(AcquisitionError, match='source_not_on_disk'):
        acquisition.reverify_closed_snapshot(storage_limit_bytes=10**9)
    assert acquisition.status()['state'] == 'idle'
    acquisition.close()


def test_a_prepared_source_is_not_reverified_twice(tmp_path):
    files = _repository_files()
    manifest = _injected_manifest(files)
    _seed_stage(tmp_path, files)
    acquisition = SelectedModelAcquisition(tmp_path / 'state', _transport=_BombTransport(), _manifest=manifest)
    acquisition._run_blocking(_consent(sum(map(len, files.values()))))
    with pytest.raises(AcquisitionError, match='source_already_prepared'):
        acquisition.reverify_closed_snapshot(storage_limit_bytes=10**9)
    acquisition.close()


def test_start_reverifies_a_closed_snapshot_instead_of_asking_for_a_new_download(tmp_path):
    app, ops, members, run = make(tmp_path)
    state = {'value': 'closed', 'calls': 0}
    app.acquisition.status = lambda: {'state': state['value']}

    def reverify():
        state['calls'] += 1
        state['value'] = 'prepared'
        return True
    app.reverify_model_snapshot = reverify
    run.start()
    status = wait(run, lambda s: s['state'] in {'serving', 'failed'})
    assert status['state'] == 'serving', status
    assert state['calls'] == 1


def test_start_still_refuses_when_the_snapshot_cannot_be_reverified(tmp_path):
    app, ops, members, run = make(tmp_path)
    app.acquisition.status = lambda: {'state': 'closed'}
    app.reverify_model_snapshot = lambda: False
    run.start()
    status = wait(run, lambda s: s['state'] == 'failed')
    assert (status['failed_step'], status['error_code']) == ('model', 'owner_model_not_prepared')


def test_the_app_reverifies_with_the_standing_storage_consent_only(tmp_path):
    from mycelium_a13_macos.app import DemoApp, read_resource_policy
    from test_app_authority import CONSENT

    class Acquisition:
        def __init__(self): self.calls = []; self.state = 'closed'
        def status(self): return {'state': self.state}
        def reverify_closed_snapshot(self, *, storage_limit_bytes):
            self.calls.append(storage_limit_bytes); self.state = 'starting'
        def cancel(self): pass
        def close(self): pass

    app = DemoApp(tmp_path / 'owner', check_platform=False).start()
    try:
        app.action('/api/create', {'consent': CONSENT})
        app.acquisition.close()
        acquisition = app.acquisition = Acquisition()
        app._policy_observer = lambda: {'power': 'battery', 'thermal': 'nominal', 'network': 'unmetered'}
        assert app.reverify_model_snapshot() is False and acquisition.calls == []  # resource gates are never softened
        app._policy_observer = lambda: {'power': 'external', 'thermal': 'nominal', 'network': 'unmetered'}
        assert app.reverify_model_snapshot() is True
        assert acquisition.calls == [read_resource_policy(app.root)['storage_bytes']]
        assert app.reverify_model_snapshot() is False  # already running
    finally:
        app.close()
