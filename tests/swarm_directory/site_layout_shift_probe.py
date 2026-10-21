"""Reproduce both historical CLS defects against LOCAL servers only.

This diagnostic deliberately restores old behavior in intercepted local asset
responses, without changing source files. The negative controls must fail the
layout budget; the current app must pass. Requires the browser-test environment.
"""
import sys,json,re,asyncio
from pathlib import Path
root=Path(__file__).resolve().parents[2]
output=root/'docs/design/site-v2-local/negative-controls'
output.mkdir(parents=True,exist_ok=True)
sys.path[:0]=[str(root),str(root/'tests/swarm_directory')]
from playwright.async_api import async_playwright
from mycelium_swarm_directory.server import DirectoryServer
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.store import MemoryStore
from mycelium_swarm_directory.crypto import verify
from test_site_v2_browser import CLS
async def main():
  results=[]
  server=DirectoryServer(Directory(MemoryStore(),verify,allow_loopback_entries=True)).start()
  try:
    async with async_playwright() as p:
      browser=await p.chromium.launch()
      for scenario in ['startup-negative','startup-fixed','privacy-negative','privacy-fixed']:
        mobile=scenario.startswith('startup')
        context=await browser.new_context(viewport={'width':390 if mobile else 1440,'height':844 if mobile else 900},color_scheme='dark')
        page=await context.new_page();await page.add_init_script(CLS)
        errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
        async def delayed(route):
          response=await route.fetch();await asyncio.sleep(1);await route.fulfill(response=response)
        await page.route('**/swarms/swarms.js',delayed)
        await page.route('**/swarms/api/v1/config',delayed)
        if scenario.endswith('negative'):
          async def old_notice_markup(route):
            response=await route.fetch();html=await response.text()
            await route.fulfill(response=response,body=html.replace('id="privacy-notice"','id="notice"'))
          if not mobile:await page.route('**/swarms/',old_notice_markup)
          async def mutate(route):
            response=await route.fetch();css=await response.text()
            css=css.replace('body:not([data-route]) main,body:not([data-route]) footer{visibility:hidden}','')
            if not mobile:css=re.sub(r'#(?:privacy|demo)\{height:\d+px(?:;overflow:auto)?\}', '',css)
            await route.fulfill(response=response,body=css)
          await page.route('**/swarms/swarms.css',mutate)
        await page.goto(server.origin+'/swarms/#'+('new' if mobile else 'notice'))
        await page.wait_for_selector('body[data-ready="1"]');await page.wait_for_timeout(700)
        values=await page.evaluate('({cls:window.__cls.reduce((a,b)=>a+b,0),shifts:window.__clsDetails,route:document.body.dataset.route,privacyHeight:getComputedStyle(document.getElementById("privacy")).height,privacyChars:document.getElementById("privacy").textContent.length})')
        values.update(scenario=scenario,errors=errors);results.append(values);print(json.dumps(values),flush=True)
        await page.screenshot(path=str(output/f'{scenario}.png'),full_page=True)
        await context.close()
      await browser.close()
  finally:server.close()
  (output/'results.json').write_text(json.dumps(results,indent=2)+'\n')
  assert all(x['cls']<.01 for x in results if x['scenario'].endswith('fixed'))
  assert all(x['cls']>=.01 for x in results if x['scenario'].endswith('negative'))
asyncio.run(main())
