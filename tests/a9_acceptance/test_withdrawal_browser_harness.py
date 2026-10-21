# SPDX-License-Identifier: AGPL-3.0-only
"""Two-session browser withdrawal harness: unit tests with fakes, plus a real-Chromium smoke.

No devices and no product origin.  The fakes render the Nodes table from the *real*
authority overlay and publisher door, so the harness is checked against the actual
fault contract; the Chromium smoke serves a tiny non-product page (its own origin,
no product CSP) only to prove the locator reads and CDP EventSource collection work
in a real browser.  Never gate evidence.
"""
from __future__ import annotations

import ast
import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mycelium_live import supervisor
from tests.a9_acceptance.test_a9_authority_fault_control import GRANT, GRANT_ENV, _Route, _clean_ledger, _records  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
import run_a9_withdrawal_browser as harness  # noqa: E402

HEADERS = ["Device", "Class", "Member generation", "Authority generation", "Lifecycle", "Runtime", "Lease",
           "Activation eligible", "Placement"]


def _render(records):
    """The product Nodes table (pseudonymous device ids) from overlay records."""
    rows = {}
    for record in records:
        capability = record["runtime_capability"]
        entity = "device-" + record["node_id"][-1] + "x"
        rows[entity] = dict(zip(HEADERS, [
            entity, record["peer_class"], str(record["generation"]), str(record["authority_generation"]),
            record["lifecycle_state"], f"{capability['runtime_backend']} · {capability['transport']}", "fresh",
            "Yes" if record["activation_eligible"] else "No", "Unavailable"]))
    return rows


class FakeLocator:
    def __init__(self, page, kind, index=None):
        self.page, self.kind, self.index = page, kind, index

    def filter(self, has_text):
        return FakeLocator(self.page, self.kind + "|" + has_text, self.index)

    def locator(self, selector):
        if self.kind.startswith("section.panel") and selector == "thead th":
            return FakeLocator(self.page, "headers")
        if self.kind.startswith("section.panel") and selector == "tbody tr":
            return FakeLocator(self.page, "rows")
        if self.kind == "row" and selector == "th, td":
            return FakeLocator(self.page, "cells", self.index)
        raise AssertionError(f"unexpected locator chain {self.kind!r} {selector!r}")

    def nth(self, index):
        return FakeLocator(self.page, "row", index)

    def all_inner_texts(self):
        shown = self.page.shown_rows()
        if self.kind == "headers":
            return list(HEADERS)
        return list(list(shown.values())[self.index].values())

    def count(self):
        if self.kind == "rows":
            return len(self.page.shown_rows())
        return int(self.page.withheld and self.kind.startswith("status"))


class FakePage:
    def __init__(self, world, *, delay=0, blind=False, stream_events=True, withheld=False):
        self.world, self.delay, self.blind, self.stream_events, self.withheld = world, delay, blind, stream_events, withheld
        self.gotos, self.stream, self.navigations = [], [], []
        self._seen = None
        self._reads = 0
        self._shown = None

    def goto(self, url):
        self.gotos.append(url)
        self.navigations.append(url)
        self._shown = None

    def shown_rows(self):
        current = _render(self.world.records())
        if self._shown is None:
            self._shown, self._seen, self._reads = current, current, 0
        elif current != self._seen:
            self._reads += 1
            if not self.blind and self._reads > self.delay:
                self._shown, self._seen, self._reads = current, current, 0
                if self.stream_events:
                    self._deliver()
        return self._shown

    def _deliver(self):
        self.stream.append({"event": "message", "id": str(len(self.stream)), "bytes": 10, "digest": "sha256:x"})

    def locator(self, selector):
        assert selector == "section.panel"
        return FakeLocator(self, selector)

    def get_by_role(self, role):
        assert role == "status"
        return FakeLocator(self, "status")


