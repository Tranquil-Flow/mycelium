# SPDX-License-Identifier: AGPL-3.0-only
"""Swarm-creation UX copy and disabled-state hints in the macOS app UI.

app.js is run under Node with a minimal DOM stub (no browser, no network); index.html is
checked statically. Each test pins one friction point from docs/design/swarm-ux-walkthrough.md.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

import mycelium_a13_macos as pkg

WEB = Path(pkg.__file__).parent / "web"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is required to execute app.js")

HARNESS = r"""
const vm = require('vm'), fs = require('fs');
const web = process.argv[1];  // `node -e` drops the script name from argv
const scenario = JSON.parse(process.argv[2]);
const els = {}; const listeners = {};
function el(id) {
  return {id, textContent: '', value: '', checked: false, disabled: false, hidden: false, open: false,
    dataset: {}, style: {}, max: '', children: [], className: '',
    addEventListener(type) { (listeners[id] = listeners[id] || []).push(type); },
    replaceChildren(...c) { this.children = c; }, append(...c) { this.children.push(...c); },
    insertRow() { return {insertCell() { return el('cell'); }, append() {}}; }, setAttribute() {}};
}
const document = {getElementById(id) { return els[id] = els[id] || el(id); }, createElement(t) { return el(t); },
  querySelectorAll() { return []; }, addEventListener() {}};
const window = {addEventListener() {}, confirm() { return true; }};
const responses = scenario.responses || {};
async function fetch(path) {
  const r = responses[path] || {ok: false, body: {error: 'x'}};
  return {ok: r.ok, async json() { return r.body; }};
}
const ctx = vm.createContext({document, window, fetch, setInterval() {}, console, JSON, Date, Number, Math, String,
  Object, Array, Promise, Set, Error, Symbol, isNaN, parseInt});
