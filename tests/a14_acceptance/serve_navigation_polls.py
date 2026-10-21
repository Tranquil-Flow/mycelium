#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Test-only loopback host for the navigation/background-poll browser test.

Serves the BUILT production app through the ordinary A14 conformance
composition (ProductEvidenceApplication + ProductGateway, see
serve_browser.py) and only adds latency, so that a browser test can hold
two windows open deterministically:

* A14_NAV_DOCUMENT_DELAY_MS delays every document response (the app shell).
  A reload, Back or Forward then keeps the old document alive while the new
  one is requested, which is when background poll timers keep firing.
* A14_NAV_POLL_DELAY_MS delays every ``/__mycelium/*`` poll response, so a
  poll is in flight when the navigation starts.

No response body or status is rewritten; the poll endpoints keep answering
404 exactly as the conformance host does (the product's own degraded states).
"""
import importlib.util
import json
import os
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

source = Path(__file__).resolve().parent / 'serve_browser.py'
spec = importlib.util.spec_from_file_location('a14_browser_host', source)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

DOCUMENT_DELAY = int(os.environ.get('A14_NAV_DOCUMENT_DELAY_MS', '0')) / 1000
POLL_DELAY = int(os.environ.get('A14_NAV_POLL_DELAY_MS', '0')) / 1000


class Handler(module.Handler):
    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path in ('/', '/index.html', '/operator', '/operator/'):
            time.sleep(DOCUMENT_DELAY)
        elif path.startswith('/__mycelium/'):
            time.sleep(POLL_DELAY)
        return super().do_GET()


if __name__ == '__main__':
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)  # type: ignore[arg-type]
    server.daemon_threads = True
    timer = threading.Timer(1800, server.shutdown)
    timer.daemon = True
    timer.start()
    print(json.dumps({'origin': f'http://127.0.0.1:{server.server_port}',
                      'evidence_class': 'local_conformance', 'physical_devices': 0}), flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    finally:
        timer.cancel()
        server.server_close()
