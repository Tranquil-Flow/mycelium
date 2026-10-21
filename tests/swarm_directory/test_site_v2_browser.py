# SPDX-License-Identifier: AGPL-3.0-only
"""Real local directory + entry, scripted generation only. No live website traffic."""
import json
import os
from pathlib import Path

import pytest
from playwright.sync_api import expect, sync_playwright
from test_hosted_ui_browser import make_stack, close_stack, create_swarm, terminal

CLS = """window.__cls=[];window.__clsDetails=[];
new PerformanceObserver(l=>{for(const e of l.getEntries())if(!e.hadRecentInput){
window.__cls.push(e.value);window.__clsDetails.push({value:e.value,time:e.startTime,sources:e.sources.map(s=>({
selector:s.node?(s.node.id?'#'+s.node.id:s.node.tagName+'.'+String(s.node.className).replaceAll(' ','.')):'detached',
previous:s.previousRect.toJSON(),current:s.currentRect.toJSON()}))});
}}).observe({type:'layout-shift',buffered:true})"""

@pytest.fixture
def local(tmp_path):
    import time
    from test_hosted_ui_browser import RUN_DOC
    value = make_stack(tmp_path)
    started = int(time.time()) - 125
    value['app'].owner_run.status = lambda: dict(RUN_DOC, started_at=started)
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
        with owner.expect_response(lambda r: r.url.endswith('/grants') and r.request.method == 'POST', timeout=10000) as saved:
            owner.locator('[data-approve]').click()
        assert saved.value.status == 200
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


@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_apex_real_counts_delayed_and_unavailable_without_identity(local, monkeypatch, width, height, theme):
    import time
    from mycelium_swarm_directory import docs
    from mycelium_swarm_directory.crypto import Ed25519Signer
    d = local['directory'].directory
    signer = Ed25519Signer.generate()
    sid = 'sw-aaaaaaaaaaaaaaaaaaaa'
    document = docs.swarm_record(signer, swarm_id=sid, seq=1, name='One Mac test route', model='Qwen/Qwen3-0.6B', access_mode='request')
    assert d.handle('POST','/records',json.dumps(document).encode(),'ip:fixture')[0] == 200
    beat = docs.heartbeat(signer,sid,status='serving',members=1,served_model='Qwen/Qwen3-0.6B',at=d.now())
    assert d.handle('POST','/heartbeat',json.dumps(beat).encode(),'ip:fixture')[0] == 200
    original = d.list_swarms
    def delayed():
        time.sleep(.7)  # server thread only; CSS and script are NOT blocked
        return original()
    monkeypatch.setattr(d,'list_swarms',delayed)
    with sync_playwright() as p:
        browser=p.chromium.launch()
        for failed in (False,True):
            page=browser.new_context(viewport={'width':width,'height':height},color_scheme=theme).new_page()
            page.add_init_script(CLS)
            requests=[]; page.on('request',lambda r: requests.append((r.method,r.url)))
            if failed: page.route('**/swarms/api/v1/swarms',lambda r:r.fulfill(status=503,body='{}',content_type='application/json'))
            page.goto(local['directory'].origin+'/')
            if failed:
                expect(page.locator('#apex-fresh')).to_contain_text('Status unavailable')
                expect(page.locator('#serving-count')).to_have_text('—')
            else:
                expect(page.locator('#serving-count')).to_have_text('1')
                expect(page.locator('#mac-count')).to_have_text('1')
            page.wait_for_timeout(700)
            cls=sum(page.evaluate('window.__cls'))
            assert cls < .01, (width,theme,failed,cls,page.evaluate('window.__clsDetails'))
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.evaluate('async()=> (await indexedDB.databases()).length') == 0
            assert page.evaluate('localStorage.length + sessionStorage.length') == 0
            assert all(method=='GET' and url.startswith(local['directory'].origin) for method,url in requests)
            expect(page.get_by_role('link',name='Try the demo chat')).to_have_attribute('href','https://demo.mycelium.now/chat')
            if os.environ.get('SITEB_SHOTS'):
                out=Path(os.environ['SITEB_SHOTS']);out.mkdir(parents=True,exist_ok=True)
                name=f'apex-{width}-{theme}'+('-unavailable' if failed else '')
                page.screenshot(path=str(out/(name+'.png')),full_page=True)
                (out/(name+'.cls.json')).write_text(json.dumps({'cls':cls,'shifts':page.evaluate('window.__clsDetails')},indent=2))
            page.context.close()
        browser.close()


