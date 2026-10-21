# SPDX-License-Identifier: AGPL-3.0-only
"""QR option for the join flow: an HTTPS join page on the owner's relay origin.

The owner's "Invite a Mac" card shows a QR code whose payload is exactly
``<relay origin>/pairing-window/v1/<window id>/join/<code>``.  Opened on a phone
or a Mac, that page shows: how to get the app (ask the host), the short code to
type in the Mycelium app, and an "Open in Mycelium" link carrying the full
``mycelium-join:`` link (which pins the owner's seed key).

What it exposes: only what the URL already holds.  The 4-character window id is
not a secret, so the 8-character code is part of the URL and is checked like a
typed code -- a wrong code counts toward the window's 5-try lockout.  The page
is served only while its window is ``open``: claimed, expired, revoked or
locked windows answer 410 with no code.  The QR is rendered server-side as
SVG by the vendored Project Nayuki encoder (MIT, pinned by sha256); nothing is
fetched from the network and no script runs on the page.
"""
from __future__ import annotations

import html
import re
from typing import Any

from .pairing import PAIRING_WINDOW_PREFIX, PairingError
from .vendor.qrcodegen import QrCode

_PAGE_RE = re.compile(r'^/pairing-window/v1/([23456789ABCDEFGHJKMNPQRSTVWXYZ]{4})/join/'
                      r'([23456789ABCDEFGHJKMNPQRSTVWXYZ]{8})$')
HTML = 'text/html; charset=utf-8'


def is_join_page(path: str) -> bool:
    return isinstance(path, str) and path.startswith(PAIRING_WINDOW_PREFIX) and '/join/' in path


def _open_window(windows: Any, window_id: str):
    window = windows._windows.get(window_id)
    if window is None:
        return None
    windows._refresh(window, windows._now())
    return window if window.state == 'open' else None


def join_page_url(windows: Any, window_id: str) -> str | None:
    with windows._lock:
        window = _open_window(windows, window_id)
        if window is None:
            return None
        return '%s%s%s/join/%s' % (window.origin, PAIRING_WINDOW_PREFIX, window.window_id, window.code)


def qr_svg(text: str, *, border: int = 4) -> bytes:
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    parts = ['M%d,%dh1v1h-1z' % (x + border, y + border)
             for y in range(size) for x in range(size) if qr.get_module(x, y)]
    total = size + 2 * border
    return ('<svg xmlns="http://www.w3.org/2000/svg" version="1.1" viewBox="0 0 %d %d" '
            'stroke="none" role="img" aria-label="Join QR code">'
            '<rect width="100%%" height="100%%" fill="#FFFFFF"/>'
            '<path d="%s" fill="#000000"/></svg>' % (total, total, ' '.join(parts))).encode()


def owner_qr_svg(windows: Any, window_id: str) -> bytes | None:
    """Owner-only (session-authenticated) QR of an OPEN window's join page."""
    url = join_page_url(windows, window_id)
    return None if url is None else qr_svg(url)


def _page(title: str, body: str) -> bytes:
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta name="referrer" content="no-referrer"><title>%s</title></head><body>%s</body></html>'
            % (html.escape(title), body)).encode()


_CLOSED = _page('Invitation closed', '<h1>This invitation is closed</h1>'
                '<p>It expired, was used, or was closed by the host. Ask the host to invite you again.</p>')


def render(windows: Any, path: str) -> tuple[int, bytes, str]:
    match = _PAGE_RE.match(path or '')
    if match is None:
        return 404, _page('Not found', '<h1>Not found</h1>'), HTML
    window_id, code = match.groups()
    with windows._lock:
        try:
            window = windows._live(window_id, code, windows._now())
        except PairingError as error:
            if error.code in {'pairing_window_unknown', 'pairing_window_code_invalid'}:
                return 404, _page('Not found', '<h1>Not found</h1>'), HTML
            return 410, _CLOSED, HTML
        if window.state != 'open':
            return 410, _CLOSED, HTML
        view = windows._owner_view(window, with_secret=True)
    short = html.escape(view['short_code'])
    tunnel = html.escape(view['tunnel_name'])
    link = html.escape(view['join_link'], quote=True)
    body = ('<h1>Join this Mycelium swarm</h1>'
            '<p><strong>1. Get the app.</strong> Ask the host for the app '
            '(Mycelium A13 Demo, usually by AirDrop). It runs on Apple Silicon Macs with macOS 26 or later.</p>'
            '<p><strong>2. Type this code</strong> in the app under “Join code or link”:</p>'
            '<p><big><code>%s %s</code></big></p>'
            '<p><strong>Or, on the Mac that joins:</strong> <a href="%s">Open in Mycelium</a>. '
            'If the link does not open the app, copy it and paste it into the app instead:</p>'
            '<p><code>%s</code></p>'
            '<p>The host still checks your Mac’s fingerprint and approves it. This page stops working '
            'when the invitation is used, expires or is closed.</p>') % (tunnel, short, link, link)
    return 200, _page('Join Mycelium', body), HTML
