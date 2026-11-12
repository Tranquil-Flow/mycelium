"""Fraunces headlines (owner pick, 12 Nov 2026).

The site had no webfonts at all: the CSP is default-src 'none' with no
font-src, so a font had to be self-hosted (same-origin) and font-src added.
This pins the whole chain - file, licence, route, CSP, @font-face, the
headline rule, the preload in every shell - and then proves a real browser
on a real local server loads it and computes h1 to Fraunces.
"""
from pathlib import Path
import re

import pytest
from playwright.sync_api import sync_playwright, expect

from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory' / 'web'
SERVER = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory' / 'server.py'
FONT = 'fraunces-latin-wght-normal.woff2'
SHELLS = ('apex.html', 'index.html', 'about.html')


def test_the_font_file_ships_with_its_licence_and_a_same_origin_route():
    data = (WEB / 'fonts' / FONT).read_bytes()
    assert data[:4] == b'wOF2', 'not a woff2 file'
    assert len(data) < 60_000, len(data)
    assert 'SIL Open Font License' in (WEB / 'fonts' / 'OFL-Fraunces.txt').read_text()
    server = SERVER.read_text()
    assert f"'/swarms/fonts/{FONT}': ('fonts/{FONT}', 'font/woff2')" in server


def test_the_csp_now_allows_only_same_origin_fonts():
    # default-src 'none' would block every webfont, so font-src has to be
    # explicit; 'self' keeps the door closed to every third-party foundry.
    assert "font-src 'self'" in SERVER.read_text()


def test_the_headline_rule_and_the_preload_are_in_place():
    css = (WEB / 'design.css').read_text()
    face = re.search(r'@font-face\s*\{[^}]*\}', css).group(0)
    assert 'font-family: "Fraunces"' in face
    assert f'url("/swarms/fonts/{FONT}")' in face, 'must be same-origin, not a CDN'
    assert 'font-display: optional' in face, 'swap would risk the CLS gate'
    assert 'font-weight: 100 900' in face, 'the 550 headline weight must be covered'
    rule = re.search(r'h1,\s*h2,\s*h3\s*\{[^}]*\}', css).group(0)
    assert 'font-family: "Fraunces", Georgia, serif' in rule
    for shell in SHELLS:
        head = (WEB / shell).read_text()
        assert f'<link rel="preload" href="/swarms/fonts/{FONT}" as="font" type="font/woff2" crossorigin>' in head, shell


@pytest.mark.parametrize('path', ['/', '/about', '/swarms/'])
def test_a_real_browser_loads_it_and_computes_h1_to_fraunces(local, path):  # noqa: F811
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={'width': 1440, 'height': 900}, color_scheme='dark')
        context.add_init_script(CLS)
        page = context.new_page()
        font_hits = []
        page.on('response', lambda r: font_hits.append((r.url, r.status)) if FONT in r.url else None)
        response = page.goto(local['directory'].origin + path, wait_until='networkidle')
        assert "font-src 'self'" in response.headers['content-security-policy']
        page.wait_for_function("document.fonts.check('550 40px Fraunces')", timeout=8000)
        computed = page.evaluate("getComputedStyle(document.querySelector('h1')).fontFamily")
        assert computed.split(',')[0].strip(' "') == 'Fraunces', computed
        assert [(u, s) for u, s in font_hits if s == 200], font_hits
        assert not [u for u, _ in font_hits if not u.startswith(local['directory'].origin)], 'third-party font'
        browser.close()
