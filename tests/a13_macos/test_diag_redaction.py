# SPDX-License-Identifier: AGPL-3.0-only
"""PV26: the support bundle is redacted -- proved against seeded secrets.

Nothing a person would not paste into a public bug report may leave the Mac: no keys, grants, tokens,
join links, prompts or chat text, no home paths, no hostnames. Redaction is allow-by-construction for the
diagnostics document (it only ever holds counts, codes and timings) AND a scrubber over every string and
over the app log tail, which is free text.
"""
from __future__ import annotations

import io
import json
import socket
import time
import zipfile
from types import SimpleNamespace

import pytest

from mycelium_a13_macos.chat import ChatService
from mycelium_a13_macos.diagnostics.bundle import build_bundle
from mycelium_a13_macos.diagnostics.redact import Redactor

HOME = "/Users/seedowner"
HOSTNAME = "seeded-host-7.local"
KEY_HEX = "9f" * 32
PRIVATE_KEY = "ed25519-private:" + "A1b2C3d4" * 8
GRANT = "mycelium-grant:eyJhbGciOiJFZERTQSIsInR5cCI6IkdSQU5UIn0.eyJzY29wZSI6InN3YXJtLWRlbW8ifQ.c2VlZGVkLXNpZ25hdHVyZQ"
BEARER = "Bearer q7Zr3tKx9LmN2pVb8CdE4fGh6JkS1wYa"
CLAIM = "claim-" + "ab12" * 12
JOIN_LINK = "mycelium-join:https://quiet-river.trycloudflare.com/j/ABCD-EFGH-JKLM?k=seeded"
JOIN_CODE = "ABCD-EFGH-JKLM"
JOIN_WORDS = "quiet-river-apple-stone"
RELAY_URL = "https://quiet-river-apple-stone.trycloudflare.com"
OWN_ORIGIN = "https://demo.seeded-example.org"
PROMPT = "please summarise the confidential Acme merger memo"
REPLY = "the Acme merger closes on Tuesday"
NATIVE = "native-credential-" + "Zx9" * 10
SESSION = "session-cookie-" + "Qw4" * 10
LAUNCH = "launch-code-" + "Lm7" * 10
IPV4 = "203.0.113.77"

SECRETS = [HOME, HOSTNAME, KEY_HEX, PRIVATE_KEY, GRANT, BEARER.split()[1], CLAIM, JOIN_LINK, JOIN_CODE, JOIN_WORDS,
           RELAY_URL.split("//")[1], "demo.seeded-example.org", PROMPT, REPLY, NATIVE, SESSION, LAUNCH, IPV4,
           "seedowner"]


def log_text():
    return "\n".join([
        f"2026-10-21 12:00:00 starting in {HOME}/Library/Application Support/Mycelium A13",
        f'{{"event":"chat","prompt":"{PROMPT}","text":"{REPLY}","error_code":"backend_failed"}}',
        f"Authorization: {BEARER}",
        f"claim token {CLAIM} issued to node nodeaaaa1111",
        f"join link {JOIN_LINK} and code {JOIN_WORDS} {JOIN_CODE}",
        f"relay opened at {RELAY_URL}/chat and {OWN_ORIGIN}/managed-control/poll from {IPV4}",
        f"grant {GRANT}",
        f"signing key {PRIVATE_KEY} digest {KEY_HEX}",
        f"native {NATIVE} session {SESSION} launch {LAUNCH}",
        f"host {HOSTNAME} is up",
        "peer_liveness quarantined misses=6",
    ])


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: HOSTNAME)
    monkeypatch.setenv("HOME", HOME)
    log = tmp_path / "app.log"
    log.write_text(log_text())
    chat = ChatService(SimpleNamespace(), clock=time.monotonic)
    chat._requests["rid"] = {"id": "rid", "tokens": [REPLY, " more"], "done": True, "error": "backend_failed",
                             "terminal": "failed", "client": None, "cancel_reason": None, "max_new_tokens": 8,
                             "clamped": False, "served_by": [{"label": f"{HOSTNAME} (owner)", "node": "nodeaaaa1111",
                                                              "layers": [0, 4]}],
                             "run_id": "run-1", "started": time.monotonic() - 3, "finished": time.monotonic() - 2,
                             "prompt": PROMPT}
    chat._order.append("rid")
    return SimpleNamespace(
        installation_role="owner", root=tmp_path / "state", log_path=log, chat=chat,
        native_credential=NATIVE, session=SESSION, launch_code=LAUNCH,
        authority=SimpleNamespace(coordinator=SimpleNamespace(members=lambda: (
            {"node_id": "nodeaaaa1111", "generation": 1, "lifecycle_state": "active", "liveness_status": "idle_fresh",
             "liveness_age_seconds": 1.0, "endpoint_addrs": [f"{IPV4}:4001"], "verification_key_digest": KEY_HEX},)),
            control_mailbox=None),
        owner_run=SimpleNamespace(status=lambda: {"state": "serving", "run_id": "run-1", "participants": [
            {"label": f"{HOSTNAME} (owner)", "node_id": "nodeaaaa1111", "layers": [0, 4]}], "step": None}),
        public_relay=SimpleNamespace(status=lambda: {"state": "relay_process_running", "provider": "cloudflare",
                                                     "public_origin": RELAY_URL, "error_code": None,
                                                     "privacy": f"relay at {RELAY_URL}"}),
        origin="http://127.0.0.1:1",
    )


