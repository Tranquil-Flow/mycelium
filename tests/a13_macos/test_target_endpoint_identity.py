# SPDX-License-Identifier: AGPL-3.0-only
"""Target-native endpoint identity binding to the joined member key.

Real loopback TLS + native worker hello; no model load, no sidecar start.
The joined target key (state_root/identity/node.key) must be the same key the
native runtime uses as its endpoint secret; it must never be re-generated or
substituted at activation time.
"""
import os
import stat
from pathlib import Path
import pytest
from test_control_pump import _world
from test_control_session import owner, session
from test_managed_staging import bundle
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_worker import POLICY, SAFE
from mycelium_a13_macos.target_app import TargetApplication
from mycelium_a13_macos.worker import WorkerLimits
from mycelium_a13_macos.authority import _private_directory


def _joined_key(w):
    return w['target'].state_root / 'identity' / 'node.key'


def _app(w):
    app = TargetApplication(w['target'].state_root)
    app.peer = w['target']
    return app


def _offer(w, *, preposition=None):
    archive, _ = bundle()
    return w['authority']._control_mailbox.publish_offer(
        target=w['member'], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
        archive=archive, preposition=preposition)


def _options(w, **changes):
    options = dict(acknowledged=True, resource_policy=POLICY,
        limits=WorkerLimits(max_rss_bytes=512*1024*1024, max_disk_bytes=100_000,
            max_wall_seconds=30, max_commands=20),
        authorize_load=lambda envelope: False,
        _test_observer=lambda: dict(SAFE),
        _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
    options.update(changes)
    return options


def test_work_activation_binds_native_runtime_to_joined_endpoint_key(native_sidecar_required, tmp_path):
    with _world(tmp_path/'world') as w:
        app = _app(w)
        key = _joined_key(w)
        assert key.exists()
        before = key.read_bytes()
        offer = _offer(w)
        sender = owner(tmp_path/'sender', w)
        try:
            status = app.start_work(offer, **_options(w))
            assert status['state'] == 'running'
            runtime = app._runtime
            # Worker was constructed with the trusted joined key path.
            assert runtime.worker.endpoint_secret_file == key
            assert runtime.worker.endpoint_secret_file.is_absolute()
            # Real native hello over signed TLS still works with the binding.
            native = session(sender, w)
            reply = native.send(command_id='identity-hello', command='hello', payload={})
            assert reply['ok'] is True and reply['route_ready'] is False
            native.close()
        finally:
            app.stop_control(); sender.close()
        # No re-generation or substitution of the joined key.
        assert key.read_bytes() == before
        mode = stat.S_IMODE(key.lstat().st_mode)
        assert mode == 0o600


def test_control_only_activation_uses_same_joined_endpoint_key(native_sidecar_required, tmp_path):
    with _world(tmp_path/'world') as w:
        app = _app(w)
        key = _joined_key(w)
        before = key.read_bytes()
        offer = _offer(w)
        try:
            status = app.start_control(offer, acknowledged=True,
                resource_policy=POLICY, max_disk_bytes=100_000,
                _test_observer=lambda: dict(SAFE),
                _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
            assert status['state'] == 'running'
            assert app._runtime.worker.endpoint_secret_file == key
        finally:
            app.stop_control()
        assert key.read_bytes() == before


def test_missing_joined_key_fails_before_staging_or_process(tmp_path):
    with _world(tmp_path/'world') as w:
        app = _app(w)
        key = _joined_key(w)
        key.unlink()
        offer = _offer(w)
        with pytest.raises(ValueError, match='endpoint_identity_missing'):
            app.start_work(offer, **_options(w))
        assert app._runtime is None
        assert not (app.peer.state_root/'managed'/RUN_ID).exists()
        # Peer still refuses to re-generate identity after post-identity state exists.
        assert not key.exists()


def test_substituted_joined_key_rejected_before_staging_or_process(tmp_path):
    from mycelium_node.identity import load_or_create_node_signer
    with _world(tmp_path/'world') as w:
        app = _app(w)
        key = _joined_key(w)
        enrolled = w['member']['endpoint_id']
        # Substitute a different valid key at the same trusted path (fresh dir).
        offer = _offer(w)
        key.unlink()
        replacement_root = tmp_path/'replacement'
        replacement_root.mkdir(mode=0o700)
        replacement = load_or_create_node_signer(replacement_root/'node.key')
        assert replacement.endpoint_id != enrolled
        mode = key.parent.stat().st_mode
        key.write_bytes((replacement_root/'node.key').read_bytes())
        os.chmod(key, 0o600)
        with pytest.raises(ValueError, match='endpoint_identity_mismatch'):
            app.start_work(offer, **_options(w))
        assert app._runtime is None
        assert not (app.peer.state_root/'managed'/RUN_ID).exists()


def test_prepare_runtime_rejects_nonjoined_secret_path(tmp_path):
    from test_managed_runtime import runtime as make_runtime
    with _world(tmp_path/'world') as w:
        app = _app(w)
        prepared=make_runtime(tmp_path/'prepared',w)
        try:
            with pytest.raises(ValueError,match='endpoint_identity_path_invalid'):
                app.prepare_runtime(run_id=RUN_ID, deployment_id=DEPLOYMENT_ID,
                    staging=prepared.staging, archive=prepared._archive,
                    archive_digest=prepared.archive_digest,
                    resource_policy=POLICY, limits=prepared.worker.limits,
                    sidecar_binary=prepared.worker.sidecar_binary, authorize_load=None,
                    endpoint_secret_file=Path('/tmp/nonjoined-node.key'),
                    _test_observer=lambda: dict(SAFE),
                    _test_ca_certificate=w['authority'].seed_server.material.ca_certificate)
        finally: prepared.close()
