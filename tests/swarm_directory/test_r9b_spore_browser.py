"""Fantasy-family acceptance. Visual morphology is also reviewed on the sheet."""
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local


def test_fantasy_family_is_varied_and_regenerates_exactly(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(reduced_motion='reduce')
        page.goto(local['directory'].origin + '/')
        result = page.evaluate('''async()=>{
          const m=await import('/swarms/spore-print.js');
          const ids=Array.from({length:48},(_,i)=>'sw-study-'+String(i+1).padStart(4,'0'));
          const draw=(id)=>{const c=document.createElement('canvas');m.drawSpore(c,id,{size:40,dpr:1});return c.toDataURL()};
          const prints=ids.map(draw),specs=ids.map(m.sporeSpec);
          for(let i=0;i<140;i++)draw('evict-'+i);
          return {specs,unique:new Set(prints).size,regenerated:draw(ids[0])===prints[0],cache:m.sporeCacheStats()};
        }''')
        assert {s.get('archetype') for s in result['specs']} == {
            'vortex', 'lightning', 'halo', 'petal', 'twins', 'crystal', 'constellation', 'aurora'
        }
        assert {'violet','magenta','teal','gold','ember','frost','ink-violet','iridescent'} <= {s['colour'] for s in result['specs']}
        assert 10 <= sum(s['colour'] in ('copper','ink-violet') for s in result['specs']) <= 12
        muted={s['archetype'] for s in result['specs'] if s['colour'] in ('copper','ink-violet')}
        assert len(muted)>=6, 'pigment mood must not be coupled to two archetypes'
        assert result['unique'] == 48 and result['regenerated']
        assert result['cache']['entries'] <= result['cache']['maxEntries']
        assert result['cache']['bytes'] <= result['cache']['maxBytes']
        browser.close()