def test_owner_undo_cancels_write_and_reload_cancels_hold(local):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        owner=page_for(browser,local,'#new')
        create_swarm(owner,local,'Undo room','request')
        sid=owner.locator('[data-owned]').first.get_attribute('data-owned')
        visitor=page_for(browser,local,'#chat/'+sid)
        visitor.click('#ask')
        expect(visitor.locator('#gate')).to_have_attribute('data-state','pending')
        owner.click('#refresh');expect(owner.locator('#owned-count')).to_have_text('1')
        writes=[]
        owner.on('request',lambda r:writes.append(r.url) if r.method=='POST' and r.url.endswith(('/grants','/deny')) else None)
        row=owner.locator('.request')
        for label in ('Approve','Decline'):
            row.get_by_role('button',name=label,exact=True).click()
            expect(row.locator('[data-undo]')).to_be_visible()
            expect(row.locator('[data-decision]')).to_contain_text('in 5 seconds')
            if os.environ.get('SITEB_SHOTS'):
                out=Path(os.environ['SITEB_SHOTS']);out.mkdir(parents=True,exist_ok=True)
                owner.screenshot(path=str(out/f'owner-held-{label.lower()}.png'),full_page=True)
            row.locator('[data-undo]').click()
            owner.wait_for_timeout(5200)
            assert writes == []
            visitor.click('#refresh');expect(visitor.locator('#gate')).to_have_attribute('data-state','pending')
        row.get_by_role('button',name='Approve',exact=True).click()
        owner.reload();owner.wait_for_selector('body[data-ready="1"]');owner.wait_for_timeout(5200)
        assert writes == []
        expect(owner.locator('#owned-count')).to_have_text('1')
        with owner.expect_response(lambda r:r.url.endswith('/grants') and r.request.method=='POST',timeout=10000) as saved:
            owner.locator('[data-approve]').click()
        assert saved.value.status == 200 and len(writes)==1
        visitor.click('#refresh');expect(visitor.locator('#consent')).to_be_visible()
        expect(owner.locator('#owned-count')).to_have_text('0')
        assert sum(owner.evaluate('window.__cls')) < .01, owner.evaluate('window.__clsDetails')
        browser.close()


@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
def test_mac_app_header_inbox_uses_real_owner_requests(local, width, height):
    """Shipped app HTML/CSS/swarm.js; loopback HTTP facade over the real publisher.

    Other app panels are inert here: no device action, model launch or live app traffic.
    """
    import re
    from mycelium_swarm_directory import docs
    from mycelium_swarm_directory.crypto import Ed25519Signer
    web=Path(__file__).parents[2]/'mycelium_a13_macos/web'
    pub=local['publisher']
    pub.publish({'directory':local['directory'].origin,'name':'Mac owner inbox','access_mode':'request'})
    sid=pub.state['swarm_id']
    visitor=Ed25519Signer.generate()
    d=local['directory'].directory
    body=docs.access_request(visitor,sid,note='Please approve',at=d.now())
    assert d.handle('POST','/access-requests',json.dumps(body).encode(),'ip:fixture')[0]==200
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_context(viewport={'width':width,'height':height}).new_page()
        page.add_init_script(CLS)
        def facade(route):
            path=route.request.url.split(local['directory'].origin,1)[-1]
            if path=='/mac-owner':
                html=re.sub(r'<script defer src="(?!/swarm.js)[^"]+"></script>','', (web/'index.html').read_text()).replace('id="private" hidden','id="private"')
                return route.fulfill(body=html,content_type='text/html')
            if path in ('/style.css','/swarm.js'):
                return route.fulfill(body=(web/path[1:]).read_text(),content_type='text/css' if path.endswith('css') else 'text/javascript')
            if path=='/api/status':
                return route.fulfill(json={'installation_role':'owner','swarm':pub.status()})
            if path=='/api/swarm/inbox':
                return route.fulfill(json=pub.inbox({}))
            route.abort()  # no unmatched requests leave the local facade
        page.route('**/*',facade)
        page.goto(local['directory'].origin+'/mac-owner')
        expect(page.locator('#swarm-inbox-count')).to_have_text('1')
        expect(page.locator('#swarm-inbox-header')).to_be_visible()
        box=page.locator('#swarm-inbox-header').bounding_box()
        assert box and box['x']>=0 and box['x']+box['width']<=width
        if os.environ.get('SITEB_SHOTS'):
            out=Path(os.environ['SITEB_SHOTS']);out.mkdir(parents=True,exist_ok=True)
            page.screenshot(path=str(out/f'mac-header-{width}.png'))
        # Native anchor targets the real inbox; fetching is authenticated by the publisher.
        page.click('#swarm-inbox-header')
        expect(page.locator('#swarm-requests')).to_contain_text('Please approve')
        pub.deny({'requester_key':visitor.public_key})
        page.click('#swarm-inbox-header')
        expect(page.locator('#swarm-inbox-count')).to_have_text('0')
        page.wait_for_timeout(700)
        assert sum(page.evaluate('window.__cls')) < .01, page.evaluate('window.__clsDetails')
        browser.close()


