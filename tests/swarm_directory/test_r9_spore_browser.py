"""Fantasy pigments, deterministic raster caching and bounded backing storage.

These are characterization checks of the procedural drawing, not a photo-realism oracle.
The named-asset test was red before the module was added; morphology is reviewed visually.
"""
import pytest
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local

@pytest.mark.parametrize('dpr',[1,2])
@pytest.mark.parametrize('theme',['dark','light'])
def test_spore_identity_is_deterministic_and_cached(local,dpr,theme):
    with sync_playwright() as p:
        browser=p.chromium.launch()
        page=browser.new_page(device_scale_factor=dpr,color_scheme=theme,reduced_motion='reduce')
        page.goto(local['directory'].origin+'/')
        result=page.evaluate('''async dpr=>{
            const m=await import('/swarms/spore-print.js');
            const draw=(id,size)=>{const c=document.createElement('canvas');m.drawSpore(c,id,{size,dpr});return c;};
            const first=draw('sw-study-0001',40),initial=m.sporeCacheStats(),again=draw('sw-study-0001',40),repeated=m.sporeCacheStats();
            const detail=draw('sw-study-0001',96),other=draw('sw-study-0002',40);
            const centre=Array.from(detail.getContext('2d').getImageData(detail.width/2,detail.height/2,1,1).data);
            document.documentElement.dataset.glow='blue';
            const afterHue=draw('sw-study-0001',40);
            const specs=Array.from({length:24},(_,i)=>m.sporeSpec('sw-study-'+String(i+1).padStart(4,'0')));
            const seed=m.sporeSpec('sw-study-0001');
            return {same:first.toDataURL()===again.toDataURL(),different:first.toDataURL()!==other.toDataURL(),hueIndependent:first.toDataURL()===afterHue.toDataURL(),initial,repeated,small:first.width,detail:detail.width,centre,specs,seedAgain:m.sporeSpec('sw-study-0001'),seed};
        }''',dpr)
        assert result['same'] and result['different'] and result['hueIndependent']
        assert result['initial']['renders']==result['repeated']['renders']
        assert result['repeated']['hits']==result['initial']['hits']+1
        assert result['small']==40*dpr and result['detail']==96*dpr
        assert result['centre'][3] > 0  # stem/backing can be dark or off-centre
        assert result['seed']==result['seedAgain']
        assert len({s['colour'] for s in result['specs']}) >= 8
        assert 0<sum(s['type']=='pores' for s in result['specs'])<12
        bounded=page.evaluate('''async()=>{const m=await import('/swarms/spore-print.js');for(let i=0;i<20;i++){const c=document.createElement('canvas');m.drawSpore(c,'sw-large-study-'+i,{size:128,dpr:3});}return m.sporeCacheStats();}''')
        assert bounded['bytes']<=bounded['maxBytes'] and bounded['entries']<=bounded['maxEntries']
        browser.close()
