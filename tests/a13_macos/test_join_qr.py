# SPDX-License-Identifier: AGPL-3.0-only
"""QR join option: QR payload is the HTTPS join page URL; the page closes with its window."""
import hashlib
from pathlib import Path

import pytest

from mycelium_a13_macos.pairing import PairingWindows

ORIGIN = 'https://quiet-owl-test.trycloudflare.com'
SEED = 'sha256:' + 'a' * 64


class Clock:
    def __init__(self): self.t = 1_000_000.0
    def __call__(self): return self.t


@pytest.fixture
def windows():
    clock = Clock()
    registry = PairingWindows(clock=clock)
    opened = registry.open(origin=ORIGIN, seed_key_digest=SEED, ttl_seconds=300)
    return registry, opened, clock


def page_path(opened):
    window_id, a, b = opened['short_code'].split('-')
    return '/pairing-window/v1/%s/join/%s%s' % (window_id, a, b)


def test_vendored_qr_encoder_is_pinned():
    raw = (Path(__file__).resolve().parents[2] / 'mycelium_a13_macos/vendor/qrcodegen.py').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'b089855caf16185c61421ea4927c1b213cf9468940d71fa8ab11ef83662dcc84'
    assert b'MIT License' in raw[:400]


def test_qr_payload_is_exactly_the_join_page_url(windows, monkeypatch):
    from mycelium_a13_macos import join_page
    registry, opened, _ = windows
    seen = []
    real = join_page.QrCode.encode_text
    monkeypatch.setattr(join_page.QrCode, 'encode_text', staticmethod(lambda text, ecl: seen.append(text) or real(text, ecl)))
    svg = join_page.owner_qr_svg(registry, opened['window_id'])
    assert seen == [ORIGIN + page_path(opened)]
    assert svg.startswith(b'<svg') and b'<path' in svg
    assert join_page.join_page_url(registry, opened['window_id']) == ORIGIN + page_path(opened)


def test_page_shows_code_and_pinned_link_only_for_its_open_window(windows):
    from mycelium_a13_macos import join_page
    registry, opened, _ = windows
    other = registry.open(origin=ORIGIN, seed_key_digest=SEED, ttl_seconds=300)
    status, raw, kind = join_page.render(registry, page_path(opened))
    text = raw.decode()
    assert status == 200 and kind == 'text/html; charset=utf-8'
    assert opened['short_code'] in text and opened['join_link'] in text
    assert 'quiet-owl-test' in text and 'Ask the host for the app' in text
    assert other['short_code'] not in text and other['join_link'] not in text
    assert '<script' not in text


@pytest.mark.parametrize('close', ['revoke', 'expire'])
def test_page_closes_with_the_window(windows, close):
    from mycelium_a13_macos import join_page
    registry, opened, clock = windows
    if close == 'revoke': registry.revoke(opened['window_id'])
    else: clock.t += 301
    status, raw, _ = join_page.render(registry, page_path(opened))
    assert status == 410 and opened['short_code'] not in raw.decode()
    assert join_page.owner_qr_svg(registry, opened['window_id']) is None


def test_wrong_code_counts_toward_the_window_lockout(windows):
    from mycelium_a13_macos import join_page
    registry, opened, _ = windows
    window_id = opened['window_id']
    for _ in range(4):
        assert join_page.render(registry, '/pairing-window/v1/%s/join/22222222' % window_id)[0] == 404
    assert join_page.render(registry, '/pairing-window/v1/%s/join/22222222' % window_id)[0] == 410
    assert join_page.render(registry, page_path(opened))[0] == 410  # locked even with the right code


def test_malformed_paths_are_not_found(windows):
    from mycelium_a13_macos import join_page
    registry, _, _ = windows
    for path in ('/pairing-window/v1/ABCD/join', '/pairing-window/v1/abcd/join/22222222',
                 '/pairing-window/v1/ABCD/join/22222222/x'):
        assert join_page.render(registry, path)[0] == 404


def test_owner_public_route_serves_the_page_as_html(tmp_path, windows):
    from mycelium_a13_macos.app import DemoApp
    registry, opened, _ = windows
    app = DemoApp(tmp_path / 'o', check_platform=False)
    app.pairing_windows = registry
    status, raw, kind = app._public_route('GET', page_path(opened), b'')
    assert status == 200 and kind.startswith('text/html') and opened['short_code'].encode() in raw
