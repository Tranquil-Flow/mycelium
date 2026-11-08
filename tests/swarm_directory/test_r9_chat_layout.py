"""Late real entry metadata must not move the conversation or composer."""
import json
import time
import pytest
from playwright.sync_api import sync_playwright, expect
from test_site_v2_browser import local, page_for
from test_hosted_ui_browser import create_swarm, terminal

@pytest.mark.parametrize('width,height', [(1280,720),(390,844)])
@pytest.mark.parametrize('theme',['dark','light'])
def test_delayed_entry_metadata_keeps_zero_cls(local,tmp_path,width,height,theme):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        owner=page_for(browser,local,'#new')
        create_swarm(owner,local,'Metadata fixture','open',requests=1)
        sid=owner.locator('[data-owned]').first.get_attribute('data-owned')
        visitor=page_for(browser,local,'#chat/'+sid,viewport={'width':width,'height':height},color_scheme=theme)
        visitor.click('#consent')
        expect(visitor.locator('#prompt')).to_be_enabled()
        # All static assets have arrived. Hold only the real entry response,
        # not CSS delivery, and let browser time cross the recent-input window.
        from mycelium_a13_macos import swarm_entry
        def delayed(method,path,headers,body):
            response=swarm_entry.handle(local['app'],method,path,headers,body,via_relay=True)
            if path.endswith(('/send','/events')):
                time.sleep(.85)
            return response
        local['proxy'].bind_swarm_entry(delayed)
        visitor.fill('#prompt','capital of Germany?')
        visitor.click('#send')
        expect(terminal(visitor,'completed')).to_be_visible()
        visitor.fill('#prompt','second prompt');visitor.click('#send')
        expect(terminal(visitor,'quota')).to_be_visible()
        visitor.wait_for_timeout(650)
        details=visitor.evaluate('__clsDetails')
        (tmp_path/'late-metadata-cls.json').write_text(json.dumps(details,indent=2))
        assert sum(visitor.evaluate('__cls'))==0,details
        browser.close()
