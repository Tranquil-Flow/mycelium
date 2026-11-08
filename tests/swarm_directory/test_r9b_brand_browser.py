"""The project brand always returns home; Browse remains its own navigation."""
from pathlib import Path
from html.parser import HTMLParser
from playwright.sync_api import sync_playwright, expect
from test_site_v2_browser import local

class Brands(HTMLParser):
    def __init__(self):
        super().__init__(); self.links=[]
    def handle_starttag(self, tag, attrs):
        attrs=dict(attrs)
        if tag=='a' and 'brand' in (attrs.get('class') or '').split():
            self.links.append(attrs.get('href'))

def test_brand_html_targets_home():
    web=Path(__file__).resolve().parents[2]/'mycelium_swarm_directory/web'
    for name in ('apex.html','index.html'):
        p=Brands();p.feed((web/name).read_text())
        assert p.links==['/'], (name,p.links)

def test_brand_returns_to_apex_on_every_swarm_route(local):
    with sync_playwright() as p:
        b=p.chromium.launch();page=b.new_page(reduced_motion='reduce')
        origin=local['directory'].origin
        routes=['/','/swarms/#browse','/swarms/#chat','/swarms/#contributing',
                '/swarms/#owned','/swarms/#new','/swarms/#notice',
                '/swarms/#swarm/missing','/swarms/#chat/missing','/swarms/#unknown']
        for route in routes:
            page.goto(origin+route)
            if route!='/':page.wait_for_selector('body[data-ready="1"]')
            expect(page.locator('a.brand')).to_have_attribute('href','/')
            page.locator('a.brand').click()
            expect(page).to_have_url(origin+'/')
        page.goto(origin+'/swarms/#notice')
        expect(page.locator('a[href="#browse"]').first).to_be_attached()
        b.close()