def test_stale_browse_and_owner_republish_real_record(local):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        owner=page_for(browser,local,'#new')
        owner.fill('#c-name','Sleeping room');owner.select_option('#c-model','Qwen/Qwen3-0.6B');owner.click('#c-go')
        expect(owner.locator('[data-owned]')).to_be_visible()
        sid=owner.locator('[data-owned]').get_attribute('data-owned')
        d=local['directory'].directory
        visitor=page_for(browser,local,'#browse')
        visitor.locator('#offline-summary').click()
        expect(visitor.locator('.swarm',has_text='Sleeping room')).to_be_visible()
        row=d.store.get('swarm',sid);row['offline_since']=d.now()-d.limits.offline_hide_seconds;d.store.put('swarm',sid,row)
        visitor.click('#refresh')
        expect(visitor.locator('#offline-summary')).to_have_text('Show offline (0)')
        expect(visitor.locator('.swarm',has_text='Sleeping room')).to_be_hidden()
        row['offline_since']=d.now()-d.limits.offline_delist_seconds;d.store.put('swarm',sid,row)
        owner.click('#refresh')
        expect(owner.locator('[data-owned]')).to_be_visible()
        with owner.expect_response(lambda r:r.url.endswith('/records') and r.request.method=='POST') as saved:
            owner.get_by_role('button',name='Republish listing').click()
        assert saved.value.status==200
        visitor.click('#refresh')
        expect(visitor.locator('#offline-summary')).to_have_text('Show offline (1)')
        expect(visitor.locator('.swarm',has_text='Sleeping room')).to_be_visible()
        assert d.store.get('swarm',sid)['record']['seq']==2
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
    from scripts.generate_site_models import rendered
    source = (Path(__file__).parents[2] / 'mycelium_swarm_directory/web/index.html').read_text()
    assert rendered(source) == source  # also pins download size, label and layers