class World:
    """Real overlay + real ledger semantics behind a fake product."""

    def __init__(self):
        from mycelium_a9_authority_ledger import LEDGER, overlay_membership
        self.ledger, self.overlay = LEDGER, overlay_membership
        self.published = []

    def records(self):
        return self.overlay(_records())

    def publish(self, *, kind, node_id, value):
        self.published.append((kind, node_id, value))
        generation = self.ledger.publish(kind, node_id, grant=GRANT, value=value)
        return {"kind": kind, "node_id": node_id, "generation": generation}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _sessions(world, **second):
    pages = [FakePage(world), FakePage(world, **second)]
    return [harness.Session(n, p, p.stream, p.navigations) for n, p in zip(("session-a", "session-b"), pages)], pages


def _run(world, sessions, publisher=None, **kw):
    clock = Clock()
    return harness.run_withdrawal(sessions, publisher or world.publish, base_url="http://127.0.0.1:9", node_id="node-a",
                                  timeout_s=5, interval_s=0.5, clock=clock, sleep=clock.sleep, **kw)


def test_both_sessions_see_each_fault_withdrawn_then_restored_without_a_reload():
    world = World()
    sessions, pages = _sessions(world, delay=3)
    receipt = _run(world, sessions)
    assert receipt["blockers"] == [] and receipt["passed"] is True
    assert [s["kind"] for s in receipt["steps"]] == ["stage_withdrawal", "codec_withdrawal", "unknown_registry_value"]
    for step in receipt["steps"]:
        for observed in step["sessions"].values():
            assert observed["eligible_before"] == "Yes" and observed["eligible_after"] == "No"
            assert observed["authority_generation_after"] > observed["authority_generation_before"]
            assert observed["navigations_after_publish"] == 0 and observed["stream_messages_after_publish"] >= 1
        assert step["checks"]["same_capacity_in_both_sessions"] is True
        assert {r["eligible"] for r in step["restored"].values()} == {"Yes"}
    assert receipt["steps"][2]["sessions"]["session-a"]["entity"] == "device-ax"
    assert all(len(p.gotos) == 1 and p.gotos[0].endswith("/#nodes") for p in pages)  # one load, never reloaded
    assert [p[0] for p in world.published] == ["stage_withdrawal", "restore", "codec_withdrawal", "restore",
                                               "unknown_registry_value", "restore"]
    assert "claim_boundary" in receipt and "token" not in json.dumps(receipt).lower()


def test_an_acknowledged_publish_the_second_session_never_renders_is_a_failure():
    world = World()
    sessions, _ = _sessions(world, blind=True)
    receipt = _run(world, sessions)
    assert receipt["passed"] is False
    assert "stage_withdrawal:session-b:withdrawal_not_observed" in receipt["blockers"]
    assert receipt["steps"][0]["ack"]["kind"] == "stage_withdrawal"  # acked, still not evidence
    assert len(receipt["steps"]) == 1  # stops at the first failed fault
    assert world.published[-1][0] == "restore"  # the fault is cleaned up


def test_no_events_stream_message_after_publish_fails_even_if_the_ui_changed():
    world = World()
    sessions, _ = _sessions(world, stream_events=False)
    receipt = _run(world, sessions)
    assert receipt["passed"] is False
    assert "stage_withdrawal:session-b:no_events_stream_message_after_publish" in receipt["blockers"]


# -- the Nodes workspace reads /events with fetch(Accept: text/event-stream) + a body reader ----------


class FakeCdp:
    """The slice of a Playwright CDPSession the harness uses: ``on`` plus a test-side ``emit``."""

    def __init__(self):
        self.handlers = {}

    def on(self, name, handler):
        self.handlers.setdefault(name, []).append(handler)

    def emit(self, name, params):
        for handler in self.handlers.get(name, []):
            handler(params)


EVENTS_URL = "http://127.0.0.1:9/api/v1/product/events"


def _fetch_request(request_id="r1", url=EVENTS_URL, accept="text/event-stream"):
    return {"requestId": request_id, "request": {"url": url, "method": "GET", "headers": {"Accept": accept}}}


