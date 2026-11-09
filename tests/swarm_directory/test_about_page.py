# SPDX-License-Identifier: AGPL-3.0-only
"""/about: the owner-approved vision page (Simple | Technical) and its links.

Renamed from /vision (owner, 9 Nov 2026): the route is /about, /vision is a
301 to /about (browsers keep the #fragment), the header link is 'About', the
homepage's closing link 'About Mycelium →' and the title 'About · Mycelium';
the page keeps its 'Our vision' eyebrow and H1.

Static checks pin the route, the copy boundaries ("Today" next to every
direction claim), the asset allowlists and staging isolation. Browser checks
cover the switch (persistence, hash deep links, keyboard, no-JS), the header
and homepage links, CLS 0, no third-party requests and text contrast measured
on the actual backdrop pixels under the glyphs, in both themes and both hues.
"""
import http.client
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser

import numpy as np
import pytest
from PIL import Image
import io
from playwright.sync_api import expect, sync_playwright

from mycelium_demo import public_origin_proxy as proxy
from mycelium_swarm_directory import server
from mycelium_swarm_directory.crypto import verify
from mycelium_swarm_directory.directory import Directory
from mycelium_swarm_directory.server import DirectoryServer, csp
from mycelium_swarm_directory.store import MemoryStore

VISION = server.WEB / 'about.html'
HOME = server.WEB / 'apex.html'
GLOW_KEY = 'mycelium-site-glow-v1'
CLS = """window.__cls=[];new PerformanceObserver(l=>{for(const e of l.getEntries())
if(!e.hadRecentInput)window.__cls.push(e.value)}).observe({type:'layout-shift',buffered:true});"""


def fetch(url):
    try:
        with urllib.request.urlopen(url) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), b''


@pytest.fixture
def directory(monkeypatch):
    monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    srv = DirectoryServer(Directory(MemoryStore(), verify)).start()
    try:
        yield srv
    finally:
        srv.close()


class Tree(HTMLParser):
    """Text per data-aim / data-panel / table row, plus every link and resource."""

    def __init__(self):
        super().__init__()
        self.stack, self.links, self.resources, self.aims, self.rows = [], [], [], {}, {}
        self.panels = {}
        self.today = 0
        self.aim = self.panel = self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'a':
            self.links.append(a)
        if tag in ('script', 'link') and (a.get('src') or a.get('href')):
            self.resources.append(a.get('src') or a.get('href'))
        if 'data-panel' in a:
            self.panel = a['data-panel']
            self.panels[self.panel] = ''
        if 'data-aim' in a:
            self.aim = a['data-aim']
            self.aims[self.aim] = {'text': '', 'today': ''}
        if 'data-table' in a:
            self.table = a['data-table']
            self.rows[self.table] = []
        if tag == 'tr' and self.panel:
            self.row = []
        if tag == 'td' and self.row is not None:
            self.row.append('')
        if 'goal-today' in (a.get('class') or '').split():
            self.today += 1
        self.stack.append((tag, 'goal-today' in (a.get('class') or '').split()))

    def handle_endtag(self, tag):
        while self.stack:
            t, today = self.stack.pop()
            if today:
                self.today -= 1
            if t == tag:
                break
        if tag in ('article', 'section'):
            self.aim = None
        if tag == 'tr' and self.row:
            self.rows[self.table].append(self.row)
            self.row = None
        if tag == 'div' and self.panel and not any(t == 'div' for t, _ in self.stack[2:]):
            pass

    def handle_data(self, data):
        if self.panel:
            self.panels[self.panel] += data
        if self.aim:
            self.aims[self.aim]['text'] += data
            if self.today:
                self.aims[self.aim]['today'] += data
        if self.row is not None and self.row:
            self.row[-1] += data


def parse(path):
    tree = Tree()
    tree.feed(path.read_text())
    return tree


# ---- static: route, copy boundaries, allowlists, staging isolation ----

