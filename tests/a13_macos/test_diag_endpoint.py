# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: the owner-only /api/diagnostics endpoint (and the member's own view) over the real HTTP server."""
from __future__ import annotations

import http.client
import io
import json
import re
import time
import urllib.parse
import zipfile
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.app import DemoApp
from mycelium_a13_macos.diagnostics import ensure_installed
from mycelium_a13_macos.diagnostics.state import state_for

SNTP = "sudo sntp -sS time.apple.com"


class Authority:
    def __init__(self, root):
        self.coordinator = None
        self.control_mailbox = None

    def start(self):
        return self

    def status(self):
        return {"membership": "not_joined", "route_ready": False}

    def stop(self):
        pass

    def renew(self):
        return self.status()


@pytest.fixture
def app(tmp_path):
    a = DemoApp(tmp_path / "private", authority_factory=Authority, check_platform=False).start()
    yield a
    a.close()


def get(app, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=10)
    c.request("GET", path, headers=headers or {})
    r = c.getresponse()
    out = (r.status, dict(r.getheaders()), r.read())
    c.close()
    return out


def login(app):
    code = re.search(r'name="launch" value="([^"]+)"', app.launch_file.read_text()).group(1)
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=3)
    c.request("POST", "/session", urllib.parse.urlencode({"launch": code}),
              {"Content-Type": "application/x-www-form-urlencoded", "Origin": "null"})
    r = c.getresponse()
    assert r.status == 303
    cookie = r.getheader("Set-Cookie").split(";")[0]
    r.read()
    c.close()
    return {"Cookie": cookie}


def test_diagnostics_need_the_local_session(app):
    assert get(app, "/api/diagnostics")[0] == 401
    assert get(app, "/api/diagnostics/bundle")[0] == 401


def test_owner_document_shape(app):
    status, headers, raw = get(app, "/api/diagnostics", login(app))
    assert status == 200 and headers["Content-Type"].startswith("application/json")
    assert headers["Cache-Control"] == "no-store"
    doc = json.loads(raw)
    assert doc["schema"] == "mycelium.diagnostics.v1" and doc["role"] == "owner"
    assert {"peers", "chats", "relay", "timings", "host", "findings", "member_view", "generated_at"} <= set(doc)
    assert doc["member_view"] is None
    assert doc["host"]["disk_free_bytes"] > 0
    assert doc["relay"]["state"] in {"private", "stopped", "relay_process_running"}
    # no route is open in this test: the reason is stated rather than a silent null
    assert doc["timings"]["available"] is False and doc["timings"]["reason"]
    assert doc["timings"]["per_hop"]["available"] is False and doc["timings"]["per_hop"]["reason"]
    assert doc["chats"]["available"] in (True, False)


def test_a_skewed_peer_shows_up_as_a_finding_with_the_fix(app):
    node = "nodeaaaa1111"
    app.authority.coordinator = SimpleNamespace(members=lambda: (
        {"node_id": node, "generation": 1, "lifecycle_state": "active", "liveness_status": "idle_fresh",
         "liveness_age_seconds": 0.5, "liveness_dead": False},))
    state_for(app).clock.add(node, remote_ts=time.time() + 9.19, local_ts=time.time())
    doc = json.loads(get(app, "/api/diagnostics", login(app))[2])
    peer = doc["peers"][0]
    assert peer["node_id"] == node and peer["clock"]["direction"] == "ahead"
    assert peer["clock"]["offset_s"] == pytest.approx(9.19, abs=0.3)
    assert peer["last_seen"]["membership_age_s"] == pytest.approx(0.5, abs=0.01)
    assert peer["liveness"]["available"] is False and peer["liveness"]["reason"]
    assert peer["mailbox"]["available"] is False  # no mailbox in this fake authority; the reason is stated
    item = next(f for f in doc["findings"] if f["id"] == "peer_clock_skew")
    assert SNTP in item["fix"]


def test_member_sees_its_own_view(app):
    app.installation_role = "target"
    state = state_for(app)
    state.clock.add("owner-node", remote_ts=time.time() - 9.19, local_ts=time.time())
    doc = json.loads(get(app, "/api/diagnostics", login(app))[2])
    assert doc["role"] == "target" and doc["peers"] == []
    view = doc["member_view"]
    assert view["owner_clock"]["direction"] == "behind"
    assert view["claim_loop"]["polls"] == 0
    assert any(f["id"] == "owner_clock_skew" for f in doc["findings"])


def test_bundle_download(app):
    status, headers, raw = get(app, "/api/diagnostics/bundle", login(app))
    assert status == 200 and headers["Content-Type"] == "application/zip"
    assert re.fullmatch(r'attachment; filename="mycelium-support-\d{8}T\d{6}Z\.zip"', headers["Content-Disposition"])
    assert headers["Cache-Control"] == "no-store"
    names = zipfile.ZipFile(io.BytesIO(raw)).namelist()
    assert "diagnostics.json" in names and "versions.json" in names


def test_diagnostics_are_read_only_over_http(app):
    headers = {**login(app), "Origin": app.origin, "X-Mycelium-Action": "1", "Content-Type": "application/json"}
    c = http.client.HTTPConnection("127.0.0.1", app.port, timeout=3)
    c.request("POST", "/api/diagnostics", body="{}", headers=headers)
    r = c.getresponse()
    r.read()
    c.close()
    assert r.status != 200
    assert get(app, "/api/diagnostics?x=1", login(app))[0] == 400  # the server's own path guard


def test_diagnostics_are_not_reachable_through_the_chat_or_public_routes(app):
    status, _, raw = get(app, "/chat/api/diagnostics")
    assert status != 200 and b"mycelium.diagnostics" not in raw


def test_install_probes_once_on_the_owner_mailbox(app):
    calls = []

    class Mailbox:
        def handle_http(self, method, path, body):
            calls.append(path)
            return 200, {}

        def _clock(self):
            return 1.0

    mailbox = Mailbox()
    app.authority.control_mailbox = mailbox
    ensure_installed(app)
    first = mailbox.handle_http
    ensure_installed(app)
    assert mailbox.handle_http is first  # not wrapped twice
    mailbox.handle_http("POST", "/managed-control/poll",
                        {"statement": {"target_node_id": "n1", "issued_at": 10.0}})
    assert calls == ["/managed-control/poll"]
    assert state_for(app).clock.estimate("n1")["samples"] == 1


def test_status_polling_installs_the_probe(app):
    class Mailbox:
        def handle_http(self, method, path, body):
            return 200, {}

        def _clock(self):
            return 1.0

    app.authority.control_mailbox = Mailbox()
    app.status()
    assert getattr(app.authority.control_mailbox, "_diag_probe_state", None) is state_for(app)


def test_the_card_script_is_served_with_the_other_assets(app):
    status, headers, raw = get(app, "/diagnostics.js")
    assert status == 200 and headers["Content-Type"].startswith("text/javascript")
    assert b"/api/diagnostics" in raw