class FetchStreamPage(FakePage):
    """Delivery arrives as a fetch body-reader chunk (CDP dataReceived), never an EventSource message."""

    def __init__(self, world, *, data_length=120, announce=True, **kw):
        super().__init__(world, **kw)
        self.cdp, self.data_length, self.announce, self._requests = FakeCdp(), data_length, announce, 0
        harness.record_event_streams(self.cdp, self.stream)

    def _deliver(self):
        self._requests += 1
        request_id = f"fetch-{self._requests}"
        if self.announce:
            self.cdp.emit("Network.requestWillBeSent", _fetch_request(request_id))
        self.cdp.emit("Network.dataReceived", {"requestId": request_id, "dataLength": self.data_length,
                                               "encodedDataLength": self.data_length})
        self.cdp.emit("Network.loadingFinished", {"requestId": request_id, "encodedDataLength": self.data_length})


def _fetch_sessions(world, **kw):
    pages = [FetchStreamPage(world, **kw), FetchStreamPage(world, **kw)]
    return [harness.Session(n, p, p.stream, p.navigations) for n, p in zip(("session-a", "session-b"), pages)], pages


def test_a_fetch_stream_delivery_on_events_counts_and_the_run_passes():
    world = World()
    sessions, pages = _fetch_sessions(world)
    receipt = _run(world, sessions)
    assert receipt["blockers"] == [] and receipt["passed"] is True
    for step in receipt["steps"]:
        for observed in step["sessions"].values():
            assert observed["stream_messages_after_publish"] >= 1
    assert {entry["event"] for page in pages for entry in page.stream} == {"fetch-stream"}


def test_no_fetch_stream_delivery_after_publish_is_the_no_events_stream_message_blocker():
    world = World()
    sessions, _ = _fetch_sessions(world, data_length=0)  # request opened, nothing read
    receipt = _run(world, sessions)
    assert receipt["passed"] is False
    assert "stage_withdrawal:session-a:no_events_stream_message_after_publish" in receipt["blockers"]
    assert "stage_withdrawal:session-b:no_events_stream_message_after_publish" in receipt["blockers"]
    unannounced = World()
    sessions2, _ = _fetch_sessions(unannounced, announce=False)  # bytes on a request that was never an /events stream
    receipt2 = _run(unannounced, sessions2)
    assert "stage_withdrawal:session-a:no_events_stream_message_after_publish" in receipt2["blockers"]


def test_only_event_stream_fetches_to_events_are_recorded():
    cdp, stream = FakeCdp(), []
    harness.record_event_streams(cdp, stream)
    for request_id, url, accept in (("a", "http://h/api/v1/product/export", "text/event-stream"),  # not /events
                                    ("b", EVENTS_URL, "application/json"),  # not an event stream
                                    ("c", EVENTS_URL + "?after=3", "Text/Event-Stream, */*")):  # query, mixed case: yes
        cdp.emit("Network.requestWillBeSent", _fetch_request(request_id, url, accept))
        cdp.emit("Network.dataReceived", {"requestId": request_id, "dataLength": 50, "encodedDataLength": 50})
    cdp.emit("Network.dataReceived", {"requestId": "never-seen", "dataLength": 50, "encodedDataLength": 50})
    assert [(e["event"], e["id"], e["bytes"]) for e in stream] == [("fetch-stream", "c", 50)]


def test_loading_finished_with_bytes_counts_once_and_empty_or_failed_requests_do_not():
    cdp, stream = FakeCdp(), []
    harness.record_event_streams(cdp, stream)
    cdp.emit("Network.requestWillBeSent", _fetch_request("x"))
    cdp.emit("Network.dataReceived", {"requestId": "x", "dataLength": 40, "encodedDataLength": 40})
    cdp.emit("Network.loadingFinished", {"requestId": "x", "encodedDataLength": 40})  # already counted via dataReceived
    cdp.emit("Network.requestWillBeSent", _fetch_request("y"))
    cdp.emit("Network.loadingFinished", {"requestId": "y", "encodedDataLength": 90})  # no dataReceived seen: bytes finished
    cdp.emit("Network.requestWillBeSent", _fetch_request("z"))
    cdp.emit("Network.loadingFinished", {"requestId": "z", "encodedDataLength": 0})
    cdp.emit("Network.requestWillBeSent", _fetch_request("w"))
    cdp.emit("Network.loadingFailed", {"requestId": "w"})
    cdp.emit("Network.dataReceived", {"requestId": "w", "dataLength": 10, "encodedDataLength": 10})
    assert [e["id"] for e in stream] == ["x", "y"]


