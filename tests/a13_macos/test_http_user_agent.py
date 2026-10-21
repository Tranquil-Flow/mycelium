# SPDX-License-Identifier: AGPL-3.0-only
"""PV18: every HTTP client the app or node points at a public origin names itself.

Cloudflare's Browser Integrity Check answers ``403 error 1010`` to the default
``Python-urllib`` agent (live finding, 2026-10-20 go-live).  Every client therefore sends
``User-Agent: Mycelium/<version> (+https://mycelium.now)`` through one helper
(``mycelium_http_identity.with_user_agent``); a static contract keeps a new ``Request()``,
``urlopen`` or ``http.client`` call from going out without it.
"""
from __future__ import annotations

import ast
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mycelium_a13_macos

ROOT = Path(__file__).resolve().parents[2]
_AGENT = re.compile(r"^Mycelium/" + re.escape(mycelium_a13_macos.__version__) + r" \(\+https://mycelium\.now\)\Z")

#: Source scanned by the static contract (the app, the directory, the seed/relay and the
#: acquisition clients; ``mycelium_platform_neutral`` has none today and is kept in scope).
SCANNED = (
    "mycelium_a13_macos", "mycelium_swarm_directory", "mycelium_seed", "mycelium_platform_neutral",
    "mycelium_request_gateway", "mycelium_live/artifact_transport.py", "mycelium_internet/physical.py",
    "mycelium_demo_watchdog/probes.py",
)
#: ``surface_probe`` is the public-surface monitor: it names itself ``mycelium-surface-probe/1``
#: (pinned by tests/swarm_directory/test_surface_probe_user_agent.py), never Python-urllib.
EXPLICIT_OTHER_AGENT = {"mycelium_swarm_directory/surface_probe.py"}

#: Calls that put a request on the wire.  ``Request(...)`` builds the urllib request;
#: ``.request(...)`` is ``http.client``; bare ``urlopen``/``.open(url_string)`` would carry no headers.
_REQUEST_NAMES = {"Request"}


def _sources():
    for entry in SCANNED:
        path = ROOT / entry
        yield from ([path] if path.suffix == ".py" else sorted(path.rglob("*.py")))


def _sent_requests(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in _REQUEST_NAMES:
            yield node, "Request"
        elif isinstance(func, ast.Attribute) and func.attr in _REQUEST_NAMES and isinstance(func.value, (ast.Name, ast.Attribute)):
            yield node, "Request"
        elif isinstance(func, ast.Attribute) and func.attr == "request" and isinstance(func.value, ast.Name) \
                and func.value.id in {"connection", "conn"}:
            yield node, "http.client request"
        elif (isinstance(func, ast.Name) and func.id == "urlopen") or (isinstance(func, ast.Attribute) and func.attr == "urlopen"):
            # a *call* of urlopen (the DirectoryProbe default is a reference, not a call)
            yield node, "urlopen"


def test_every_request_in_the_app_and_directory_packages_carries_the_user_agent():
    missing, seen = [], 0
    for path in _sources():
        relative = path.relative_to(ROOT).as_posix()
        if relative in EXPLICIT_OTHER_AGENT:
            continue
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node, kind in _sent_requests(tree):
            if kind == "urlopen":
                missing.append(f"{relative}:{node.lineno} bare urlopen sends no User-Agent")
                continue
            seen += 1
            segment = ast.get_source_segment(text, node) or ""
            if "with_user_agent(" not in segment:
                missing.append(f"{relative}:{node.lineno} {kind} without with_user_agent(...)")
    assert seen >= 15, f"the scan found only {seen} requests; the contract is not looking at the clients"
    assert not missing, "\n".join(missing)


def test_no_scanned_client_hard_codes_a_second_agent_string_or_python_urllib():
    offenders = []
    for path in _sources():
        relative = path.relative_to(ROOT).as_posix()
        if relative in EXPLICIT_OTHER_AGENT:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"""['"]User-Agent['"]""", line, re.IGNORECASE) and "mycelium_http_identity" not in line:
                offenders.append(f"{relative}:{number}: {line.strip()}")
    assert not offenders, "\n".join(offenders)


def test_the_agent_names_the_product_version_and_site():
    import mycelium_http_identity as identity

    assert identity.USER_AGENT == f"Mycelium/{mycelium_a13_macos.__version__} (+https://mycelium.now)"
    assert _AGENT.fullmatch(identity.USER_AGENT)
    assert identity.with_user_agent() == {"User-Agent": identity.USER_AGENT}
    merged = identity.with_user_agent({"Accept": "application/json", "User-Agent": "Python-urllib/3.14"})
    assert merged == {"Accept": "application/json", "User-Agent": identity.USER_AGENT}


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str | None] = []

    def _answer(self):
        type(self).seen.append(self.headers.get("User-Agent"))
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._answer()

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        self._answer()

    def log_message(self, *args):  # noqa: ANN002
        return


def _serve():
    handler = type("H", (_Recorder,), {"seen": []})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, handler


def test_the_join_window_client_sends_the_agent():
    from mycelium_a13_macos.pairing import JoinWindowClient

    server, handler = _serve()
    try:
        client = JoinWindowClient("https://example.trycloudflare.com", _test_base_url=f"http://127.0.0.1:{server.server_port}")
        assert client.post("/window", b"{}") == b'{"ok":true}'
    finally:
        server.shutdown()
    assert len(handler.seen) == 1 and _AGENT.fullmatch(handler.seen[0] or ""), handler.seen


def test_the_swarm_publish_directory_client_sends_the_agent_on_get_and_post():
    from mycelium_a13_macos.swarm_publish import DirectoryClient

    server, handler = _serve()
    try:
        client = DirectoryClient(f"http://127.0.0.1:{server.server_port}")
        client.get("/swarms")
        client.post("/heartbeat", {"a": 1})
    finally:
        server.shutdown()
    assert len(handler.seen) == 2 and all(_AGENT.fullmatch(value or "") for value in handler.seen), handler.seen


def test_the_swarm_entry_probe_and_directory_watchdog_send_the_agent():
    from mycelium_swarm_directory import probe, watchdog

    server, handler = _serve()
    try:
        origin = f"http://127.0.0.1:{server.server_port}"
        probe._post(origin, "x", {"a": 1})
        try:
            watchdog.DirectoryProbe(origin, origin)._get("/swarms/api/v1/config")
        except OSError:
            pass
    finally:
        server.shutdown()
    assert len(handler.seen) == 2 and all(_AGENT.fullmatch(value or "") for value in handler.seen), handler.seen


def test_the_doh_fallback_request_sends_the_agent(monkeypatch):
    from mycelium_a13_macos import public_https

    captured = []

    class _Opener:
        def open(self, request, timeout):
            captured.append(request.get_header("User-agent"))
            raise OSError("stop")

    monkeypatch.setattr(public_https, "build_opener", lambda *handlers: _Opener())
    try:
        public_https._dns_document("a-b.trycloudflare.com", timeout=1)
    except OSError:
        pass
    assert len(captured) == 1 and _AGENT.fullmatch(captured[0] or ""), captured