def read_zip(raw):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def test_no_seeded_secret_survives_anywhere_in_the_bundle(app):
    files = read_zip(build_bundle(app))
    assert {"diagnostics.json", "app-log-tail.txt", "versions.json", "README.txt"} <= set(files)
    everything = b"\n".join(files.values()).decode("utf-8", "replace") + "\n".join(files)
    leaked = [secret for secret in SECRETS if secret in everything]
    assert leaked == []


def test_the_bundle_is_still_useful_after_redaction(app):
    files = read_zip(build_bundle(app))
    doc = json.loads(files["diagnostics.json"])
    assert doc["schema"] == "mycelium.diagnostics.v1" and doc["role"] == "owner"
    log = files["app-log-tail.txt"].decode()
    assert "backend_failed" in log and "peer_liveness quarantined misses=6" in log
    assert "<HOME>" in log and "<HOST>" in log
    assert "<JOIN-LINK>" in log and "<TOKEN>" in log
    assert doc["chats"]["recent"][0]["error_code"] == "backend_failed"
    versions = json.loads(files["versions.json"])
    assert "app_version" in versions and "python" in versions


def test_chat_text_and_prompts_are_never_collected_at_all(app):
    from mycelium_a13_macos.diagnostics.collect import collect
    text = json.dumps(collect(app))
    assert PROMPT not in text and REPLY not in text
    assert "tokens" not in json.loads(text)["chats"]["recent"][0]


def test_peer_names_are_aliased_in_the_bundle(app):
    doc = json.loads(read_zip(build_bundle(app))["diagnostics.json"])
    assert [p["name"] for p in doc["peers"]] == ["Joined Mac 1"]


def test_key_names_are_redacted_whatever_the_value_looks_like():
    redactor = Redactor()
    out = redactor.json({"signing_key": "x", "access_token": "y", "grant": "g", "prompt": "p", "chat_text": "t",
                         "join_link": "l", "password": "pw", "authorization": "a", "error_code": "backend_failed",
                         "offset_s": 9.19, "nested": [{"swarm_key": "k", "state": "serving"}]})
    assert out["error_code"] == "backend_failed" and out["offset_s"] == 9.19
    assert out["nested"][0]["state"] == "serving"
    for key in ("signing_key", "access_token", "grant", "prompt", "chat_text", "join_link", "password", "authorization"):
        assert out[key] == "[redacted]", key
    assert out["nested"][0]["swarm_key"] == "[redacted]"


def test_known_secrets_are_replaced_exactly_even_when_they_look_harmless():
    redactor = Redactor(known_secrets=["plainword-secret"])
    assert "plainword-secret" not in redactor.text("value=plainword-secret;")


def test_paths_hosts_ips_and_urls():
    redactor = Redactor(home="/Users/seedowner", hostnames=["seeded-host-7.local"])
    out = redactor.text("/Users/seedowner/Library/x /Users/someoneelse/y /home/linuxer/z C:\\Users\\Winner\\w "
                        "http://127.0.0.1:50000/api https://evil.example.org/p?q=1 seeded-host-7.local 203.0.113.9")
    assert "seedowner" not in out and "someoneelse" not in out and "linuxer" not in out and "Winner" not in out
    assert "evil.example.org" not in out and "203.0.113.9" not in out and "seeded-host-7.local" not in out
    assert "<HOME>/Library/x" in out
    assert "127.0.0.1:50000" in out  # loopback is not identifying and keeps the log readable


def test_error_codes_and_numbers_are_not_mangled():
    redactor = Redactor()
    line = "chat failed code=affected_peer_quarantined offset=9.19 s ttl=4.5 sha256=short node nodeaaaa1111"
    assert redactor.text(line) == line


def test_a_missing_log_is_reported_not_faked(app, tmp_path):
    app.log_path = tmp_path / "nope.log"
    files = read_zip(build_bundle(app))
    assert b"not available" in files["app-log-tail.txt"]


def test_the_log_tail_is_bounded(app, tmp_path):
    big = tmp_path / "big.log"
    big.write_text("\n".join(f"line {i} filler filler filler" for i in range(100_000)))
    app.log_path = big
    log = read_zip(build_bundle(app))["app-log-tail.txt"]
    assert len(log) <= 80_000 and b"line 99999" in log and b"line 5 " not in log
