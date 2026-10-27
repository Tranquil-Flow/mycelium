# SPDX-License-Identifier: AGPL-3.0-only
"""Real staging/TLS and native start, injected pre-launch transport failures; no tensors."""
import time

import pytest

from mycelium_a13_macos.managed_control import ManagedControlClient, ManagedControlError
from mycelium_a13_macos.managed_offers import ManagedOfferClient
from mycelium_a13_macos.standing_consent import (
    StandingAcceptor, bind_standing_consent, grant_standing_consent,
)
from mycelium_a13_macos.work_acceptance import preview_work, accept_work
from test_control_pump import _world
from test_work_acceptance_parent import arrange
from test_standing_consent import GRANT, POLICY


@pytest.mark.parametrize('failure,nth', [('read', 1), ('read', 2), ('read', 4),
                                         ('lost_ack', 1), ('lost_ack', 2), ('lost_ack', 3)])
def test_revalidated_staging_resumes_before_exactly_one_native_start(native_sidecar_required, tmp_path, monkeypatch, failure, nth):
    with _world(tmp_path / 'world') as w:
        app, offer, options = arrange(w)
        now = time.time()
        grant_standing_consent(tmp_path, GRANT, policy=POLICY, now=now)
        binding = app.peer.membership_binding()
        bind_standing_consent(tmp_path, binding, now=now)
        ca = options['_test_ca_certificate']
        original_pending = app.pending_stage
        monkeypatch.setattr(app, 'pending_stage', lambda **kw: original_pending(_test_ca_certificate=ca))
        def preview(actor, offered, **kw):
            return preview_work(actor, offered, _test_ca_certificate=ca, **kw)
        def accept(actor, value, **kw):
            return accept_work(actor, value, _test_ca_certificate=ca, **kw)
        acceptor = StandingAcceptor(tmp_path, preview=preview, accept=accept,
            limits_for=lambda _: vars(options['limits']))
        attempts, starts, receipts = [], [], []
        original_fetch = ManagedOfferClient.fetch
        original_request = ManagedControlClient._request
        original_activate = app.activate_runtime
        def fetch(client, *args, **kwargs):
            if failure == 'read':
                attempts.append('read')
                if len(attempts) == nth:
                    raise ManagedControlError('managed_control_unreachable',
                        request_path='/managed-control/staging/read')
            return original_fetch(client, *args, **kwargs)
        def request(client, path, envelope):
            result = original_request(client, path, envelope)
            if path == '/managed-control/staging/ack':
                receipts.append(w['authority'].control_mailbox.stage_receipt(offer))
                if failure == 'lost_ack':
                    attempts.append('ack')
                    if len(attempts) == nth:
                        raise ManagedControlError('managed_control_response_timeout', status=502, request_path=path)
            return result
        def activate(runtime):
            starts.append(runtime)
            return original_activate(runtime)
        monkeypatch.setattr(ManagedOfferClient, 'fetch', fetch)
        monkeypatch.setattr(ManagedControlClient, '_request', request)
        monkeypatch.setattr(app, 'activate_runtime', activate)
        kwargs = dict(policy=POLICY, binding=binding, resource_check=lambda: None,
                      _test_observer=options['_test_observer'])
        consent_before = app.peer._load_consent()
        try:
            failed = acceptor.tick(app, **kwargs)
            assert failed['state'] == 'failed'
            assert failed['reason'] == ('managed_control_unreachable' if failure == 'read'
                                        else 'managed_control_response_timeout')
            assert len(attempts) == nth
            assert app._runtime is None and starts == []
            assert app.peer._load_consent() == consent_before
            # Any already committed ack is reproduced exactly, not a second launch.
            result = acceptor.tick(app, **kwargs)
            assert result is not None and result['state'] == 'accepted'
            assert len(starts) == 1
            native = app.status()['native_control']
            assert native['state'] == 'running' and native['work_admitted'] is True
            assert native['model_load_authorized'] is False
            assert receipts and all(receipt == receipts[0] for receipt in receipts)
            assert acceptor.tick(app, **kwargs) is None
            assert len(starts) == 1
            assert original_pending(_test_ca_certificate=ca)['expires_at'] == offer['expires_at']
        finally:
            app.stop_control()