def test_eventsource_messages_are_still_recorded_for_the_observatory_path():
    cdp, stream = FakeCdp(), []
    harness.record_event_streams(cdp, stream)
    cdp.emit("Network.eventSourceMessageReceived", {"eventName": "message", "eventId": "7", "data": "hello"})
    assert [(e["event"], e["id"], e["bytes"]) for e in stream] == [("message", "7", 5)]
    assert stream[0]["digest"].startswith("sha256:")


def test_a_reload_after_publish_fails(monkeypatch):
    world = World()
    sessions, pages = _sessions(world)
    original = world.publish

    def reloading(**kw):
        pages[0].navigations.append("reload")  # a main-frame navigation after the baseline
        return original(**kw)
    receipt = _run(world, sessions, publisher=reloading)
    assert "stage_withdrawal:session-a:page_reloaded_after_publish" in receipt["blockers"]
    assert receipt["passed"] is False


def test_a_second_changed_device_or_a_pinned_mismatch_is_not_the_target_capacity():
    world = World()
    sessions, _ = _sessions(world)
    receipt = _run(world, sessions, pinned_entity="device-bx")
    assert "stage_withdrawal:session-a:not_exactly_the_target_capacity" in receipt["blockers"]
    world2 = World()
    sessions2, _ = _sessions(world2)

    def two(**kw):
        world2.publish(kind="stage_withdrawal", node_id="node-b", value=None)
        return world2.publish(**kw)
    receipt2 = _run(world2, sessions2, publisher=two)
    assert receipt2["passed"] is False
    assert any(b.endswith("not_exactly_the_target_capacity") for b in receipt2["blockers"])


def test_a_change_that_is_not_a_withdrawal_or_does_not_advance_authority_is_a_failure():
    world = World()
    sessions, _ = _sessions(world)

    def only_restore(**kw):  # bumps the authority generation, leaves the capacity eligible
        return world.publish(kind="restore", node_id=kw["node_id"], value=None)
    receipt = _run(world, sessions, publisher=only_restore)
    assert "stage_withdrawal:session-a:capacity_not_withdrawn" in receipt["blockers"]

    class Frozen(World):
        def records(self):  # withdraws the capacity but never advances authority_generation
            return tuple(dict(r, authority_generation=3) for r in super().records())
    frozen = Frozen()
    sessions2, _ = _sessions(frozen)
    receipt2 = _run(frozen, sessions2)
    assert "stage_withdrawal:session-a:authority_generation_not_advanced" in receipt2["blockers"]


def test_sessions_that_withdraw_different_capacities_are_a_failure():
    world_a, world_b = World(), World()
    pages = [FakePage(world_a), FakePage(world_b)]
    sessions = [harness.Session(n, p, p.stream, p.navigations) for n, p in zip(("session-a", "session-b"), pages)]

    def split(**kw):
        world_b.publish(kind=kw["kind"], node_id="node-b" if kw["kind"] != "restore" else "node-a", value=kw["value"])
        return world_a.publish(**kw)
    receipt = _run(world_a, sessions, publisher=split)
    assert "stage_withdrawal:sessions_name_different_capacities" in receipt["blockers"]
    assert receipt["steps"][0]["checks"]["same_capacity_in_both_sessions"] is False


def test_an_unexpected_page_error_after_publish_still_restores_the_fault():
    world = World()
    sessions, pages = _sessions(world)
    broken = pages[1]
    original = broken.shown_rows
    broken.shown_rows = lambda: (_ for _ in ()).throw(RuntimeError("page crashed")) if world.published else original()
    with pytest.raises(RuntimeError, match="page crashed"):
        _run(world, sessions)
    assert world.published[-1][0] == "restore"


