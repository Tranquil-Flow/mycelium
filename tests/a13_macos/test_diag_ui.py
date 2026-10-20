# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: the Diagnostics card (index.html + diagnostics.js), run under Node with a minimal DOM stub."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

import mycelium_a13_macos as pkg

WEB = Path(pkg.__file__).parent / "web"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is required to execute diagnostics.js")

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const web = process.argv[1];
const scenario = JSON.parse(process.argv[2]);
const els = {};
function el(id) {
  return {id, textContent: '', value: '', disabled: false, hidden: false, href: '', dataset: {}, children: [],
    className: '', addEventListener() {}, replaceChildren(...c) { this.children = c; },
    append(...c) { this.children.push(...c); }, setAttribute() {}};
}
const document = {getElementById(id) { return els[id] = els[id] || el(id); }, createElement(t) { return el(t); },
  addEventListener() {}};
async function fetch(path) {
  const r = scenario.responses[path] || {ok: false, body: {error: 'x'}};
  return {ok: r.ok, async json() { return r.body; }};
}
const ctx = vm.createContext({document, window: {addEventListener() {}}, fetch, setInterval() {}, console, JSON, Date,
  Number, Math, String, Object, Array, Promise, Set, Error, isNaN, parseInt});
function text(e) { return [e.textContent, ...e.children.map(text)].join(' ').trim(); }
(async () => {
  vm.runInContext(fs.readFileSync(web + '/diagnostics.js', 'utf8'), ctx);
  await new Promise((r) => setTimeout(r, 30));
  const out = {};
  for (const id of scenario.read) { const e = document.getElementById(id); out[id] = {text: text(e), hidden: e.hidden, href: e.href}; }
  console.log(JSON.stringify(out));
})();
"""


def run(doc, read, ok=True):
    scenario = {"responses": {"/api/diagnostics": {"ok": ok, "body": doc}}, "read": read}
    done = subprocess.run([NODE, "-e", HARNESS, str(WEB), json.dumps(scenario)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def doc(**kw):
    base = {"schema": "mycelium.diagnostics.v1", "role": "owner", "generated_at": 1.0, "findings": [], "peers": [],
            "chats": {"available": True, "recent": [], "error_codes": {}},
            "relay": {"state": "private", "provider": None},
            "timings": {"available": False, "reason": "no route is open", "per_hop": {"available": False,
                        "reason": "the route records whole-request timings only"}},
            "host": {"load_average": [0.5, 0.4, 0.3], "memory_pressure": "normal", "disk_free_bytes": 50_000_000_000,
                     "spotlight_mds_cpu_percent": 1.0}, "member_view": None}
    base.update(kw)
    return base


def test_index_has_the_card_and_loads_the_script():
    html = (WEB / "index.html").read_text()
    assert '<section id="diagnostics"' in html
    assert '/diagnostics.js' in html
    assert re.search(r'<a [^>]*id="diag-bundle"[^>]*href="/api/diagnostics/bundle"', html) or \
        re.search(r'<a [^>]*href="/api/diagnostics/bundle"[^>]*id="diag-bundle"', html)
    assert "redacted" in html.lower()  # the card says what the bundle leaves out


def test_script_is_safe_and_plain():
    js = (WEB / "diagnostics.js").read_text()
    assert "innerHTML" not in js and "eval(" not in js and "document.write" not in js
    assert re.search(r"(?i)game[ -]mode", js) is None


@needs_node
def test_no_problems_is_said_plainly():
    out = run(doc(), ["diag-summary", "diag-findings"])
    assert "No problems found" in out["diag-summary"]["text"]


@needs_node
def test_findings_show_title_and_fix():
    findings = [{"id": "peer_clock_skew", "severity": "critical", "peer": "n1",
                 "title": "The joined Mac's clock is 9.2 s ahead of this Mac",
                 "detail": "Short-lived commands expire.", "fix": "Run sudo sntp -sS time.apple.com on both Macs."}]
    out = run(doc(findings=findings), ["diag-summary", "diag-findings"])
    assert "1 problem" in out["diag-summary"]["text"]
    assert "The joined Mac's clock is 9.2 s ahead of this Mac" in out["diag-findings"]["text"]
    assert "sudo sntp -sS time.apple.com" in out["diag-findings"]["text"]


@needs_node
def test_peers_and_host_and_relay_are_listed():
    peers = [{"node_id": "nodeaaaa1111", "name": "The joined Mac",
              "clock": {"available": True, "offset_s": 9.19, "direction": "ahead", "uncertainty_s": 0.1},
              "liveness": {"available": True, "label": "quarantined", "last_seen_seconds_ago": 95.0},
              "last_seen": {"membership_age_s": 1.2},
              "mailbox": {"available": True, "total": 20, "counts": {"completed": 2, "expired": 18,
                          "reconciliation_required": 0}, "median_claim_latency_s": 0.4}}]
    out = run(doc(peers=peers, relay={"state": "relay_process_running", "provider": "cloudflare",
                                      "expires_in_seconds": 600.0}),
              ["diag-peers", "diag-host", "diag-relay"])
    text = out["diag-peers"]["text"]
    assert "The joined Mac" in text and "9.2 s ahead" in text and "quarantined" in text and "18 expired" in text
    assert "disk" in out["diag-host"]["text"].lower()
    assert "cloudflare" in out["diag-relay"]["text"]


@needs_node
def test_member_view_is_shown_for_a_joined_mac():
    view = {"owner_clock": {"available": True, "offset_s": -9.19, "direction": "behind", "uncertainty_s": 0.1},
            "claim_loop": {"polls": 12, "errors": {}, "median_poll_seconds": 0.4, "median_ack_seconds": 0.3,
                           "last_command": None}}
    out = run(doc(role="target", member_view=view), ["diag-member"])
    assert not out["diag-member"]["hidden"] and "9.2 s behind" in out["diag-member"]["text"]


@needs_node
def test_a_failed_request_is_explained_not_blank():
    out = run({"error": "open_from_native_launcher"}, ["diag-summary"], ok=False)
    assert "Diagnostics unavailable" in out["diag-summary"]["text"]