def test_vision_route_serves_both_versions_with_the_site_csp(directory):
    status, headers, body = fetch(directory.origin + '/about')
    assert status == 200 and body == VISION.read_bytes()
    assert headers['Content-Type'] == 'text/html; charset=utf-8'
    assert headers['Content-Security-Policy'] == csp(False)
    assert headers['Cache-Control'] == 'no-store' and 'Set-Cookie' not in headers
    html = body.decode()
    assert html.count('data-panel="simple"') == 1 and html.count('data-panel="technical"') == 1
    for line in ('AI that belongs to the people who use it', 'The short version', 'How it works', "Who's in charge",
                 'Access is not charity', 'Built for the people usually left out', 'Where we are',
                 "What we haven't solved", 'Part of something bigger', 'The problem, precisely', 'How Mycelium works',
                 'What runs today', 'Honest limits today', "What we're building towards", 'The wider stack (CCAI)',
                 'Principles',
                 'We take no venture capital, so there are no investors to repay',
                 "<strong>We're building AI owned by the people who use it.</strong>"):
        assert line in html, line
    assert 'Our vision' in html and '<title>About · Mycelium</title>' in html
    assert '<h1 id="vision-title">AI that belongs to the people who use it</h1>' in html
    # Owner decisions: no Berlin, no tokens, CCAI named, no vault rename.
    lowered = html.lower()
    assert 'berlin' not in lowered and 'token' not in lowered and 'ddai' not in lowered
    assert 'Collective Commons AI' in html
    for path in ('/about/', '/about.html', '/about?mode=technical', '/about#technical', '/vision/', '/vision.html',
                 '/vision?mode=technical', '/swarms/vision.css', '/swarms/vision.js', '/vision.html'):
        assert fetch(directory.origin + path.replace('#', '%23'))[0] == 404, path


def test_old_vision_route_is_a_permanent_redirect_to_about(directory):
    for method in ('GET', 'HEAD'):
        conn = http.client.HTTPConnection(directory.origin.split('//', 1)[1])
        conn.request(method, '/vision')
        response = conn.getresponse()
        assert response.status == 301 and response.getheader('Location') == '/about', (method, response.status)
        assert response.getheader('Content-Length') == '0' and response.read() == b''
        assert response.getheader('Content-Security-Policy') == csp(False) and response.getheader('Set-Cookie') is None
        conn.close()
    # Through the apex proxy's allowlist too (the directory answers the 301).
    assert proxy._directory_route_decision('GET', '/vision') == (True, 200, 'ok')
    assert proxy._directory_route_decision('POST', '/vision')[0] is False


def test_every_direction_claim_sits_next_to_an_honest_today_line():
    # Simple v3 (owner, 9 Nov): each direction section ends with its 'Today' line.
    tree = parse(VISION)
    assert set(tree.aims) == {'how', 'in-charge', 'access', 'solved', 't-how'}
    for aim in tree.aims.values():
        assert aim['today'].startswith('Today'), aim
    assert 'download it for Apple-silicon Macs' in tree.aims['how']['today']
    assert 'the person who starts a swarm coordinates it' in tree.aims['in-charge']['today']
    access = tree.aims['access']
    assert 'will fund a shared AI Commons' in access['text'] and 'no payments' in access['today']
    assert 'The Commons is designed, not built.' in access['today']
    solved = tree.aims['solved']
    assert 'Did the right model answer' in solved['text'] and 'can see what they process' in solved['today']
    assert 'told real answers from imitations 90–95% of the time in early tests on one model' in solved['text']
    # Today's platform support is stated once (in How it works), not in the stage table.
    simple = tree.panels['simple']
    assert simple.count('Apple') == 1 and 'about 95% of the time' not in simple
    assert "AI will shape how the rest of us live and work, and whoever owns it will decide how. We'd rather that be all of us." in simple
    stages = tree.rows['stages']
    assert [r[0] for r in stages] == ['1. Trusted swarms', '2. Connected communities', '3. A shared commons',
                                      '4. Proof instead of trust']
    assert [r[2].strip() for r in stages] == ['Working now as an early preview',
                                              'Designed; first model-checking tools in testing', 'Designed', 'Research']
    assert [r[2] for r in tree.rows['layers']] == ['Built: early preview', 'Designed', 'Planned']
    technical = tree.panels['technical']
    for limit in ('Not private from your swarm.', 'Not verified.', 'One coordinator per swarm.',
                  'Not automatically cheaper or greener.'):
        assert limit in technical
    assert 'so this is research' in technical
    # Technical v4: today's coordinator stated honestly after the how-it-works steps.
    assert tree.aims['t-how']['today'] == ('Today: the device that starts a swarm admits its members and is the entry '
                                           'point for every request, so it sees every prompt. The other devices see '
                                           'the activations they process.')