(async () => {
  vm.runInContext(fs.readFileSync(web + '/app.js', 'utf8'), ctx);
  await new Promise((r) => setTimeout(r, 20));
  for (const [id, props] of Object.entries(scenario.pre || {})) Object.assign(document.getElementById(id), props);
  const out = {};
  if (scenario.status) {
    ctx.__status = scenario.status;
    vm.runInContext('render(__status)', ctx);
  }
  if (scenario.perform) {
    ctx.__p = scenario.perform;
    await vm.runInContext('perform(__p.path, {}, "ok")', ctx);
  }
  out.text = {}; out.disabled = {}; out.hidden = {};
  for (const id of scenario.read || []) {
    const e = document.getElementById(id);
    out.text[id] = e.textContent; out.disabled[id] = e.disabled; out.hidden[id] = e.hidden;
  }
  out.listeners = listeners;
  console.log(JSON.stringify(out));
})();
"""


def run_ui(scenario):
    done = subprocess.run([NODE, "-e", HARNESS, str(WEB), json.dumps(scenario)], capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout.strip().splitlines()[-1])


def status(*, role="owner", created=False, member="none", label="Qwen3-14B", origin=None, acquisition=None,
           policy=True):
    selected = {"model_id": "Qwen/" + label, "label": label, "snapshot_bytes": 15_242_807_270,
                "owner_storage_bytes": 23_052_975_062, "whole_model_member_disk_bytes": 11_297_336_366}
    return {
        "installation_role": role, "running": True,
        "authority": {"membership_state": member, "swarm_created": created, "members": [],
                      "resource_use_acknowledged": created},
        "model_contract": None,
        "resource_policy": {"power": "ac_only", "network": "unmetered_only", "storage_bytes": 23_052_975_062}
        if policy else None,
        "serving": {"state": "not_serving", "next_action": ""}, "role": "not_serving",
        "acquisition": acquisition, "resource_observation": None, "join_flow": {},
        "network": {"state": "private", "public_origin": origin},
        "run_model": {"resources": {"selected": selected, "models": [], "storage_consent_ceiling_bytes": 23_052_975_062,
                                    "pairing_quota_ceiling_bytes": 11_297_336_366,
                                    "device_memory_bytes": 17_179_869_184, "memory_cap_options": []}},
    }


def html():
    return (WEB / "index.html").read_text()


# -- (2) first-model download: copy must match the acquisition code path ---------------------

def test_static_acquisition_copy_does_not_claim_local_seed_only():
    text = html().split('id="acquisition-detail">')[1].split("</p>")[0]
    assert "Hugging Face" in text
    assert "refuses instead of downloading" not in text and "only ever fetches" not in text


@needs_node
def test_default_model_copy_says_download_comes_from_hugging_face():
    out = run_ui({"status": status(label="Qwen3-14B"), "read": ["acquisition-detail"]})
    text = out["text"]["acquisition-detail"]
    assert "Hugging Face" in text and "Download / resume source" in text


@needs_node
def test_seeded_7b_copy_explains_seed_and_how_to_download_instead():
    out = run_ui({"status": status(label="Qwen2.5-7B"), "read": ["acquisition-detail"]})
    text = out["text"]["acquisition-detail"]
    assert "Hugging Face" in text            # the code does download from the pinned origin
    assert "refuses instead of downloading" not in text
    assert "15,242,807,270" in text          # tells the user how large the full download is
    assert "raise" in text.lower()           # ...and that the byte limit must be raised to allow it


def test_run_hint_to_download_names_the_authorization_step():
    run_js = (WEB / "run.js").read_text()
    assert "tick the authorization box" in run_js


# -- (3a) raw error codes become sentences ---------------------------------------------------

@needs_node
def test_action_error_notice_is_a_sentence_with_next_step():
    out = run_ui({"status": status(), "perform": {"path": "/api/model/download"},
                  "responses": {"/api/model/download": {"ok": False, "body": {"error": "membership_required"}}},
                  "read": ["notice"]})
    notice = out["text"]["notice"]
    assert "Create or join a swarm first" in notice and "membership required" not in notice


@needs_node
def test_unknown_action_error_still_reports_the_code():
    out = run_ui({"status": status(), "perform": {"path": "/api/x"},
                  "responses": {"/api/x": {"ok": False, "body": {"error": "some_new_code"}}}, "read": ["notice"]})
    assert "some new code" in out["text"]["notice"]


@needs_node
@pytest.mark.parametrize("code,needle", [
    ("download_limit_exceeded", "Raise"),
    ("storage_limit_exceeded", "larger storage limit"),
    ("insufficient_disk", "free disk"),
    ("network_error", "internet"),
    ("resource_power_policy_violation", "plug in"),
])
def test_acquisition_failure_state_has_a_human_sentence(code, needle):
    acq = {"state": "failed", "error_code": code, "downloaded_bytes": 0, "total_bytes": 10}
    out = run_ui({"status": status(member="current", acquisition=acq), "read": ["acquisition-state"]})
    assert needle.lower() in out["text"]["acquisition-state"].lower()


@pytest.mark.parametrize("name", ["run.js", "swarm.js"])
def test_unknown_error_fallback_is_not_a_bare_code(name):
    js = (WEB / name).read_text()
    assert "Action not completed" in js


# -- (3b) disabled buttons say why ------------------------------------------------------------

@needs_node
@pytest.mark.parametrize("kw,needle", [
    (dict(member="none"), "Create or join a swarm first"),
    (dict(member="current"), "authorization box"),
])
def test_download_button_hint(kw, needle):
    out = run_ui({"status": status(**kw), "read": ["download-hint", "download-weights"]})
    assert out["disabled"]["download-weights"] is True
    assert needle in out["text"]["download-hint"]


@needs_node
def test_download_hint_clears_once_download_is_possible():
    out = run_ui({"pre": {"download-consent": {"checked": True}}, "status": status(member="current"),
                  "read": ["download-hint", "download-weights"]})
    assert out["disabled"]["download-weights"] is False and out["text"]["download-hint"] == ""


@needs_node
@pytest.mark.parametrize("kw,needle", [
    (dict(created=False, origin=None), "Create the swarm"),
    (dict(created=False, origin="https://x.example"), "Create the swarm"),
])
def test_invite_button_hint(kw, needle):
    out = run_ui({"status": status(**kw), "read": ["invite-hint", "window-open"]})
    assert out["disabled"]["window-open"] is True
    assert needle in out["text"]["invite-hint"]


@needs_node
def test_invite_available_before_the_relay_with_a_ready_once_relay_starts_hint():
    out = run_ui({"status": status(created=True, member="current", origin=None),
                  "read": ["invite-hint", "window-open"]})
    assert out["disabled"]["window-open"] is False  # codes can be asked for any time
    assert "once the relay starts" in out["text"]["invite-hint"]


@needs_node
def test_invite_hint_empty_when_invite_available():
    out = run_ui({"status": status(created=True, member="current", origin="https://x.example"),
                  "read": ["invite-hint", "window-open"]})
    assert out["disabled"]["window-open"] is False and out["text"]["invite-hint"] == ""


@needs_node
@pytest.mark.parametrize("pre,needle", [
    ({}, "visibility"),
    ({"visibility": {"checked": True}}, "standing"),
])
def test_join_with_code_hint_names_the_missing_box(pre, needle):
    out = run_ui({"pre": pre, "status": status(role="target", member="none"), "read": ["join-hint", "join-with-code"]})
    assert out["disabled"]["join-with-code"] is True
    assert needle in out["text"]["join-hint"]


@needs_node
def test_join_hint_updates_when_the_visibility_box_changes():
    out = run_ui({"read": []})
    assert "change" in out["listeners"].get("visibility", [])


@needs_node
def test_joined_mac_is_told_how_to_leave():
    out = run_ui({"status": status(role="target", member="current"), "read": ["leave-hint"]})
    text = out["text"]["leave-hint"]
    # Owner ruling R24/R25 (2026-10-20): a member leaves with the Leave swarm control (smoothly or instantly).
    assert "Leave swarm" in text and "Leave smoothly" in text and "Instant leave" in text and "owner" in text


@needs_node
def test_owner_has_no_leave_hint():
    out = run_ui({"status": status(role="owner", created=True, member="current"), "read": ["leave-hint"]})
    assert out["text"]["leave-hint"] == ""


def test_hint_elements_exist_in_index():
    page = html()
    for ident in ("download-hint", "invite-hint", "join-hint", "leave-hint"):
        assert f'id="{ident}"' in page
