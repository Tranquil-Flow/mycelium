# SPDX-License-Identifier: AGPL-3.0-only
"""Owner light mode (11 Nov): sage ground, ink copy, white ✳ Mycelium.

The choice lives on <html data-theme>, set by the blocking head script
theme.js from localStorage (else the OS), so the stylesheets key off
:root[data-theme=...] instead of a media query - a manual choice can never
disagree with the CSS and there is no palette flash. Light copy stays ink
(#22261c on #a6b18b = 6.82:1) so the hero keeps clearing the repo's 4.5:1
gate in test_r11_2c; only the header wordmark is cream (#f7f2e7, 2.03:1 on
the sage ground), which is outside main and therefore outside that gate.
"""
from pathlib import Path
import re

import pytest
from playwright.sync_api import sync_playwright, expect

from test_site_v2_browser import local, CLS  # noqa: F401  (fixture)

WEB = Path(__file__).resolve().parents[2] / 'mycelium_swarm_directory/web'
SHELLS = ('apex.html', 'about.html', 'index.html')
# The owner-approved light tokens: sage ground, ink copy, deep-green accent.
LIGHT = {
    '--bg': '#a6b18b', '--bg-2': '#a6b18b', '--card': '#a6b18b',
    '--ink': '#22261c', '--muted': '#343a2a',
    '--accent': '#0f4218', '--accent-ink': '#f7f2e7',
    # the drawn mycelium is white in light mode (owner pick, panel B)
    '--strand': '#f7f2e7', '--glow': '#f7f2e7', '--core': '#ffffff',
}


def light_block(css):
    """The declarations inside the :root[data-theme="light"] rule."""
    sel = css.index(':root[data-theme="light"]')
    body = css[css.index('{', sel) + 1:css.index('}', sel)]
    # A comment inside the block would otherwise swallow the declaration that
    # follows it, since comments are not semicolon-terminated.
    body = re.sub(r'/\*.*?\*/', '', body, flags=re.S)
    return {
        key.strip(): value.strip()
        for key, value in (line.split(':', 1) for line in body.split(';') if ':' in line)
    }


def test_every_shell_offers_the_theme_switch():
    for name in SHELLS:
        html = (WEB / name).read_text()
        header = html[html.index('<header class="top">'):html.index('</header>')]
        assert 'aria-label="Colour theme"' in header, name
        assert 'data-theme-choice="dark"' in header, name
        assert 'data-theme-choice="light"' in header, name
        # Blocking head script, so the palette is right on the first paint.
        assert '<script src="/swarms/theme.js"></script>' in html.split('</head>')[0], name


def test_light_palette_is_the_owner_choice_and_not_a_media_query():
    design = (WEB / 'design.css').read_text()
    block = light_block(design)
    for token, value in LIGHT.items():
        assert token in block, token
        assert block[token].strip() == value, (token, block[token])
    # The swarms app shares the ground so /swarms/ matches the landing page.
    app = light_block((WEB / 'site.css').read_text())
    assert app['--bg'].strip() == '#a6b18b'
    assert app['--ink'].strip() == '#22261c'
    # theme.js is the single source of truth: no stylesheet may follow the OS
    # behind a manual choice's back.
    for css in WEB.glob('*.css'):
        assert 'prefers-color-scheme' not in css.read_text(), css.name


def test_theme_js_reads_the_choice_and_writes_nothing_on_load():
    theme = (WEB / 'theme.js').read_text()
    assert 'mycelium-site-theme-v1' in theme
    assert 'dataset.theme = theme' in theme
    # Reads only: an explicit click in design.js is what persists a choice.
    assert 'setItem' not in theme