def test_header_and_homepage_link_to_about():
    for page in (HOME, VISION):
        html = page.read_text()
        nav = re.search(r'<nav class="row" aria-label="Project">(.*?)</nav>', html).group(1)
        hrefs = re.findall(r'href="([^"]+)"', nav)
        assert hrefs == ['/about', 'https://github.com/Tranquil-Flow/mycelium', '/swarms/'], (page.name, hrefs)
    assert 'href="/about" aria-current="page">About</a>' in VISION.read_text()
    assert '>Vision</a>' not in VISION.read_text() + HOME.read_text() and 'href="/vision"' not in HOME.read_text()
    closing = HOME.read_text().split('<h2>This is the first step</h2>', 1)[1].split('</section>', 1)[0]
    assert '<a class="vision-link" href="/about">About Mycelium →</a>' in closing
    vision_links = [a for a in parse(HOME).links if a.get('href') == '/about']
    assert len(vision_links) == 2


def test_vision_page_requests_only_same_origin_resources():
    tree = parse(VISION)
    assert tree.resources and all(r.startswith('/') and not r.startswith('//') for r in tree.resources)
    assert set(tree.resources) <= {p for p in server.ASSETS}
    for link in tree.links:
        href = link['href']
        assert href.startswith(('/', '#', 'https://github.com/Tranquil-Flow/mycelium')), href
        if href.startswith('https://'):
            assert 'noopener' in link.get('rel', '') and 'target' not in link


def test_vision_assets_are_named_in_the_server_and_proxy_allowlists():
    for path in ('/about', '/swarms/about.css', '/swarms/about.js'):
        assert path in server.ASSETS and path in proxy._DIRECTORY_ASSET_PATHS
    assert '/vision' in server.REDIRECTS and '/vision' not in server.ASSETS
    for path in ('/swarms/vision.css', '/swarms/vision.js'):
        assert path not in server.ASSETS and proxy._directory_route_decision('GET', path)[0] is False
    for path in ('/about', '/swarms/about.css', '/swarms/about.js'):
        assert proxy._directory_route_decision('GET', path) == (True, 200, 'ok')
        assert proxy._directory_route_decision('POST', path)[0] is False
    for path in ('/about/', '/about?x=1', '/about.html', '/swarms/about.html', '/vision/', '/vision?x=1'):
        assert proxy._directory_route_decision('GET', path)[0] is False


@pytest.mark.parametrize('origin,flag,staging', [
    ('https://next.mycelium.now', None, False),
    ('https://mycelium.now', '1', False),
    ('https://next.mycelium.now', '1', True),
])
def test_staging_banner_on_vision_only_with_flag_and_staging_origin(monkeypatch, origin, flag, staging):
    if flag is None:
        monkeypatch.delenv('MYCELIUM_STAGING_SAMPLES', raising=False)
    else:
        monkeypatch.setenv('MYCELIUM_STAGING_SAMPLES', flag)
    srv = DirectoryServer(Directory(MemoryStore(), verify), public_origin=origin).start()
    try:
        status, _, html = fetch(srv.origin + '/about')
        assert status == 200
        for marker in (b'staging-banner', b'data-bg-choice', b'style-switch'):
            assert (marker in html) is staging, marker
        assert b'data-intro-choice' not in html  # the intro comparison is home-page only
        if not staging:
            assert html == VISION.read_bytes()
        else:
            assert html.index(b'staging-banner') < html.index(b'<header class="top">')
    finally:
        srv.close()