@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_access_states_real_entry_and_composer_geometry(local, width, height, theme):
    from test_hosted_ui_browser import RUN_DOC
    with sync_playwright() as p:
        browser = p.chromium.launch()
        owner = page_for(browser, local, '#new', viewport={'width':width,'height':height}, color_scheme=theme)
        create_swarm(owner, local, 'Reading room', 'request')
        sid = owner.locator('[data-owned]').first.get_attribute('data-owned')
        visitor = page_for(browser, local, '#chat/'+sid, viewport={'width':width,'height':height}, color_scheme=theme)
        public_key = visitor.evaluate('me.key')
        results = []
        def capture(state):
            expect(visitor.locator('#gate')).to_have_attribute('data-state',state)
            if state != 'ready': expect(visitor.locator('#prompt')).to_be_disabled()
            else: expect(visitor.locator('#prompt')).to_be_enabled()
            # Measure before screenshots/locator clicks can auto-scroll the page.
            rect = visitor.locator('#prompt').bounding_box()
            assert rect and rect['y'] >= 0 and rect['y'] + rect['height'] < height - (58 if width < 640 else 0)
            assert visitor.evaluate('window.scrollY') == 0
            visitor.wait_for_timeout(550)
            cls = sum(visitor.evaluate('window.__cls'))
            assert cls < .01, (state,width,theme,cls,visitor.evaluate('window.__clsDetails'))
            results.append({'state':state,'cls':cls,'shifts':visitor.evaluate('window.__clsDetails')})
            if os.environ.get('SITEB_SHOTS'):
                out = Path(os.environ['SITEB_SHOTS']); out.mkdir(parents=True,exist_ok=True)
                name = f'access-{state}-{width}-{theme}'
                visitor.screenshot(path=str(out/(name+'.png')),full_page=True)
                (out/(name+'.aria.txt')).write_text(visitor.locator('#chat-card').aria_snapshot())
        capture('needs-request')
        visitor.fill('#note','Hello, can I join?')
        visitor.click('#ask'); capture('pending')
        visitor.reload(); capture('pending')
        owner.click('#refresh')
        expect(owner.locator('#owned-count')).to_have_text('1')
        if os.environ.get('SITEB_SHOTS'):
            owner.locator('[data-owner-grants]').scroll_into_view_if_needed()
            owner.screenshot(path=str(Path(os.environ['SITEB_SHOTS'])/f'owner-inbox-{width}-{theme}.png'),full_page=True)
        with owner.expect_response(lambda r: r.url.endswith('/deny') and r.request.method == 'POST', timeout=10000) as saved:
            owner.locator('.request button',has_text='Decline').click()
        assert saved.value.status == 200
        visitor.click('#refresh'); capture('declined')
        expect(visitor.locator('#ask')).to_be_disabled()
        expect(visitor.locator('#gate-copy')).to_contain_text('ask again after')
        owner.click('#refresh')
        row = owner.locator('.request')
        row.locator('[aria-label=Hours]').fill('6')
        owner.click('#refresh')
        expect(row.locator('[aria-label=Hours]')).to_have_value('6')
        with owner.expect_response(lambda r: r.url.endswith('/grants') and r.request.method == 'POST', timeout=10000) as saved:
            owner.locator('[data-approve]').click()
        assert saved.value.status == 200
        visitor.click('#refresh'); capture('approved')
        visitor.click('#consent'); capture('ready')
        visitor.fill('#prompt','kept while asleep')
        local['app'].owner_run.status = lambda: {'state':'idle'}
        local['publisher'].tick()
        visitor.click('#refresh'); capture('offline')
        expect(visitor.locator('#prompt')).to_have_value('kept while asleep')
        local['app'].owner_run.status = lambda: dict(RUN_DOC)
        local['publisher'].tick()
        owner.click('#refresh')
        owner.locator('[data-kill]').click()
        visitor.click('#refresh'); capture('killed')
        owner.locator('[data-kill]').click()
        visitor.click('#refresh')
        # Real short-lived signed allowance: no fake page state or time override.
        owner.evaluate('async key => { await approve(swarms[0], key, 2/3600, 1, 2000); }', public_key)
        visitor.click('#refresh')
        visitor.wait_for_timeout(2100)
        visitor.click('#refresh'); capture('expired')
        owner.evaluate('async key => { await approve(swarms[0], key, 1, 1, 2000); }', public_key)
        visitor.click('#refresh')
        visitor.fill('#prompt','capital of Germany?'); visitor.click('#send')
        expect(terminal(visitor,'completed')).to_be_visible()
        visitor.fill('#prompt','second prompt'); visitor.click('#send')
        expect(terminal(visitor,'quota')).to_be_visible(); capture('quota')
        visitor.click('#retry-access')
        expect(visitor.locator('#gate-title')).to_have_text('Request sent')
        visitor.reload()
        expect(visitor.locator('#gate-title')).to_have_text('Request sent')
        owner.click('#refresh')
        with owner.expect_response(lambda r: r.url.endswith('/grants') and r.request.method == 'POST', timeout=10000) as saved:
            owner.locator('[data-approve]').click()
        assert saved.value.status == 200
        visitor.click('#refresh')
        visitor.click('#consent')
        expect(visitor.locator('#prompt')).to_be_enabled()
        visitor.fill('#prompt','new allowance works'); visitor.click('#send')
        expect(terminal(visitor,'completed')).to_be_visible()
        # A fresh key sees invite-only, not a phantom request button.
        owner.select_option('.access-mode','invite'); owner.click('.save-access')
        expect(owner.locator('.access-state')).to_contain_text('Saved')
        local['publisher'].tick()
        visitor.context.close()
        visitor = page_for(browser,local,'#chat/'+sid,viewport={'width':width,'height':height},color_scheme=theme)
        capture('invite')
        owner.select_option('.access-mode','open'); owner.click('.save-access')
        expect(owner.locator('.access-state')).to_contain_text('Saved')
        local['publisher'].tick()
        visitor.click('#refresh'); capture('first')
        if os.environ.get('SITEB_SHOTS'):
            (Path(os.environ['SITEB_SHOTS'])/f'cls-access-{width}-{theme}.json').write_text(json.dumps(results,indent=2)+'\n')
        browser.close()


