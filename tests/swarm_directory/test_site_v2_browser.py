# SPDX-License-Identifier: AGPL-3.0-only
"""Real local directory + entry, scripted generation only. No live website traffic."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_hosted_ui_browser import make_stack, close_stack, create_swarm, terminal

CLS = "window.__cls=[];new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput)window.__cls.push(e.value)}).observe({type:'layout-shift',buffered:true})"

@pytest.fixture
def local(tmp_path):
    value = make_stack(tmp_path)
    yield value
    close_stack(value)


def page_for(browser, local, route='#browse', **options):
    page = browser.new_context(**options).new_page()
    page.add_init_script(CLS)
    page.goto(local['directory'].origin + '/swarms/' + route)
    page.wait_for_selector('body[data-ready="1"]')
    return page


def test_twenty_real_app_behaviours(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        owner = page_for(browser, local, '#new')
        # 1–5: native registry picker, form validity, quota visibility, actual creation.
        assert owner.locator('#c-model').evaluate('(e)=>e.tagName') == 'SELECT'
        assert owner.locator('#c-model option').count() == 6
        expect(owner.locator('#c-go')).to_be_disabled()
        owner.fill('#c-name', 'Reading room')
        owner.select_option('#c-model', 'Qwen/Qwen3-0.6B')
        expect(owner.locator('#c-go')).to_be_enabled()
        expect(owner.locator('#c-quota')).to_be_hidden()
        owner.select_option('#c-mode', 'open')
        expect(owner.locator('#c-quota')).to_be_visible()
        create_swarm(owner, local, 'Reading room', 'request')
        sid = owner.locator('[data-owned]').first.get_attribute('data-owned')
        visitor = page_for(browser, local, '#chat/' + sid)
        errors = []
        visitor.on('pageerror', lambda e: errors.append(str(e)))
        # 6–10: fixed composer, note count/preservation, real signed request, reload persistence.
        expect(visitor.locator('#prompt')).to_be_disabled()
        visitor.fill('#note', 'Hi it is Sam')
        expect(visitor.locator('#cnt')).to_have_text('12 / 140')
        visitor.click('#refresh')
        expect(visitor.locator('#note')).to_have_value('Hi it is Sam')
        visitor.click('#ask')
        expect(visitor.locator('#gate-title')).to_have_text('Request sent')
        visitor.reload()
        expect(visitor.locator('#gate-title')).to_have_text('Request sent')
        expect(visitor.locator('#prompt')).to_be_disabled()
        # 11–14: owner badge, approval, consent, actual streamed answer and browser measurement.
        owner.click('#refresh')
        expect(owner.locator('#owned-count')).to_have_text('1')
        owner.locator('[data-approve]').click()
        visitor.click('#refresh')
        expect(visitor.locator('#consent')).to_be_visible()
        visitor.click('#consent')
        expect(visitor.locator('#prompt')).to_be_enabled()
        visitor.fill('#prompt', 'slow story')
        visitor.click('#send')
        expect(visitor.locator('#stop')).to_be_visible()
        visitor.click('#stop')
        expect(terminal(visitor, 'cancelled_by_you')).to_be_visible()
        visitor.fill('#prompt', 'capital of Germany?')
        visitor.click('#send')
        expect(terminal(visitor, 'completed')).to_be_visible()
        expect(visitor.locator('#log')).to_contain_text('tok/s')
        assert sum(visitor.evaluate('window.__cls')) < .01
        # 15–17: keyboard tab routing, search empty state, offline entries folded.
        visitor.goto(local['directory'].origin + '/swarms/#browse')
        visitor.focus('#tab-browse')
        visitor.keyboard.press('ArrowRight')
        expect(visitor.locator('#tab-chat')).to_have_attribute('aria-selected', 'true')
        visitor.click('#tab-browse')
        visitor.fill('#search', 'does-not-exist')
        expect(visitor.locator('#browse-empty')).to_be_visible()
        assert not visitor.locator('#offline-fold').evaluate('(e)=>e.open')
        # 18–20: approved-but-offline reason, human telemetry, no page errors.
        visitor.fill('#search', '')
        local['app'].owner_run.status = lambda: {'state': 'idle'}
        local['publisher'].tick()
        visitor.goto(local['directory'].origin + '/swarms/#chat/' + sid)
        visitor.click('#refresh')
        expect(visitor.locator('#gate-title')).to_contain_text('asleep')
        expect(visitor.locator('#prompt')).to_be_disabled()
        assert 'Unavailable (' not in visitor.locator('body').inner_text()
        assert not errors
        browser.close()


def test_registry_dropdown_matches_source():
    from html.parser import HTMLParser
    from mycelium_a13_macos.run_model import CHOICES
    class Options(HTMLParser):
        values = []
        inside = False
        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if tag == 'select': self.inside = attrs.get('id') == 'c-model'
            if tag == 'option' and self.inside and attrs.get('value'): self.values.append(attrs['value'])
        def handle_endtag(self, tag):
            if tag == 'select': self.inside = False
    parser = Options()
    parser.feed((Path(__file__).parents[2] / 'mycelium_swarm_directory/web/index.html').read_text())
    assert parser.values == [c.model.model_id for c in CHOICES]


@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_routes_slow_loading_cls_and_screenshots(local, width, height, theme):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        owner = page_for(browser, local, '#new')
        owner.select_option('#c-model', 'Qwen/Qwen3-0.6B')
        create_swarm(owner, local, 'Reading room', 'request')
        sid = owner.locator('[data-owned]').first.get_attribute('data-owned')
        results = []
        for route in ['browse', 'chat', 'contributing', 'owned', 'new', 'swarm/'+sid, 'chat/'+sid]:
            page = browser.new_context(viewport={'width':width,'height':height}, color_scheme=theme).new_page()
            page.add_init_script(CLS)
            # Delayed actual response, not invented JSON: exercise first-visit skeletons.
            def delayed(r):
                import time
                time.sleep(.7)
                r.continue_()
            page.route('**/swarms/api/v1/swarms', delayed)
            page.goto(local['directory'].origin+'/swarms/#'+route)
            page.wait_for_selector('body[data-loaded="1"]')
            page.wait_for_timeout(1200)
            cls = sum(page.evaluate('window.__cls'))
            assert cls < .01, (route,width,theme,cls)
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            name = route.split('/')[0] + ('-detail' if '/' in route else '') + f'-{width}-{theme}'
            results.append({'page':name, 'cls':cls})
            shots = os.environ.get('SITEB_SHOTS')
            if shots:
                Path(shots).mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(Path(shots)/(name+'.png')),full_page=True)
            page.context.close()
        if os.environ.get('SITEB_SHOTS'):
            (Path(os.environ['SITEB_SHOTS'])/f'cls-{width}-{theme}.json').write_text(json.dumps(results,indent=2)+'\n')
        browser.close()
