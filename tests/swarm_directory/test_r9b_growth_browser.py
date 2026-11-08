"""Fixed laid geometry, monotone tips, connected fusion and post-fusion forks."""
from playwright.sync_api import sync_playwright
from test_site_v2_browser import local


def test_dendritic_tips_and_fusions_are_connected_and_fixed(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={'width':1440,'height':900})
        page.goto(local['directory'].origin + '/')
        page.wait_for_function('!!window.__setPulseTime')
        assert page.evaluate('typeof window.__dendriticProbe') == 'function'
        result = page.evaluate('''()=>{
          const before=JSON.stringify(__rootPaths),samples=[];
          for(let i=0;i<180;i++){__setPulseTime(i/6);samples.push(__dendriticProbe(i/6))}
          return {fixed:before===JSON.stringify(__rootPaths),samples,field:__understoryAudit};
        }''')
        assert result['fixed']
        assert min(s['activeTips'] for s in result['samples']) > 0
        assert all(s['fusions'] > 0 and s['connectedSplits'] == s['fusions'] for s in result['samples'])
        assert all(s['regressingTips'] == 0 for s in result['samples'])
        assert result['field']['fusions'] > 0 and result['field']['branches'] > 0
        browser.close()