def test_theme_choice_applies_persists_and_keeps_the_hero_readable(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(
            viewport={'width': 1440, 'height': 900}, color_scheme='dark')
        context.add_init_script(CLS)
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        origin = local['directory'].origin

        page.goto(origin + '/')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.evaluate('localStorage.length') == 0  # nothing stored yet
        page.get_by_role('button', name='Light', exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        expect(page.locator('body')).to_have_attribute('data-theme', 'light')
        assert page.evaluate('localStorage.getItem("mycelium-site-theme-v1")') == 'light'
        # Owner palette actually applied: sage ground, ink copy, cream wordmark.
        bg = page.locator('body').evaluate('e => getComputedStyle(e).backgroundColor')
        assert bg == 'rgb(166, 177, 139)', bg
        h1 = page.locator('h1').first.evaluate('e => getComputedStyle(e).color')
        assert h1 == 'rgb(34, 38, 28)', h1
        brand = page.locator('.brand').first.evaluate('e => getComputedStyle(e).color')
        assert brand == 'rgb(247, 242, 231)', brand
        expect(page.locator('[data-theme-choice="light"]')).to_have_attribute('aria-pressed', 'true')
        assert sum(page.evaluate('window.__cls')) == 0

        # The choice survives a reload and travels to another shell.
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        page.goto(origin + '/about')
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        assert page.locator('h1').first.evaluate('e => getComputedStyle(e).color') == 'rgb(34, 38, 28)'

        page.get_by_role('button', name='Dark', exact=True).click()
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.evaluate('localStorage.getItem("mycelium-site-theme-v1")') == 'dark'
        assert not errors, errors
        browser.close()


def test_light_mode_is_hueless_with_white_pulses_and_no_glow_switch():
    """Owner, 12 Nov (second pass): light mode drops the Green/Blue switch
    entirely and the pulses run cream/bright white instead of the dark
    palette's green or blue. Dark keeps both: --pulse-* equals --glow/--core
    there, so dark rendering is unchanged by construction."""
    css = (WEB / 'design.css').read_text()
    light = light_block(css)
    assert light['--glow'] == '#f7f2e7' and light['--core'] == '#ffffff', 'art stays white'
    assert light['--pulse-glow'] == '#ffffff' and light['--pulse-core'] == '#ffffff'
    # A stored hue must not leak into a hueless light mode.
    assert not re.search(r':root\[data-theme="light"\]\[data-glow=', css)
    hide_match = re.search(
        r':root\[data-theme="light"\] \.glow-switch\[aria-label="Mycelium glow"\]\s*\{[^}]*\}', css)
    assert hide_match, 'light mode must hide only the Green/Blue switch'
    assert 'display: none' in hide_match.group(0), hide_match.group(0)
    # The theme and staging style groups share .glow-switch and must survive.
    assert 'theme-switch' not in hide_match.group(0)
    assert 'style-switch' not in hide_match.group(0)
    # Dark keeps the hue and keeps pulse == glow.
    dark = re.search(r'^:root\s*\{[^}]*\}', css, re.M).group(0)
    assert '--pulse-glow: #35df47' in dark and '--glow: #35df47' in dark
    blue_dark = re.search(r':root\[data-glow="blue"\]\s*\{[^}]*\}', css).group(0)
    assert '--pulse-glow: #06a8f0' in blue_dark and '--glow: #06a8f0' in blue_dark
    # The renderers actually read the pulse tokens at the motion draw sites.
    hero = (WEB / 'mycelium-hero.js').read_text()
    field = (WEB / 'mycelium-field.js').read_text()
    assert 'palette.pulse.glow' in hero and 'palette.pulse.core' in hero
    assert 'pulseGlow' in field and 'pulseCore' in field


def test_the_glow_switch_is_gone_in_light_and_pulses_stay_white(local):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for stored_hue in ('green', 'blue'):
            context = browser.new_context(
                viewport={'width': 1440, 'height': 900}, color_scheme='dark')
            context.add_init_script("localStorage.setItem('mycelium-site-theme-v1','light')")
            context.add_init_script(f"localStorage.setItem('mycelium-site-glow-v1','{stored_hue}')")
            page = context.new_page()
            page.goto(local['directory'].origin + '/', wait_until='networkidle')
            page.evaluate('document.fonts.ready')
            pulse = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--pulse-glow').trim()")
            art = page.evaluate(
                "getComputedStyle(document.documentElement).getPropertyValue('--glow').trim()")
            assert pulse == '#ffffff', (stored_hue, 'light pulses are white', pulse)
            assert art == '#f7f2e7', (stored_hue, 'art must stay white', art)
            expect(page.get_by_label('Mycelium glow')).not_to_be_visible()
            expect(page.locator('.theme-switch')).to_be_visible()
            context.close()
        # Dark keeps the switch, with the stored hue still selected.
        context = browser.new_context(
            viewport={'width': 1440, 'height': 900}, color_scheme='dark')
        context.add_init_script("localStorage.setItem('mycelium-site-theme-v1','dark')")
        context.add_init_script("localStorage.setItem('mycelium-site-glow-v1','blue')")
        page = context.new_page()
        page.goto(local['directory'].origin + '/', wait_until='networkidle')
        expect(page.get_by_label('Mycelium glow')).to_be_visible()
        expect(page.locator('[data-glow-choice=blue]')).to_have_attribute('aria-pressed', 'true')
        context.close()
        browser.close()


def test_the_light_background_mycelium_is_present_not_ghostly():
    # Owner: "hard to see the mycelium in background on the light mode".
    # Both multipliers are light-only; the dark values stay 0.62 / 1.
    field = (WEB / 'mycelium-field.js').read_text()
    assert 'fieldAlpha = light ? 0.42 : 0.62' in field
    assert 'lightMul = light ? 0.85 : 1' in field
    hero = (WEB / 'mycelium-hero.js').read_text()
    assert 'colonyFill(ctx, c, hexRgb(palette.glow), (light ? 0.75 : 1) * grow)' in hero