def test_publisher_refusal_and_withheld_baseline_and_same_session_are_blockers_not_crashes():
    world = World()
    sessions, _ = _sessions(world)

    def refused(**kw):
        raise harness.HarnessRefused("publisher_refused:403:a9_authority_fault_control_refused")
    receipt = _run(world, sessions, publisher=refused)
    assert receipt["passed"] is False and receipt["blockers"] == [
        "stage_withdrawal:publisher_refused:403:a9_authority_fault_control_refused"]
    withheld = harness.Session("session-a", FakePage(world, withheld=True), [], [])
    other = harness.Session("session-b", FakePage(world), [], [])
    out = _run(world, [withheld, other])
    assert "session-a:nodes_not_live_before_fault" in out["blockers"] and out["steps"] == []
    with pytest.raises(harness.HarnessRefused, match="two_distinct_sessions_required"):
        _run(world, [other, other])


def test_publish_fault_speaks_the_real_door_and_refuses_bad_credentials(monkeypatch, tmp_path):
    from mycelium_a9_authority_ledger import overlay_membership
    tmp_path.chmod(0o700)
    monkeypatch.setenv(GRANT_ENV, GRANT)
    control = supervisor._mount_a9_authority_fault_control(
        route=_Route(), product_port=8787, control_port=0, product_state_root=tmp_path,
        public_origin=None, trusted_https_proxy=False)
    try:
        token = (tmp_path / "a9-authority-fault.token").read_text().strip()
        ok = harness.publish_fault(port=control.port, token=token, grant=GRANT, kind="codec_withdrawal", node_id="node-a")
        assert ok["kind"] == "codec_withdrawal" and ok["node_id"] == "node-a"
        assert overlay_membership(_records())[0]["activation_eligible"] is False
        for kwargs, code in (({"token": "bad"}, "403"), ({"grant": "FLEET GRANTED TO A11"}, "403"),
                             ({"node_id": "ghost"}, "409"), ({"kind": "explode"}, "400")):
            with pytest.raises(harness.HarnessRefused, match=f"publisher_refused:{code}"):
                harness.publish_fault(**{"port": control.port, "token": token, "grant": GRANT, "kind": "stage_withdrawal",
                                         "node_id": "node-a", **kwargs})
    finally:
        control.close()
    with pytest.raises(harness.HarnessRefused, match="publisher_unreachable"):
        harness.publish_fault(port=control.port if False else 1, token="t", grant=GRANT, kind="restore", node_id="node-a")


def test_cli_refuses_without_the_grant_and_on_the_production_port_and_writes_a_private_receipt(monkeypatch, tmp_path, capsys):
    token = tmp_path / "t.token"
    token.write_text("secret\n")
    base = ["--base-url", "http://127.0.0.1:9", "--fault-port", "9001", "--token-file", str(token),
            "--node-id", "node-a", "--out-dir", str(tmp_path / "out")]
    monkeypatch.delenv(GRANT_ENV, raising=False)
    assert harness.main(base) == 2
    monkeypatch.setenv(GRANT_ENV, GRANT)
    assert harness.main([*base[:1], "http://127.0.0.1:8787", *base[2:]]) == 2
    assert harness.main([*base[:3], "8787", *base[4:]]) == 2
    assert not (tmp_path / "out").exists()
    world = World()
    sessions, _ = _sessions(world)
    from contextlib import contextmanager

    @contextmanager
    def factory(**_):
        yield sessions
    monkeypatch.setattr(harness, "publish_fault", lambda **kw: world.publish(kind=kw["kind"], node_id=kw["node_id"], value=kw["value"]))
    assert harness.main(base, sessions_factory=factory) == 0
    path = tmp_path / "out" / "30-withdrawal-browser.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    receipt = json.loads(path.read_text())
    assert receipt["passed"] is True and "secret" not in path.read_text()


