"""Owner request text uses a reserved, keyboard-readable box."""
import pytest
from playwright.sync_api import sync_playwright,expect
from test_site_v2_browser import local,page_for
from test_hosted_ui_browser import create_swarm

@pytest.mark.parametrize('width,height',[(1440,900),(390,844)])
@pytest.mark.parametrize('theme',['dark','light'])
def test_request_copy_cannot_move_decision_slot(local,width,height,theme):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        owner=page_for(browser,local,'#new',viewport={'width':width,'height':height},color_scheme=theme)
        create_swarm(owner,local,'Layout fixture','request')
        sid=owner.locator('[data-owned]').first.get_attribute('data-owned')
        visitor=page_for(browser,local,'#chat/'+sid)
        visitor.click('#ask');expect(visitor.locator('#gate')).to_have_attribute('data-state','pending')
        owner.click('#refresh');expect(owner.locator('.request')).to_be_visible()
        row=owner.locator('.request');text=row.locator('p')
        # Unit-level visual stress on the real request DOM. The existing owner
        # tests separately exercise the actual signed grant/deny transactions.
        text.evaluate("e=>e.textContent='Key AAAA-BBBB-CCCC-DDDD · pending'")
        before=row.locator('.decision-slot').bounding_box()
        text.evaluate("e=>e.textContent='Key AAAA-BBBB-CCCC-DDDD · granted · '+ 'long note '.repeat(14)")
        after=row.locator('.decision-slot').bounding_box()
        assert before==after
        text.focus();expect(text).to_be_focused()
        assert text.evaluate('e=>e.scrollHeight>e.clientHeight')
        owner.keyboard.press('End')
        expect(text).to_have_js_property('scrollTop',text.evaluate('e=>e.scrollHeight-e.clientHeight'))
        browser.close()
