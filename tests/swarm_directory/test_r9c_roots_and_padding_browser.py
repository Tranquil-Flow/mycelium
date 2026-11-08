"""Owner regression: padded fantasy deposits and root-bearing sparse geometry."""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local

@pytest.mark.parametrize('dpr',[1,2])
def test_spore_rasters_have_clear_edges_at_icon_and_detail_sizes(local,dpr):
    with sync_playwright() as p:
        b=p.chromium.launch();page=b.new_page(device_scale_factor=dpr,reduced_motion='reduce')
        page.goto(local['directory'].origin+'/')
        bad=page.evaluate('''async dpr=>{const m=await import('/swarms/spore-print.js'),bad=[];
          for(let i=1;i<=48;i++)for(const size of [40,96]){
            const c=document.createElement('canvas');m.drawSpore(c,'sw-study-'+String(i).padStart(4,'0'),{size,dpr});
            const a=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
            let edge=0;for(let k=0;k<c.width;k++)for(const j of [k,c.width*(c.height-1)+k,k*c.width,k*c.width+c.width-1])edge+=a[j*4+3];
            if(edge)bad.push({i,size,edge});
          }return bad;}''',dpr)
        assert not bad,bad
        b.close()

@pytest.mark.parametrize('width',[390,1440,1920])
def test_sparse_roots_have_laterals_and_content_aware_margins(local,width):
    with sync_playwright() as p:
        b=p.chromium.launch();page=b.new_page(viewport={'width':width,'height':900})
        page.goto(local['directory'].origin+'/');page.wait_for_function('!!window.__understoryAudit')
        audit=page.evaluate('__understoryAudit')
        assert audit.get('loneThreads')==0,audit
        assert page.evaluate('''()=>__myceliumScene.field.strands.every(p=>p.segments.length===p.points.length-1)'''), 'Every visible field edge must exist in the shared graph'
        assert audit.get('minLateralsPer100px',0)>=4
        assert audit.get('explorerLoneThreads')==0
        assert audit['straightness']['understory']['whole']['p95']<.995
        assert audit.get('marginKnots',0)>0 if width>=1440 else audit.get('marginKnots',0)==0
        assert audit.get('marginDensityRatio')==.4
        # One clock must remain aligned after the hero spends time offscreen.
        page.evaluate('scrollTo(0,1600)');page.wait_for_timeout(350)
        page.evaluate('scrollTo(0,0)');page.wait_for_timeout(150)
        delta=page.evaluate("Math.abs(Number(document.querySelector('.pulse-canvas').dataset.time)-Number(document.querySelector('#page-mycelium').dataset.time))")
        assert delta<.08,delta
        pulses=page.evaluate('''()=>Array.from({length:1200},(_,i)=>__marginPulseProbe(i/2))''')
        assert all(len(s['active'])==len(set(s['active'])) for s in pulses)
        if width>=1440:
            assert any(s['active'] for s in pulses)
            assert page.evaluate('''()=>__myceliumScene.routes.every(r=>r.fieldList.every(v=>!__myceliumScene.graph.ss[v.segment].fieldPath.margin))'''), 'Hero leak must not add a second margin pulse'
        assert all(s['intensity']==.5 for s in pulses)
        page.set_viewport_size({'width':800,'height':900});page.wait_for_timeout(400)
        assert page.evaluate('__understoryAudit.marginKnots')==0
        b.close()
