# SPDX-License-Identifier: AGPL-3.0-only
"""Chromium owner/target pairing through real local TLS, no physical inference."""
import json
import os
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright, expect
from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.peer import RemoteMacMember
from mycelium_a13_macos.tls import pinned_seed_client, pinned_seed_client_from_invite


@pytest.mark.parametrize('_stress_run', range(int(os.environ.get('A13_CONTROL_STRESS_COUNT', '1'))))
def test_browser_owner_target_encrypted_pairing(tmp_path, monkeypatch, _stress_run):
    owner=DemoApp(tmp_path/'owner',check_platform=False).start()
    target=None
    try:
        def client(*,bundle=None,membership=None,now,timeout):
            ca=owner.authority.seed_server.material.ca_certificate
            if bundle is not None:
                return pinned_seed_client_from_invite(bundle,now=now,ca_certificate=ca,timeout=timeout)
            return pinned_seed_client(seed_url=membership['seed_url'],swarm_id=membership['swarm_id'],
                seed_key_digest=membership['seed_key_digest'],seed_key_records=list(membership['seed_key_records']),
                ca_certificate=ca,timeout=timeout)
        target=DemoApp(tmp_path/'target',check_platform=False,
            _test_peer_factory=lambda root:RemoteMacMember(root,_test_seed_client_factory=client)).start()
        with sync_playwright() as p:
            browser=p.chromium.launch()
            op=browser.new_context(viewport={'width':1280,'height':900}).new_page()
            tp=browser.new_context(viewport={'width':1280,'height':900}).new_page(); errors=[]
            for page,app in ((op,owner),(tp,target)):
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(app.launch_file.as_uri())
                page.get_by_role('button',name='Open private demo',exact=True).click()
                expect(page.locator('#private')).to_be_visible()
                page.locator('#visibility').check()
            op.locator('#visibility').check()
            op.locator('#create').click()
            expect(op.locator('#ladder')).to_contain_text('current')
            tp.locator('#visibility').check()
            tp.locator('#join').click()
            expect(tp.locator('#pair-output')).to_be_visible()
            pair=json.loads(tp.locator('#pair-output').inner_text())
            op.locator('#pairing-details').evaluate('(el)=>el.open=true')
            op.locator('#pair-request-input').fill(json.dumps(pair))
            op.locator('#pair-preview').click()
            expect(op.locator('#pair-identity')).to_contain_text(pair['identity_key_digest'])
            expect(op.locator('#pair-approve')).to_be_disabled()
            op.locator('#pair-confirm').check()
            op.locator('#pair-approve').click()
            expect(op.locator('#handoff-output')).to_be_visible()
            handoff=op.locator('#handoff-output').inner_text()
            tp.locator('#pairing-details').evaluate('(el)=>el.open=true')
            tp.locator('#handoff-input').fill(handoff)
            tp.locator('#accept-handoff').click()
            expect(tp.locator('#ladder')).to_contain_text('current')
            expect(tp.locator('#create')).to_be_disabled()
            expect(tp.locator('#submit')).to_be_disabled()
            expect(tp.locator('#revoke')).to_be_disabled()
            tp.locator('#capability-report').click()
            expect(tp.locator('#ladder')).to_contain_text('Self-report accepted')
            from test_managed_staging import bundle
            from test_managed_control import RUN_ID, DEPLOYMENT_ID
            from test_worker import SAFE
            archive,digest=bundle()
            member=owner.authority.coordinator.member(pair['node_id'])
            offered=owner.authority._control_mailbox.publish_offer(target=member,
                run_id=RUN_ID,deployment_id=DEPLOYMENT_ID,archive=archive)
            real_client=target.authority.peer.staging_control_client
            monkeypatch.setattr(target.authority.peer,'staging_control_client',
                lambda **kw:real_client(_test_ca_certificate=owner.authority.seed_server.material.ca_certificate))
            real_managed_client=target.authority.peer.managed_control_client
            monkeypatch.setattr(target.authority.peer,'managed_control_client',
                lambda **kw:real_managed_client(_test_ca_certificate=owner.authority.seed_server.material.ca_certificate))
            monkeypatch.setattr(target,'_policy_observer',lambda:dict(SAFE))
            tp.locator('#check-stage').click()
            try:expect(tp.locator('#stage-offer')).to_contain_text(digest)
            except AssertionError:
                print('staging notice:',tp.locator('#notice').inner_text())
                raise
            expect(tp.locator('#accept-stage')).to_be_disabled()
            tp.locator('#stage-consent').check()
            tp.locator('#accept-stage').click()
            expect(tp.locator('#stage-state')).to_contain_text('Staged and verified')
            assert target.authority._runtime is None
            from mycelium_a13_macos.peer import RemoteMacMemberError
            with pytest.raises(RemoteMacMemberError,match='peer_member_not_current'):
                target.authority.peer.managed_control_snapshot()
            expect(tp.locator('#submit')).to_be_disabled()
            expect(tp.locator('#start-control')).to_be_disabled()
            tp.locator('#control-consent').check()
            # Hold the actual start handler before it attaches a runtime. A click
            # only queues the POST; it does not establish a completed action.
            entered, release = threading.Event(), threading.Event()
            real_start = target.authority.start_control
            def held_start(*args, **kwargs):
                entered.set()
                if not release.wait(20):
                    raise AssertionError('held control start was not released')
                return real_start(*args, **kwargs)
            monkeypatch.setattr(target.authority, 'start_control', held_start)
            try:
                with tp.expect_response(lambda r: r.url.endswith('/api/control/start') and r.request.method == 'POST') as start_response:
                    tp.locator('#start-control').click()
                    assert entered.wait(10), 'start POST never entered its handler'
                    assert target.authority._runtime is None
                    assert 'stopped' in tp.locator('#control-state').inner_text()
                    release.set()
                assert start_response.value.ok, start_response.value.text()
            finally:
                release.set()
            expect(tp.locator('#control-state')).to_contain_text('running')
            managed=target.authority._runtime
            assert managed is not None and managed.worker.status()['model_load_authorized'] is False
            with tp.expect_response(lambda r: r.url.endswith('/api/control/stop') and r.request.method == 'POST') as stop_response:
                tp.locator('#stop-control').click()
            assert stop_response.value.ok, stop_response.value.text()
            expect(tp.locator('#control-state')).to_contain_text('stopped')
            assert not managed.worker.process_group_present()
            owner.authority._control_mailbox.request_stage_cleanup(offered)
            tp.locator('#check-cleanup').click()
            expect(tp.locator('#cleanup-offer')).to_contain_text(digest)
            expect(tp.locator('#accept-cleanup')).to_be_disabled()
            tp.locator('#cleanup-consent').check()
            tp.locator('#accept-cleanup').click()
            expect(tp.locator('#cleanup-state')).to_contain_text('Staging removed')
            assert owner.authority._control_mailbox.cleanup_receipt(offered)['removed'] is True
            assert target.authority.peer.member.node_id==pair['node_id']
            tp.reload()
            expect(tp.locator('#ladder')).to_contain_text('current')
            expect(tp.locator('#handoff-input')).to_have_value('')
            clean=browser.new_context(); other=clean.new_page(); other.goto(target.origin)
            expect(other.locator('#private')).to_be_hidden()
            expect(other.locator('#handoff-input')).to_have_value('')
            assert handoff not in other.content()
            if os.environ.get('A13_BROWSER_EVIDENCE'):
                out=Path(os.environ['A13_BROWSER_EVIDENCE']);out.mkdir(parents=True,exist_ok=True)
                tp.screenshot(path=str(out/'target-enrolled-desktop.png'),full_page=True)
                tp.set_viewport_size({'width':390,'height':844})
                assert tp.evaluate('document.documentElement.scrollWidth <= innerWidth')
                tp.screenshot(path=str(out/'target-enrolled-mobile.png'),full_page=True)
            assert errors==[]
            browser.close()
    finally:
        if target is not None:target.close()
        owner.close()
