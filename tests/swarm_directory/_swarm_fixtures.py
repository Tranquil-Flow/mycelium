# SPDX-License-Identifier: AGPL-3.0-only
"""Shared helpers for the swarm tests. FAKE (labelled): the product gateway (canned tokens over
the registry API contract) and the owner-run stub; everything else is the shipped code."""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def fake_gateway(tokens):
    """FAKE inference backend: the registry API contract with canned tokens."""
    class Gateway(BaseHTTPRequestHandler):
        inference_posts = []

        def log_message(self, *args):
            pass

        def _json(self, status, document, cookie=None):
            raw = json.dumps(document).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == '/api/v1/bootstrap':
                return self._json(200, {'session': {'csrf_header': 'X-Mycelium-CSRF', 'csrf_token': 't'}},
                                  cookie='mycelium_session=s; Path=/; HttpOnly')
            if self.path == '/api/v1/qualification/current':
                return self._json(200, {'route_ready': True,
                                        'binding': {'deployment_id': 'd', 'model_id': 'Qwen/Qwen3-0.6B'}})
            if self.path == '/api/v1/inference/r1/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                for i, text in enumerate(tokens):
                    self.wfile.write(('event: token\ndata: %s\n\n' % json.dumps({'token_index': i, 'text': text}))
                                     .encode())
                self.wfile.write(b'event: completed\ndata: {}\n\n')
                return
            return self._json(404, {'error': {'code': 'not_found'}})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
            Gateway.inference_posts.append(body)
            return self._json(202, {'request_id': 'r1', 'event_path': '/api/v1/inference/r1/events'})

    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, Gateway


class Run:
    def __init__(self, origin):
        self.origin = origin
        self.state = 'serving'

    def status(self):
        serving = self.state == 'serving'
        return {'state': self.state, 'step': 'serving' if serving else None, 'route_ready': serving,
                'registry_origin': self.origin, 'run_id': 'r',
                'participants': [{'node_id': 'a' * 20, 'label': 'Owner Mac', 'layers': [0, 14]},
                                 {'node_id': 'b' * 20, 'label': 'Joined Mac', 'layers': [14, 28]}]}


class FakeApp:
    installation_role = 'owner'

    def __init__(self, root, registry_origin):
        self.root = root
        self.owner_run = Run(registry_origin)
        self.chat = None
        self.server = None
        self.public_relay = None

    def run_model_contract(self):
        return type('Model', (), {'model_id': 'Qwen/Qwen3-0.6B'})()


def request(origin, path, document=None, *, method=None, headers=None):
    data = None if document is None else json.dumps(document).encode()
    all_headers = {'Content-Type': 'application/json', **(headers or {})}
    req = urllib.request.Request(origin + path, data=data, headers=all_headers,
                                 method=method or ('GET' if data is None else 'POST'))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as response:
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), dict(response.headers)
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b'null'), dict(error.headers)