def test_harness_never_runs_script_in_the_page_nor_touches_the_authority_modules():
    source = (ROOT / "scripts/run_a9_withdrawal_browser.py").read_text()
    tree = ast.parse(source)
    banned_calls = {"evaluate", "evaluate_handle", "wait_for_function", "add_init_script", "expose_function",
                    "expose_binding", "add_script_tag", "eval_on_selector", "eval_on_selector_all", "route"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in banned_calls, node.attr
        if isinstance(node, ast.keyword):
            assert node.arg != "bypass_csp"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [a.name for a in node.names] + [getattr(node, "module", None) or ""]
            assert not any("mycelium_a9_authority" in n for n in names)
    assert "bypass_csp=True" not in source


# -- real Chromium smoke (a non-product origin; no product CSP) -------------------

_PAGE = """<!doctype html><html><body><section class="panel"><h2>Durable membership and capability</h2>
<table><thead><tr>%s</tr></thead><tbody id="b"></tbody></table></section>
<script>
const body = document.getElementById('b');
const es = new EventSource('/events');
es.onmessage = (m) => { body.innerHTML = JSON.parse(m.data).map(r => '<tr><th scope="row">' + r[0] + '</th>' +
  r.slice(1).map(c => '<td>' + c + '</td>').join('') + '</tr>').join(''); };
</script></body></html>""" % "".join(f"<th>{h}</th>" for h in HEADERS)


_FETCH_PAGE = """<!doctype html><html><body><section class="panel"><h2>Durable membership and capability</h2>
<table><thead><tr>%s</tr></thead><tbody id="b"></tbody></table></section>
<script>
const body = document.getElementById('b');
const render = (rows) => { body.innerHTML = rows.map(r => '<tr><th scope="row">' + r[0] + '</th>' +
  r.slice(1).map(c => '<td>' + c + '</td>').join('') + '</tr>').join(''); };
(async () => {
  for (;;) {
    const response = await fetch('/events', {headers: {Accept: 'text/event-stream'}});
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = '';
    for (;;) {
      const {value, done} = await reader.read(); if (done) break;
      buffer += decoder.decode(value, {stream: true}); let at;
      while ((at = buffer.indexOf('\\n\\n')) >= 0) {
        const frame = buffer.slice(0, at); buffer = buffer.slice(at + 2);
        if (frame.startsWith('data: ')) render(JSON.parse(frame.slice(6)));
      }
    }
  }
})();
</script></body></html>""" % "".join(f"<th>{h}</th>" for h in HEADERS)


class _Sse(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):  # noqa: N802
        world = self.server.world
        if self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            last = None
            try:
                while not self.server.stop.is_set():
                    rows = list(_render(world.records()).values())
                    payload = json.dumps([list(r.values()) for r in rows])
                    if payload != last:
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        last = payload
                    self.server.stop.wait(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        raw = (_FETCH_PAGE if self.server.page == "fetch" else _PAGE).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


@pytest.mark.parametrize("page", ["eventsource", "fetch"])
def test_real_chromium_reads_the_table_and_collects_cdp_event_stream_messages(page):
    pytest.importorskip("playwright.sync_api")
    world = World()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Sse)
    server.world, server.stop, server.page = world, threading.Event(), page
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        try:
            manager = harness.playwright_sessions()
            sessions = manager.__enter__()
        except Exception as exc:  # noqa: BLE001
            pytest.skip(f"Chromium unavailable: {type(exc).__name__}")
        try:
            clock_publisher = world.publish
            receipt = harness.run_withdrawal(
                sessions, clock_publisher, base_url=f"http://127.0.0.1:{server.server_address[1]}", node_id="node-a",
                timeout_s=20, interval_s=0.1)
        finally:
            server.stop.set()
            manager.__exit__(None, None, None)
    finally:
        server.stop.set()
        server.shutdown()
        server.server_close()
    assert receipt["blockers"] == [], receipt
    assert receipt["passed"] is True
    assert all(o["stream_messages_after_publish"] >= 1 for s in receipt["steps"] for o in s["sessions"].values())
