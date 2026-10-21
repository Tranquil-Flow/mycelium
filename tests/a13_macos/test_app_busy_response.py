# SPDX-License-Identifier: AGPL-3.0-only
"""R1 (owner-approved): a saturated control server must SAY it is busy.

Defect under test (mycelium_a13_macos/app.py:185-203, HEAD b90edaf): when all 16 concurrency
slots are held, ``_Server.process_request`` called ``shutdown_request`` and returned, so the
surplus connection was ACCEPTED AND THEN CLOSED WITH NO RESPONSE AT ALL. A client saw
``ConnectionResetError`` / ``RemoteDisconnected`` -- indistinguishable from a broken server --
so it retried, which makes the pressure worse.

RED-first: the assertions on the refusal path encode the REQUIRED behaviour (an explicit 503
with a fixed, leak-free body), so they FAIL on the unmodified tree and pass only once the server
answers instead of closing silently. The three non-refusal tests (release, normal client,
adversarial clients) hold the surrounding contract and pass on both trees.

Evidence boundary: this file proves the SERVER CAN report saturation. It does NOT prove that
saturation occurred in any round, nor that any round failure was caused by saturation.

On the reset tolerance: the refusal itself is always delivered in full. When the client is still
sending (a POST body in flight) no handler on this path reads that body, so the close that follows
the refusal can be a TCP reset rather than a FIN; measured on this host the 171-byte refusal stays
readable even when the client reads it 300 ms after the reset. The assertions therefore require the
complete refusal, and tolerate at most a reset -- never a silent drop or a silent hold.
"""
import http.client
import http.server
import json
import socket
import struct
import threading
import time

from mycelium_a13_macos.app import DemoApp, _Server

SLOTS = 16          # the concurrency cap under test (app.py:_Server.__init__); frozen by R1
HOLD_TIMEOUT = 20.0  # test-side safety net so a stuck holder can never hang the run


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def observe(port, request_bytes, deadline=3.0, delay=0.0):
    """Send raw bytes; return (received, exception). Bounded, never hangs.

    ``exception`` is None when the server closed the connection cleanly; anything else
    (ConnectionResetError, TimeoutError, ...) is returned so a failing assertion can say
    exactly which non-answer the client got.
    """
    sock = socket.create_connection(('127.0.0.1', port), timeout=deadline)
    try:
        sock.settimeout(deadline)
        sock.sendall(request_bytes)
        if delay:
            time.sleep(delay)
        received = bytearray()
        while True:
            try:
                chunk = sock.recv(4096)
            except TimeoutError as exc:      # neither answered nor closed: a silent hold
                return bytes(received), exc
            except OSError as exc:           # reset / aborted: a silent drop
                return bytes(received), exc
            if not chunk:
                return bytes(received), None
            received.extend(chunk)
    finally:
        sock.close()


def get_request(port):
    return b'GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n' % port


