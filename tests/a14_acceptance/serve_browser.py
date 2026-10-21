#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only SPA fallback for the A14 loopback gateway composition.

The ordinary A14 conformance host already composes ProductEvidenceApplication,
ProductGateway and the built web app, but serves only `/` and assets. The
production app canonicalizes /#network to /operator#network; reload and direct
navigation must serve the same built entrypoint at /operator. No API response is
rewritten and no route authority is supplied by this fallback.
"""
import importlib.util
import json
from pathlib import Path
import threading
from http.server import ThreadingHTTPServer

source = Path(__file__).resolve().parents[2] / 'scripts/serve_a14_conformance.py'
spec = importlib.util.spec_from_file_location('a14_conformance_host', source)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class Handler(module.Handler):
    def send_error(self, *args, **kwargs):
        # Optional application probes may be cancelled while this local host
        # writes its ordinary 404 body; keep the 404, not a thread traceback.
        try:
            return super().send_error(*args, **kwargs)
        except (BrokenPipeError, ConnectionResetError):
            return None

    def do_GET(self):
        if self.path.split('?', 1)[0] in ('/operator', '/operator/'):
            self.path = '/'
        return super().do_GET()

if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)  # type: ignore[arg-type]
    server.daemon_threads = True
    timer = threading.Timer(600, server.shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps({'origin': f'http://127.0.0.1:{server.server_port}',
                      'evidence_class': 'local_conformance', 'physical_devices': 0}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        timer.cancel()
        server.server_close()