def test_busy_wait_is_cancellable_and_prompts_never_go_to_directory(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        owner = page_for(browser,local,'#new')
        create_swarm(owner,local,'Reading room','open')
        sid = owner.locator('[data-owned]').first.get_attribute('data-owned')
        first = page_for(browser,local,'#chat/'+sid)
        second = page_for(browser,local,'#chat/'+sid)
        posts=[]
        second.on('request',lambda r: posts.append((r.url,r.post_data)) if r.method=='POST' else None)
        for page in (first,second):
            page.click('#consent'); expect(page.locator('#prompt')).to_be_enabled()
        first.fill('#prompt','slow story'); first.click('#send')
        expect(first.locator('#log')).to_contain_text('tok1')
        second.fill('#prompt','capital of Germany?'); second.click('#send')
        expect(second.locator('#chat-state')).to_contain_text('Waiting for your turn')
        first.click('#stop')
        expect(terminal(second,'completed')).to_be_visible(timeout=15000)
        first.fill('#prompt','slow story again'); first.click('#send')
        expect(first.locator('#log')).to_contain_text('tok2')
        second.fill('#prompt','cancel this waiting draft'); second.click('#send')
        expect(second.locator('#chat-state')).to_contain_text('Waiting for your turn')
        second.click('#stop')
        expect(terminal(second,'cancelled_by_you')).to_be_visible()
        expect(second.locator('#prompt')).to_have_value('cancel this waiting draft')
        first.click('#stop')
        assert any(url.startswith(local['proxy'].origin) and 'capital of Germany?' in (body or '') for url,body in posts)
        assert not any(url.startswith(local['directory'].origin) and 'capital of Germany?' in (body or '') for url,body in posts)
        # Even a malicious listing cannot make the page POST a prompt back to its own apex.
        result=second.evaluate("async()=>{try{await entry(location.origin,'send',{prompt:'must-not-leave'});return 'sent';}catch(e){return e.message;}}")
        assert result=='entry_is_directory'
        assert not any('must-not-leave' in (body or '') for _,body in posts)
        browser.close()


@pytest.mark.parametrize('width,height', [(1440,900),(390,844)])
@pytest.mark.parametrize('theme', ['dark','light'])
def test_routes_slow_loading_cls_and_screenshots(local, monkeypatch, width, height, theme):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        owner = page_for(browser, local, '#new')
        owner.select_option('#c-model', 'Qwen/Qwen3-0.6B')
        create_swarm(owner, local, 'Reading room', 'request')
        sid = owner.locator('[data-owned]').first.get_attribute('data-owned')
        results = []
        from mycelium_swarm_directory import server as directory_server
        original_get=directory_server._Handler.do_GET
        def delayed_get(handler):
            import time
            if handler.path in ('/swarms/api/v1/swarms','/swarms/swarms.js'):
                time.sleep(.7)  # per-HTTP-request thread: CSS delivery and browser dispatch stay live
            return original_get(handler)
        monkeypatch.setattr(directory_server._Handler,'do_GET',delayed_get)
        for route in ['browse', 'chat', 'contributing', 'owned', 'new', 'notice', 'swarm/'+sid, 'chat/'+sid]:
            page = browser.new_context(viewport={'width':width,'height':height}, color_scheme=theme).new_page()
            page.add_init_script(CLS)
            # Real responses delayed by the server, without blocking CSS or Playwright's event loop.
            page.goto(local['directory'].origin+'/swarms/#'+route)
            page.wait_for_selector('body[data-loaded="1"]')
            page.wait_for_timeout(1200)
            cls = sum(page.evaluate('window.__cls'))
            assert cls < .01, (route,width,theme,cls,page.evaluate('window.__clsDetails'))
            assert page.evaluate('window.scrollY') == 0, ('hash route must not jump to a same-name DOM id', route)
            if route == 'owned':
                empty = page.locator('#owned-empty').bounding_box()
                hint = page.locator('#owned-card > .hint').bounding_box()
                assert empty['y'] >= hint['y'] + hint['height']
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            name = route.split('/')[0] + ('-detail' if '/' in route else '') + f'-{width}-{theme}'
            results.append({'page':name, 'cls':cls, 'shifts':page.evaluate('window.__clsDetails')})
            shots = os.environ.get('SITEB_SHOTS')
            if shots:
                Path(shots).mkdir(parents=True,exist_ok=True)
                page.screenshot(path=str(Path(shots)/(name+'.png')),full_page=True)
            page.context.close()
        if os.environ.get('SITEB_SHOTS'):
            (Path(os.environ['SITEB_SHOTS'])/f'cls-{width}-{theme}.json').write_text(json.dumps(results,indent=2)+'\n')
        browser.close()