class _HoldHandler(http.server.BaseHTTPRequestHandler):
    """Holds its concurrency slot until released, so saturation is deterministic."""

    protocol_version = 'HTTP/1.1'
    lock = threading.Lock()
    entered = 0
    release = threading.Event()

    @classmethod
    def reset(cls):
        with cls.lock:
            cls.entered = 0
        cls.release.clear()

    @classmethod
    def entered_count(cls):
        with cls.lock:
            return cls.entered

    def do_GET(self):
        self.close_connection = True   # one request per connection: the slot frees on release
        with _HoldHandler.lock:
            _HoldHandler.entered += 1
        _HoldHandler.release.wait(HOLD_TIMEOUT)
        body = b'ok'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_server():
    server = _Server(('127.0.0.1', 0), _HoldHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def saturate(port, count=SLOTS, timeout=5.0):
    """Occupy ``count`` slots with real in-flight requests; fail loudly if that does not happen."""
    holders = [threading.Thread(target=observe, args=(port, get_request(port), HOLD_TIMEOUT),
                                daemon=True) for _ in range(count)]
    for holder in holders:
        holder.start()
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _HoldHandler.entered_count() >= count:
            return holders
        time.sleep(0.01)
    raise AssertionError(
        f'only {_HoldHandler.entered_count()}/{count} requests reached the handler: saturation '
        'was not established, so the surplus request would not be a saturated case')


def release(server, holders):
    _HoldHandler.release.set()
    for holder in holders:
        holder.join(10)
    server.shutdown()


class Authority:
    """Minimal fake authority: proves the HTTP boundary only."""

    def __init__(self, root):
        self.created = False
    def start(self): return self
    def status(self):
        return {'membership': 'not_joined', 'route_ready': False}
    def renew(self): return self.status()
    def stop(self): return None


# ---------------------------------------------------------------------------
# the defect itself: SLOTS in-flight requests, then one more
# ---------------------------------------------------------------------------

def test_saturated_server_answers_the_surplus_request_explicitly():
    _HoldHandler.reset()
    server, port = start_server()
    holders = saturate(port)
    try:
        raw, exc = observe(port, get_request(port))
        assert raw.startswith(b'HTTP/1.1 503'), (
            f'no refusal was delivered: {len(raw)} response bytes, connection ended in {exc!r}')
        head, _, body = raw.partition(b'\r\n\r\n')
        assert body == b'{"error":"server_busy"}', body
        assert json.loads(body) == {'error': 'server_busy'}
        headers = head.decode('latin-1').lower()
        assert f'content-length: {len(body)}' in headers, head
        assert 'retry-after:' in headers, head
        # a refusal leaks no banner, version, path or traceback -- it is a fixed literal
        assert b'Python' not in raw and b'BaseHTTP' not in raw and b'Traceback' not in raw
        assert str(server.server_address).encode() not in raw
        assert exc is None or isinstance(exc, ConnectionResetError), (
            f'the surplus request ended in {exc!r} after {len(raw)} bytes: a saturated server '
            'must deliver the refusal, never a silent drop or a silent hold')
    finally:
        release(server, holders)


def test_a_normal_http_client_reads_the_503_not_a_transport_error():
    """The refusal must be legible to an ordinary client, not only to a raw socket."""
    _HoldHandler.reset()
    server, port = start_server()
    holders = saturate(port)
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    try:
        connection.request('GET', '/')
        response = connection.getresponse()          # must not raise
        assert response.status == 503, response.status
        assert response.reason == 'Service Unavailable', response.reason
        body = response.read()                       # must not raise
        assert json.loads(body) == {'error': 'server_busy'}
        assert response.getheader('Retry-After') == '1'
        assert response.getheader('Connection') == 'close'
    finally:
        connection.close()
        release(server, holders)


def test_release_after_a_refusal_leaves_the_server_serving():
    """The refusal path must not disturb slot accounting: freed slots still serve."""
    _HoldHandler.reset()
    server, port = start_server()
    holders = saturate(port)
    try:
        observe(port, get_request(port))
        _HoldHandler.release.set()
        for holder in holders:
            holder.join(10)
        raw, exc = observe(port, get_request(port), deadline=5.0)
        assert exc is None
        assert raw.startswith(b'HTTP/1.1 200'), raw[:120]
    finally:
        release(server, holders)


def test_silent_and_partial_clients_cannot_stall_the_accept_loop():
    """A refusal must never wait on the client: a silent or half-sent request cannot block
    the accept loop, so the server still serves as soon as a slot frees."""
    _HoldHandler.reset()
    server, port = start_server()
    holders = saturate(port)
    silent = socket.create_connection(('127.0.0.1', port), timeout=3)
    partial = socket.create_connection(('127.0.0.1', port), timeout=3)
    aborted = socket.create_connection(('127.0.0.1', port), timeout=3)
    aborted.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    aborted.close()                       # an aborting client: writing to it fails in the server
    try:
        partial.sendall(b'GET / HT')      # request line never completed
        time.sleep(0.3)                   # all three are refused while the slots stay full
        _HoldHandler.release.set()
        for holder in holders:
            holder.join(10)
        raw, exc = observe(port, get_request(port), deadline=5.0)
        assert exc is None
        assert raw.startswith(b'HTTP/1.1 200'), raw[:120]
    finally:
        silent.close()
        partial.close()
        release(server, holders)


# ---------------------------------------------------------------------------
# the shipped app: the real DemoApp control server
# ---------------------------------------------------------------------------

def _hold_every_slot(app):
    """Take every free concurrency slot on the REAL app server; return how many were taken."""
    held = 0
    while app.server.slots.acquire(blocking=False):
        held += 1
    return held


def test_real_app_server_refuses_explicitly_when_every_slot_is_held(tmp_path):
    app = DemoApp(tmp_path / 'private', authority_factory=Authority, check_platform=False).start()
    try:
        held = _hold_every_slot(app)
        assert held >= 1, 'no concurrency slot could be taken: the test cannot saturate the app'
        body = b'{"probe":true}'
        post = (b'POST /session HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n'
                b'Content-Length: %d\r\n\r\n' % (app.port, len(body))) + body
        for label, request_bytes in (('GET', get_request(app.port)), ('POST with body', post)):
            raw, exc = observe(app.port, request_bytes, deadline=3.0)
            assert raw.startswith(b'HTTP/1.1 503'), (
                f'no refusal was delivered to the surplus {label}: {len(raw)} response bytes, '
                f'connection ended in {exc!r}')
            head, _, refused_body = raw.partition(b'\r\n\r\n')
            assert refused_body == b'{"error":"server_busy"}', refused_body
            assert exc is None or isinstance(exc, ConnectionResetError), (
                f'the real app server ended the surplus {label} with {exc!r} after {len(raw)} '
                'bytes: a saturated app must deliver the refusal, never a silent drop or a hold')
            # a refusal is not a processed request: no auth/origin verdict may appear instead
            assert b'401' not in raw.split(b'\r\n', 1)[0] and b'403' not in raw.split(b'\r\n', 1)[0]
            # nothing private may escape in a refusal
            assert b'Traceback' not in raw and b'File "' not in raw
            assert str(tmp_path).encode() not in raw
        for _ in range(held):
            app.server.slots.release()
        # the app still serves normally once slots are free
        connection = http.client.HTTPConnection('127.0.0.1', app.port, timeout=5)
        connection.request('GET', '/')
        response = connection.getresponse()
        assert response.status == 200
        assert response.read()
        connection.close()
    finally:
        app.close()
