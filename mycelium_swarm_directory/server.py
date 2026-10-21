# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback HTTP server for the swarm directory API and its static web page.

Bound to 127.0.0.1 only; meant to sit behind the existing cloudflared tunnel
(e.g. an ingress ``swarms.mycelium.now -> http://127.0.0.1:4360``; owner step,
not done here).  Routes:

* ``GET  /swarms`` (+ ``/swarms/swarms.js|swarms.css``)  the web UI;
* ``GET  /swarms/api/v1/config``                         public origin + labels;
* ``GET  /swarms/api/v1/swarms[/<id>]``                  the phonebook;
* ``GET  /swarms/api/v1/swarms/<id>/link.svg``           QR of the swarm link;
* ``POST /swarms/api/v1/<route>``                        signed documents.

POSTs need ``Content-Type: application/json`` and ``X-Mycelium-Swarms: 1``
(no cookies are ever set, so this is defence in depth, not a session).  Nothing
is logged.  The client rate key is the socket peer, or the
``Cf-Connecting-Ip`` header only when ``trusted_proxy`` (behind cloudflared).
"""
from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from mycelium_platform_neutral import network_mode

from . import docs, hosted_defaults
from .crypto import verify
from .directory import Directory, Limits, PRIVACY_NOTE
from .store import SqliteStore

API = '/swarms/api/v1'
WEB = Path(__file__).resolve().parent / 'web'
ASSETS = {'/swarms': ('index.html', 'text/html; charset=utf-8'),
          '/swarms/': ('index.html', 'text/html; charset=utf-8'),
          '/swarms/swarms.js': ('swarms.js', 'text/javascript; charset=utf-8'),
          '/swarms/swarms.css': ('swarms.css', 'text/css; charset=utf-8')}
DEMO_LABEL = ('Demo. Hosted on one volunteer Mac through a tunnel: it may sleep. No accounts, no email, '
              'no tracking by this site. Keys live only in this browser. Traffic passes through Cloudflare, which can '
              'see your IP address, timing and prompts.')
_IP = re.compile(r'[0-9A-Fa-f:.]{2,45}\Z')
_CHUNK = re.compile(rb'[0-9A-Fa-f]{1,8}\r\n')


def csp(allow_loopback: bool) -> str:
    connect = "'self' https:" + (' http://127.0.0.1:*' if allow_loopback else '')
    return ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src %s; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'" % connect)


def qr_svg(text: str, border: int = 4) -> bytes:
    from mycelium_a13_macos.vendor.qrcodegen import QrCode  # vendored, pinned, no network
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    size = qr.get_size()
    path = ''.join('M%d,%dh1v1h-1z' % (x + border, y + border)
                   for y in range(size) for x in range(size) if qr.get_module(x, y))
    total = size + 2 * border
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" role="img" aria-label="Swarm link QR">'
            '<rect width="100%%" height="100%%" fill="#fff"/><path d="%s" fill="#000"/></svg>'
            % (total, total, path)).encode()


class DirectoryServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, directory: Directory, *, port: int = 0, public_origin: str | None = None,
                 trusted_proxy: bool = False, download: dict | None = None) -> None:
        self.directory = directory
        self.download = dict(hosted_defaults.HELPER_DOWNLOAD if download is None else download)
        self.trusted_proxy = trusted_proxy
        self.slots = threading.BoundedSemaphore(32)
        super().__init__(('127.0.0.1', port), _Handler)
        self.origin = 'http://127.0.0.1:%d' % self.server_port
        self.public_origin = public_origin or self.origin
        self.thread: threading.Thread | None = None

    def process_request(self, request, client_address):
        request.settimeout(10)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass

    def start(self) -> 'DirectoryServer':
        self.thread = threading.Thread(target=self.serve_forever, name='swarm-directory', daemon=True)
        self.thread.start()
        return self

    def close(self) -> None:
        if self.thread is not None:
            self.shutdown()
            self.thread.join(5)
        self.server_close()


class _Handler(BaseHTTPRequestHandler):
    server: DirectoryServer
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args: Any) -> None:  # no logs: no IPs, keys or paths kept
        pass

    def _send(self, status: int, raw: bytes, kind: str = 'application/json') -> None:
        self.send_response(status)
        self.send_header('Content-Type', kind)
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', csp(self.server.directory.allow_loopback))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cross-Origin-Opener-Policy', 'same-origin')
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(raw)
        self.close_connection = True

    def _json(self, status: int, value: Any) -> None:
        self._send(status, json.dumps(value, separators=(',', ':'), ensure_ascii=False).encode())

    def _client(self) -> str:
        if self.server.trusted_proxy:
            value = self.headers.get('Cf-Connecting-Ip') or ''
            return 'ip:' + value if _IP.fullmatch(value) else 'ip:unknown'
        return 'ip:' + self.client_address[0]

    def do_GET(self) -> None:  # noqa: N802
        path = self.path
        if any(part in path for part in ('..', '%', '?', '#', '\\')):
            return self._json(404, {'error': 'route_unknown'})
        if path in ASSETS:
            name, kind = ASSETS[path]
            return self._send(200, (WEB / name).read_bytes(), kind)
        if path == API + '/config':
            return self._json(200, {'directory': self.server.public_origin, 'privacy': PRIVACY_NOTE,
                                    'demo': DEMO_LABEL, 'download': self.server.download, 'limits': {'max_prompt_chars': docs.MAX_PROMPT_CHARS,
                                                                   'max_new_tokens': docs.MAX_NEW_TOKENS}})
        match = re.fullmatch(API + r'/swarms/(sw-[a-z0-9]{20})/link\.svg', path)
        if match:
            row = self.server.directory.store.get('swarm', match.group(1))
            if row is None:
                return self._json(404, {'error': 'swarm_unknown'})
            link = docs.swarm_link(self.server.public_origin, match.group(1), row['record']['owner_key'])
            return self._send(200, qr_svg(link), 'image/svg+xml')
        if path.startswith(API + '/'):
            status, value = self.server.directory.handle('GET', path[len(API):], b'', self._client())
            return self._json(status, value)
        return self._json(404, {'error': 'route_unknown'})

    do_HEAD = do_GET

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith(API + '/') or any(p in self.path for p in ('..', '%', '?', '#', '\\')):
            return self._json(404, {'error': 'route_unknown'})
        if self.headers.get('X-Mycelium-Swarms') != '1' or \
                (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            return self._json(403, {'error': 'header_required'})
        if self.headers.get('Cookie') is not None or self.headers.get('Authorization') is not None:
            return self._json(400, {'error': 'credentials_refused'})
        limit = self.server.directory.limits.max_body
        try:
            body = self._body(limit)
        except ValueError:
            return self._json(400, {'error': 'frame_invalid'})
        except OverflowError:
            return self._json(413, {'error': 'body_too_large'})
        status, value = self.server.directory.handle('POST', self.path[len(API):], body, self._client())
        return self._json(status, value)

    def do_PUT(self) -> None:  # noqa: N802
        self._json(405, {'error': 'method_not_allowed'})

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    def _body(self, limit: int) -> bytes:
        codings = self.headers.get_all('Transfer-Encoding', [])
        lengths = self.headers.get_all('Content-Length', [])
        if codings:
            # cloudflared may re-frame a body as chunked: accept exactly that, bounded.
            if len(codings) != 1 or codings[0].strip().lower() != 'chunked' or lengths:
                raise ValueError()
            body = bytearray()
            while True:
                line = self.rfile.readline(11)
                if not _CHUNK.fullmatch(line):
                    raise ValueError()
                size = int(line[:-2], 16)
                if size == 0:
                    break
                if len(body) + size > limit:
                    raise OverflowError()
                data = self.rfile.read(size)
                if len(data) != size or self.rfile.read(2) != b'\r\n':
                    raise ValueError()
                body += data
            if self.rfile.read(2) != b'\r\n':
                raise ValueError()
            return bytes(body)
        if len(lengths) != 1 or not lengths[0].isdigit():
            raise ValueError()
        length = int(lengths[0])
        if length > limit:
            raise OverflowError()
        body = self.rfile.read(length)
        if len(body) != length:
            raise ValueError()
        return body


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='python -m mycelium_swarm_directory', allow_abbrev=False,
                                     description='Serve the swarm directory on 127.0.0.1 (behind a tunnel).')
    parser.add_argument('--db', required=True, help='sqlite file (created 0600)')
    parser.add_argument('--port', type=int, default=None, help='default %d' % hosted_defaults.DIRECTORY_PORT)
    parser.add_argument('--public-origin', default=None, help='e.g. https://swarms.mycelium.now')
    parser.add_argument('--hosted', action='store_true',
                        help='the hosted-demo defaults (hosted_defaults.py): public origin %s and --trusted-proxy; '
                             'explicit flags still win' % hosted_defaults.DIRECTORY_PUBLIC_ORIGIN)
    parser.add_argument('--trusted-proxy', action='store_true', help='rate-key on Cf-Connecting-Ip (cloudflared)')
    parser.add_argument('--allow-loopback-entries', action='store_true', help='local testing only')
    network_mode.add_network_mode_argument(parser)
    return parser


def build_directory(args: argparse.Namespace, environ: Any = None) -> Directory:
    """Internet (default): public https entries only; lan: also RFC 1918 http(s) origins."""
    mode = network_mode.resolve_network_mode(args.network_mode, environ)
    private = mode.allows_private_origins
    if args.public_origin is not None:
        docs.origin(args.public_origin, allow_loopback=True, allow_private=private, code='public_origin_invalid')
    return Directory(SqliteStore(args.db), verify, limits=Limits(),
                     allow_loopback_entries=args.allow_loopback_entries, allow_private_entries=private)


def resolve(args: argparse.Namespace) -> argparse.Namespace:
    """Fill in port/origin/proxy trust from the named defaults (``--hosted`` adds the public ones)."""
    resolved = argparse.Namespace(**vars(args))
    if resolved.port is None:
        resolved.port = hosted_defaults.DIRECTORY_PORT
    if resolved.hosted:
        resolved.trusted_proxy = True
        if resolved.public_origin is None:
            resolved.public_origin = hosted_defaults.DIRECTORY_PUBLIC_ORIGIN
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = resolve(build_parser().parse_args(argv))
    directory = build_directory(args)
    server = DirectoryServer(directory, port=args.port, public_origin=args.public_origin,
                             trusted_proxy=args.trusted_proxy)
    print('swarm directory on %s' % server.origin, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
