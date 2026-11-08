"""Unified viewport composition and staging fixtures, real local HTTP stack."""
import json
import pytest
from playwright.sync_api import sync_playwright,expect
from test_site_v2_browser import local,CLS
from scripts.staging_samples import publish

@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('width',[390,1440])
def test_unified_scene_has_reachable_field_and_bounded_buffers(local,theme,width):
    with sync_playwright() as p:
        b=p.chromium.launch();c=b.new_context(viewport={'width':width,'height':900},color_scheme=theme)
        c.add_init_script(CLS);page=c.new_page();page.goto(local['directory'].origin+'/')
        page.wait_for_function('!!window.__myceliumScene && !!window.__setUnderstoryTime')
        result=page.evaluate('''()=>{const s=__myceliumScene;
          const fieldEdges=s.routes.flatMap(r=>r.fieldList).filter(v=>s.graph.ss[v.segment].b.y>s.field.top);
          const oldBytes=__understoryAudit.bytes;document.body.style.minHeight='100000px';scrollTo(0,80000);__setPulseTime(24);
          return {fieldEdges:fieldEdges.length,shared:__understoryAudit.sharedGraph,density:__understoryAudit.count/__understoryAudit.baseCount,
            buffers:oldBytes===__understoryAudit.bytes,hidden:getComputedStyle(document.querySelector('.hero-art')).visibility};}''')
        assert result['shared'] and result['fieldEdges']>0 and result['buffers']
        assert 1.18 <= result['density'] <= 1.22
        assert result['hidden']=='hidden'
        page.emulate_media(reduced_motion='reduce');page.wait_for_timeout(100)
        frozen=page.locator('#page-mycelium').evaluate('(c)=>c.toDataURL()');page.wait_for_timeout(200)
        assert frozen==page.locator('#page-mycelium').evaluate('(c)=>c.toDataURL()')
        assert sum(page.evaluate('__cls'))==0
        b.close()

@pytest.mark.parametrize('theme',['dark','light'])
def test_sample_cards_and_unreachable_chat_never_contact_entries(local,tmp_path,theme):
    d=local['directory'].directory;local['directory'].staging_samples=True
    def get(path):
        status,value=d.handle('GET',path,b'','sample-test');assert status==200,value;return value
    def post(path,value):
        status,body=d.handle('POST',path,json.dumps(value).encode(),'sample-test');assert status==200,body;return body
    rows=publish(tmp_path/'samples',get,post,at=d.now())
    with sync_playwright() as p:
        b=p.chromium.launch();page=b.new_page(viewport={'width':390,'height':844},color_scheme=theme)
        page.add_init_script(CLS);requests=[];page.on('request',lambda r:requests.append(r.url))
        page.goto(local['directory'].origin+'/swarms/#browse');page.wait_for_selector('body[data-ready="1"]')
        expect(page.locator('#staging-banner')).to_be_visible()
        expect(page.locator('.card-performance-note').filter(has_text='Sample')).to_have_count(10)
        page.goto(local['directory'].origin+'/swarms/#chat/'+rows[0]['id'])
        page.wait_for_selector('body[data-ready="1"]');page.wait_for_timeout(1000)
        expect(page.locator('#prompt')).to_be_disabled()
        composer=page.locator('#prompt').bounding_box()
        assert composer and composer['y']+composer['height']<=844, composer
        assert all(url.startswith(local['directory'].origin+'/') for url in requests)
        assert sum(page.evaluate('__cls'))==0
        b.close()