# ---- browser ----

def new_page(browser, srv, *, width=1440, height=900, theme='dark', glow='green', **options):
    context = browser.new_context(viewport={'width': width, 'height': height}, color_scheme=theme, **options)
    context.add_init_script(CLS)
    if glow == 'blue':
        context.add_init_script("try{localStorage.setItem('%s','blue')}catch{}" % GLOW_KEY)
    page = context.new_page()
    outside = []
    page.on('request', lambda r: r.url.startswith(srv.origin) or outside.append(r.url))
    page.outside = outside
    return page


def state(page):
    return page.evaluate("""(()=>({mode:document.documentElement.dataset.visionMode, hash:location.hash,
      stored:localStorage.getItem('vision-mode'),
      selected:[...document.querySelectorAll('[role=tab]')].map(t=>t.getAttribute('aria-selected')),
      tabindex:[...document.querySelectorAll('[role=tab]')].map(t=>t.tabIndex),
      shown:[...document.querySelectorAll('[data-panel]')].filter(p=>p.offsetParent).map(p=>p.dataset.panel)}))()""")


def test_old_vision_links_land_on_about_keeping_the_fragment(directory):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory)
        page.goto(directory.origin + '/vision#technical')
        assert page.url == directory.origin + '/about#technical'
        assert state(page)['mode'] == 'technical' and page.title() == 'About · Mycelium'
        page.goto(directory.origin + '/vision')
        assert page.url == directory.origin + '/about'
        assert page.outside == [], page.outside
        browser.close()


def test_switch_defaults_to_simple_persists_and_deep_links(directory):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory)
        page.goto(directory.origin + '/about')
        s = state(page)
        assert s == {'mode': 'simple', 'hash': '', 'stored': None, 'selected': ['true', 'false'],
                     'tabindex': [0, -1], 'shown': ['simple']}
        assert page.get_by_role('tablist', name='Level of detail').is_visible()
        expect(page.get_by_role('tabpanel')).to_have_count(1)
        page.get_by_role('tab', name='Technical').click()
        s = state(page)
        assert (s['mode'], s['hash'], s['stored'], s['selected'], s['shown']) == \
            ('technical', '#technical', 'technical', ['false', 'true'], ['technical'])
        page.reload()  # remembered, and reflected in the address
        assert state(page)['mode'] == 'technical' and page.url.endswith('/about#technical')
        page.goto(directory.origin + '/about')
        assert state(page)['mode'] == 'technical' and page.url.endswith('#technical')
        # Keyboard: arrows move focus and selection; Home/End; focus is visible.
        tab = page.get_by_role('tab', name='Technical')
        tab.focus()
        page.keyboard.press('ArrowLeft')
        s = state(page)
        assert s['mode'] == 'simple' and s['hash'] == '' and s['stored'] == 'simple' and s['tabindex'] == [0, -1]
        expect(page.get_by_role('tab', name='Simple')).to_be_focused()
        assert page.evaluate("getComputedStyle(document.activeElement).outlineStyle") == 'solid'
        page.keyboard.press('End')
        assert state(page)['mode'] == 'technical'
        page.keyboard.press('Home')
        assert state(page)['mode'] == 'simple'
        page.keyboard.press('ArrowRight')
        assert state(page)['mode'] == 'technical'
        page.context.close()

        # A shared #technical link wins over a stored Simple choice and is not stored.
        page = new_page(browser, directory)
        page.goto(directory.origin + '/about')
        page.get_by_role('tab', name='Simple').click()
        page.goto(directory.origin + '/about#technical')
        s = state(page)
        assert s['mode'] == 'technical' and s['stored'] == 'simple' and page.evaluate('scrollY') == 0
        # Back to Simple via an edited hash; then the in-page link to the technical version.
        page.evaluate("location.hash='simple'")
        page.wait_for_timeout(100)
        assert state(page)['mode'] == 'simple'
        link = page.get_by_role('link', name='Read the technical version →')
        link.click()
        s = state(page)
        assert s['mode'] == 'technical' and s['hash'] == '#technical' and s['stored'] == 'technical'
        expect(page.get_by_role('tab', name='Technical')).to_be_focused()
        assert page.evaluate("document.getElementById('mode-switch').getBoundingClientRect().top") >= \
            page.evaluate("document.querySelector('header.top').getBoundingClientRect().bottom")
        assert page.outside == []
        page.context.close()
        browser.close()


