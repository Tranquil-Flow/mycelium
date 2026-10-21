# SPDX-License-Identifier: AGPL-3.0-only
"""Real app/authority/TLS integration, explicitly fake public tunnel provider."""
import pytest
from mycelium_a13_macos.app import DemoApp, DemoError
from mycelium_a13_macos.public_relay import SeedRelayProxy

CONSENT={'peer_visibility':True,'power':'battery_allowed','storage_bytes':2_000_000_000,
    'network':'unmetered_only','background':False,'diagnostics':False,'thermal':'stop_on_serious'}


@pytest.mark.parametrize("provider",["cloudflare"])
def test_app_manages_public_origin_without_manual_endpoint_edits(tmp_path,monkeypatch,provider):
    import mycelium_a13_macos.public_relay as module
    instances=[];expected_provider=provider
    class Tunnel:
        def __init__(self,root,provider='cloudflare'):
            assert provider==expected_provider
            self.root=root;self.public_origin='https://relay-fixture.invalid';self.closed=False;instances.append(self)
        def start(self,*,acknowledged,max_seconds):
            assert acknowledged is True and max_seconds==1800
            self.proxy=SeedRelayProxy().start();return self
        def status(self):return {'state':'fixture','public_origin':self.public_origin}
        def close(self):self.proxy.close();self.closed=True
    monkeypatch.setattr(module,'TemporaryHttpsRelay',Tunnel)
    app=DemoApp(tmp_path/'app',check_platform=False).start()
    try:
        with pytest.raises(DemoError):app.action('/api/network/start',{'relay_acknowledged':False,'max_seconds':1800})
        assert instances==[]
        value=app.action('/api/network/start',{'relay_acknowledged':True,'max_seconds':1800,'provider':provider})
        assert value['public_origin']=='https://relay-fixture.invalid'
        app.action('/api/create',{'consent':CONSENT})
        assert app.authority.coordinator.seed_url==value['public_origin']
        assert app.authority.status()['membership_state']=='current'
        assert instances[0].proxy.server.backend is not None
        with pytest.raises(DemoError):app.action('/api/network/start',{'relay_acknowledged':True,'max_seconds':1800,'provider':provider})
    finally:app.close()
    assert instances[0].closed
