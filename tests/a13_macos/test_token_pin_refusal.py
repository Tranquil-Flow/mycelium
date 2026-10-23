# SPDX-License-Identifier: AGPL-3.0-only
"""Target policy diagnostics survive real signed TLS without inventing native output."""
import json
import threading
import time

import pytest

from mycelium_a13_macos.token_policy import TargetTokenPolicy
from physical_inference_qualification import ControllerError
from tests.live.test_token_path_admission import world
from tests.live.test_token_path_provisioning import provisioned
from test_control_pump import _world
from test_control_session import owner, session
from test_concurrent_pump import make_pump
from test_managed_control import RUN_ID, DEPLOYMENT_ID
from test_worker import worker


def test_expired_target_policy_has_explicit_owner_terminal_and_no_native_result(tmp_path, monkeypatch, world):
    policy_world = provisioned(world)
    policy_world.trust.ceiling = world.now - 1
    policy = object.__new__(TargetTokenPolicy)
    policy.trust = policy_world.trust
    policy.graph = policy_world.n.graph
    policy._peers = policy_world.peers
    policy._lock = threading.RLock()
    policy._pending = policy.installed = None
    policy._refresh = None
    with _world(tmp_path/'swarm') as swarm:
        root = tmp_path/'native'; root.mkdir()
        w = worker(root, node_id=swarm['member']['node_id'], run_id=RUN_ID, deployment_id=DEPLOYMENT_ID)
        pump = make_pump(tmp_path/'pump', swarm, w)
        sender = owner(tmp_path/'sender', swarm)
        stop = threading.Event(); errors = []

        def pump_loop():
            while not stop.is_set():
                try: pump.step()
                except Exception as error:
                    errors.append(str(error)); return
                stop.wait(.01)

        thread = threading.Thread(target=pump_loop)
        try:
            w.start()
            w._token_policy = policy
            w._load_admitted = True  # CPU guard fixture; no configure/model allowed.
            original_send = w._session.send

            def native_boundary(**kwargs):
                assert kwargs['command'] != 'install_token_path', 'refused install crossed native boundary'
                return original_send(**kwargs)

            monkeypatch.setattr(w._session, 'send', native_boundary)
            thread.start()
            started = time.monotonic()
            with pytest.raises(ControllerError) as refused:
                session(sender, swarm).send(command_id='expired-install', command='install_token_path',
                                            payload={'policy':policy_world.envelope})
            assert refused.value.code == 'managed_control_reconciliation_required'
            assert refused.value.remote_code == 'token_path_policy_expired'
            assert time.monotonic() - started < 10
            assert w.status()['command_count'] == 0
            mailbox = swarm['authority'].control_mailbox
            with mailbox._connect() as connection:
                row = connection.execute('SELECT * FROM managed_commands WHERE command_id=?', ('expired-install',)).fetchone()
            assert row['state'] == 'reconciliation_required'
            assert row['native_response_json'] is None
            terminal = json.loads(row['terminal_receipt_json'])['statement']
            assert terminal['target_error_code'] == 'token_path_policy_expired'
            local = json.loads((pump.root/'uncertain-ack.private.json').read_bytes())
            assert local['error_code'] == 'token_path_policy_expired'
            # Exact owner retry observes the same terminal, never dispatches again.
            with pytest.raises(ControllerError) as repeated:
                session(sender, swarm).send(command_id='expired-install', command='install_token_path',
                                            payload={'policy':policy_world.envelope})
            assert repeated.value.remote_code == 'token_path_policy_expired'
            assert w.status()['command_count'] == 0
        finally:
            stop.set()
            if thread.ident is not None: thread.join(5)
            pump.close(); w.close(); sender.close()
        assert not thread.is_alive() and not errors
        assert not w.process_group_present()