def test_without_javascript_both_versions_show_with_headings(directory):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory, java_script_enabled=False)
        page.goto(directory.origin + '/about')
        for name in ('Simple', 'Technical'):
            expect(page.get_by_role('heading', name=name, exact=True)).to_be_visible()
        expect(page.get_by_text('The short version')).to_be_visible()
        expect(page.get_by_text('Honest limits today')).to_be_visible()
        assert not page.locator('#mode-switch').is_visible()
        browser.close()


@pytest.mark.parametrize('width,height', [(390, 844), (768, 1000), (1440, 900)])
def test_zero_layout_shift_no_third_parties_and_header_fit(directory, width, height):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in ('dark', 'light'):
            page = new_page(browser, directory, width=width, height=height, theme=theme)
            for path in ('/about', '/about#technical', '/'):
                page.goto(directory.origin + path)
                page.wait_for_timeout(1200)
                assert sum(page.evaluate('window.__cls')) == 0, (width, theme, path)
                fit = page.evaluate("""(()=>{const h=document.querySelector('.top').getBoundingClientRect();
                  const kids=[...document.querySelectorAll('.top .wrap > *, .top nav > *, .top .nav-links > a')]
                    .filter(e=>getComputedStyle(e).display!=='contents');
                  // The phone brand row spans the width by design; judge the brand by its glyphs.
                  const range=document.createRange();range.selectNodeContents(document.querySelector('.top .brand'));
                  const boxes=kids.map(e=>e.matches('.brand')?range.getBoundingClientRect():e.getBoundingClientRect());
                  const overlap=boxes.some((a,i)=>boxes.some((b,j)=>j>i&&!kids[i].contains(kids[j])&&!kids[j].contains(kids[i])
                    &&a.width&&b.width&&a.left<b.right-0.5&&b.left<a.right-0.5&&a.top<b.bottom-0.5&&b.top<a.bottom-0.5));
                  return {inside:boxes.every(r=>r.bottom<=h.bottom+0.5&&r.right<=innerWidth+0.5&&r.left>=-0.5),
                          overlap, wide:document.documentElement.scrollWidth<=innerWidth}})()""")
                assert fit == {'inside': True, 'overlap': False, 'wide': True}, (width, theme, path, fit)
                vision = page.locator('header').get_by_role('link', name='About', exact=True)
                expect(vision).to_be_visible()
                expect(vision).to_have_attribute('href', '/about')
                vision.focus()
                expect(vision).to_be_focused()
                if width <= 640:  # phone touch target (desktop matches the existing text links)
                    assert vision.evaluate("e=>e.getBoundingClientRect().height>=44")
            home_link = page.get_by_role('link', name='About Mycelium →')
            home_link.scroll_into_view_if_needed()
            expect(home_link).to_be_visible()
            home_link.click()
            page.wait_for_url(directory.origin + '/about*')
            assert state(page)['mode'] in ('simple', 'technical')
            assert page.outside == [], page.outside
            page.context.close()
        browser.close()


@pytest.mark.parametrize('width', [390, 768, 1440])
def test_simple_v3_reads_comfortably(directory, width):
    """Simple v3: comfortable measure, Today lines in the Today treatment, the
    closing line as a pull-quote, the same two buttons, nothing overflowing."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory, width=width, height=900)
        page.goto(directory.origin + '/about')
        page.wait_for_timeout(600)
        info = page.evaluate("""(()=>{const panel=document.querySelector('[data-panel=simple]');
          const paras=[...panel.querySelectorAll('.sect > p:not(.vision-close)')];
          const close=panel.querySelector('.vision-close'), cs=getComputedStyle(close);
          const today=[...panel.querySelectorAll('.sect > p.goal-today')];
          const chars=(p)=>{const c=getComputedStyle(p),probe=document.createElement('span');
            probe.style.cssText='display:inline-block;width:10ch;position:absolute;visibility:hidden';p.append(probe);
            const ch=probe.getBoundingClientRect().width/10;probe.remove();
            return (p.getBoundingClientRect().width-parseFloat(c.paddingLeft)-parseFloat(c.paddingRight))/ch};
          return {maxChars:Math.max(...paras.map(chars)),
            overflow:document.documentElement.scrollWidth>innerWidth,
            today:today.map(p=>[getComputedStyle(p).backgroundColor, p.firstElementChild.tagName, p.textContent.slice(0,6)]),
            close:[cs.borderLeftStyle, parseFloat(cs.borderLeftWidth), parseFloat(cs.fontSize) > parseFloat(getComputedStyle(paras[0]).fontSize)],
            sections:[...panel.querySelectorAll('.sect > h2')].map(h=>h.textContent),
            buttons:[...panel.querySelectorAll('.vision-cta a')].map(a=>[a.textContent,a.getAttribute('href')])}})()""")
        assert not info['overflow'], info
        assert info['maxChars'] <= 68.5, info  # 68ch text measure (~80 characters), Today lines included
        assert len(info['today']) == 4 and all(t[1] == 'STRONG' and t[2].startswith('Today') and t[0] != 'rgba(0, 0, 0, 0)' for t in info['today']), info
        assert info['close'] == ['solid', 3.0, True], info
        assert info['sections'] == ['The short version', 'How it works', "Who's in charge", 'Access is not charity',
                                    'Built for the people usually left out', 'Where we are', "What we haven't solved",
                                    'Part of something bigger']
        assert info['buttons'] == [['Download Mycelium', '/swarms/#contributing'], ['Read the technical version →', '#panel-technical']]
        assert sum(page.evaluate('window.__cls')) == 0
        browser.close()


def test_stage_and_layer_tables_stack_into_cards_on_phones(directory):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for width, stacked in ((390, True), (1440, False)):
            page = new_page(browser, directory, width=width, height=900)
            page.goto(directory.origin + '/about#technical')
            for name in ('layers',):
                info = page.evaluate("""(n)=>{const t=document.querySelector(`[data-table=${n}]`);
                  const rows=[...t.querySelectorAll('tbody tr')];
                  return {display:getComputedStyle(rows[0]).display, wide:t.getBoundingClientRect().right<=innerWidth,
                          label:getComputedStyle(rows[0].cells[1],'::before').content}}""", name)
                assert (info['display'] == 'block') is stacked and info['wide'], (width, info)
                assert (info['label'] == '"Role"') is stacked
            page.goto(directory.origin + '/about#simple')
            info = page.evaluate("""(()=>{const rows=[...document.querySelectorAll('[data-table=stages] tbody tr')];
              return rows.map(r=>[getComputedStyle(r).display, getComputedStyle(r.cells[2].firstElementChild).backgroundColor])})()""")
            assert len(info) == 4 and all((d == 'block') is stacked for d, _ in info)
            page.context.close()
        browser.close()


def _ratio(fg, bg):
    def lum(c):
        c = np.asarray(c, dtype=float) / 255
        c = np.where(c <= 0.03928, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
        return c[..., 0] * 0.2126 + c[..., 1] * 0.7152 + c[..., 2] * 0.0722
    a, b = lum(fg), lum(bg)
    return (np.maximum(a, b) + 0.05) / (np.minimum(a, b) + 0.05)


TEXT_RUNS = """(()=>{const out=[];const top=document.querySelector('header.top').getBoundingClientRect().bottom;
  const banner=document.querySelector('.staging-banner');
  for(const root of document.querySelectorAll('main, footer')){
    const w=document.createTreeWalker(root,NodeFilter.SHOW_TEXT);
    while(w.nextNode()){const n=w.currentNode;if(!n.data.trim())continue;const el=n.parentElement;
      if(!el.offsetParent&&getComputedStyle(el).position!=='fixed')continue;
      const m=getComputedStyle(el).color.match(/[\\d.]+/g).map(Number);
      const r=document.createRange();r.selectNodeContents(n);
      const rects=[...r.getClientRects()].filter(q=>q.width>1&&q.top>=top&&q.bottom<=innerHeight&&q.top<innerHeight)
        .map(q=>[q.left,q.top,q.width,q.height]);
      if(rects.length)out.push({c:m.slice(0,3),rects,t:n.data.trim().slice(0,40)});}}
  return out})()"""
HIDE_TEXT = "*,*::before,*::after{color:transparent!important;-webkit-text-fill-color:transparent!important}"


@pytest.mark.parametrize('theme', ['dark', 'light'])
@pytest.mark.parametrize('glow', ['green', 'blue'])
@pytest.mark.parametrize('width,height', [(390, 844), (768, 1000), (1440, 900)])
def test_text_contrast_on_the_actual_backdrop(directory, theme, glow, width, height):
    """Backdrop pixels under each glyph (1 px dilated), field drawn, motion frozen."""
    worst = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory, width=width, height=height, theme=theme, glow=glow,
                        reduced_motion='reduce', bypass_csp=True)  # bypass only to inject the text-hiding probe
        for mode in ('simple', 'technical'):
            page.goto(directory.origin + '/about#' + mode)
            page.wait_for_timeout(1500)
            total = page.evaluate('document.documentElement.scrollHeight')
            y = 0
            while True:
                page.evaluate('(y)=>scrollTo(0,y)', y)
                page.wait_for_timeout(250)
                runs = page.evaluate(TEXT_RUNS)
                shown = np.asarray(Image.open(io.BytesIO(page.screenshot())).convert('RGB')).astype(int)
                handle = page.add_style_tag(content=HIDE_TEXT)
                page.wait_for_timeout(120)
                backdrop = np.asarray(Image.open(io.BytesIO(page.screenshot())).convert('RGB')).astype(int)
                handle.evaluate('e=>e.remove()')
                glyph = np.abs(shown - backdrop).sum(axis=2) > 24
                dil = glyph.copy()
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    dil |= np.roll(glyph, (dy, dx), axis=(0, 1))
                for run in runs:
                    for x, ty, w, h in run['rects']:
                        x0, y0 = max(0, int(x)), max(0, int(ty))
                        x1, y1 = min(width, int(x + w + 1)), min(height, int(ty + h + 1))
                        m = dil[y0:y1, x0:x1]
                        if not m.any():
                            continue
                        r = _ratio(np.asarray(run['c']), backdrop[y0:y1, x0:x1][m]).min()
                        worst.append((float(r), mode, y, run['t']))
                if y + height >= total:
                    break
                y += height - 140
        page.context.close()
        browser.close()
    assert worst
    worst.sort()
    print("worst contrast", theme, glow, width, len(worst), worst[:2])
    assert worst[0][0] >= 4.5, worst[:5]


def test_reduced_motion_runs_no_css_animation_on_the_vision_page(directory):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = new_page(browser, directory, reduced_motion='reduce')
        page.goto(directory.origin + '/about')
        page.wait_for_timeout(1200)
        assert page.evaluate("document.getAnimations().filter(a=>a.playState==='running').length") == 0
        page.get_by_role('tab', name='Technical').click()
        assert page.evaluate("document.getAnimations().filter(a=>a.playState==='running').length") == 0
        browser.close()
