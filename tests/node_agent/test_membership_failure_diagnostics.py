# SPDX-License-Identifier: AGPL-3.0-only
"""Effect-sandboxed entrypoint diagnostics: no real state, seed, signer or child."""
from __future__ import annotations

from contextlib import nullcontext
import importlib
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

node = importlib.import_module("mycelium_node.__main__")


@pytest.fixture
def sandbox(monkeypatch):
    effects = []

    class State:
        path = Path("/fake/private/state")
        def working_directory(self):
            effects.append("cwd")
            return nullcontext()
        def revalidate(self):
            effects.append("validate")
        def close(self):
            effects.append("close")

    state = State()
    def preflight(_args):
        effects.append("preflight")
        return state, {"token": "fake"}, {"payload": {"swarm_id": "fake", "seed_url": "fake", "nonce": "fake"}, "seed_key_records": [{}]}, client, None, None, None

    client = SimpleNamespace(rotation=lambda **kw: None, identity=lambda **kw: {"seed_node_id": "fake"})
    signer = SimpleNamespace(endpoint_id="fake")
    class Session:
        generation = 1
        seed_key_digest = "fake"
        lease_expires_at = 9999999999.0
        def __init__(self, **kw):
            effects.append("session")
        def join_request(self, **kw):
            effects.append("request")
            return {}
        def accept_join(self, *a, **kw):
            effects.append("accept")
    monkeypatch.setattr(node, "_preflight", preflight)
    monkeypatch.setattr(node, "load_or_create_node_signer", lambda *a, **kw: signer)
    monkeypatch.setattr(node, "NodeMembershipSession", Session)
    monkeypatch.setattr(node, "load_v2_profile_template", lambda *a: None)
    monkeypatch.setattr(node, "save_membership_state", lambda *a: effects.append("save"))
    monkeypatch.setattr(node, "_renew_lease", lambda **kw: False)
    monkeypatch.setattr(node.signal, "signal", lambda *a: None)
    monkeypatch.setattr(node, "_emit_status", lambda *a: effects.append("status"))
    client.join = lambda **kw: {"verification_key": {}}
    client.accepted_seed_key_digest = lambda *a, **kw: "fake"
    return effects, client, state


def args(path=None):
    result = ["--data-dir", "/fake/state", "--node-id", "fake", "--advertise", "https://fake.test/control", "--membership-control-only"]
    if path is not None:
        result += ["--failure-diagnostics", str(path)]
    return result


def private_parent(tmp_path):
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    return parent


def record(path):
    metadata = path.stat()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    raw = path.read_bytes()
    assert len(raw.splitlines()) == 1
    return json.loads(raw)


def test_default_effect_order_unchanged_and_no_diagnostic(sandbox):
    effects, client, _ = sandbox
    client.identity = lambda **kw: (_ for _ in ()).throw(ValueError("private-canary"))
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args())
    assert (caught.value.code, caught.value.exit_status) == ("node_runtime_failed", 4)
    assert effects == ["preflight", "cwd", "close"]


@pytest.mark.parametrize("kind", ["existing", "symlink", "public-parent", "relative", "traversal", "directory", "control-character"])
def test_invalid_sink_refuses_before_effects(tmp_path, sandbox, kind):
    effects, _, _ = sandbox
    parent = private_parent(tmp_path)
    path = parent / "diagnostic.jsonl"
    if kind == "existing":
        path.write_text("canary")
    elif kind == "symlink":
        path.symlink_to(parent / "victim")
    elif kind == "public-parent":
        parent.chmod(0o755)
    elif kind == "relative":
        path = Path("relative.jsonl")
    elif kind == "traversal":
        path = parent / ".." / "diagnostic.jsonl"
    elif kind == "directory":
        path.mkdir()
    elif kind == "control-character":
        path = parent / "bad\nname.jsonl"
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args(path))
    assert (caught.value.code, caught.value.exit_status) == ("node_preflight_failed", 2)
    assert effects == []
    if kind == "existing":
        assert path.read_text() == "canary"


@pytest.mark.parametrize("phase,inject,expected", [
    ("initial_identity", "identity", "ValueError"),
    ("local_setup", "signer", "OSError"),
    ("membership_exchange", "join", "SeedHTTPError"),
    ("lease_renewal", "renew", "RuntimeError"),
])
def test_main_path_failure_is_bounded_private_and_secret_free(tmp_path, sandbox, monkeypatch, phase, inject, expected):
    effects, client, _ = sandbox
    canary = "secret-canary-url-key-envelope"
    if inject == "identity":
        client.identity = lambda **kw: (_ for _ in ()).throw(ValueError(canary))
    elif inject == "signer":
        monkeypatch.setattr(node, "load_or_create_node_signer", lambda *a, **kw: (_ for _ in ()).throw(OSError(canary)))
    elif inject == "join":
        client.join = lambda **kw: (_ for _ in ()).throw(node.SeedHTTPError("invite_replayed", status=409))
    else:
        monkeypatch.setattr(node, "_renew_lease", lambda **kw: (_ for _ in ()).throw(RuntimeError(canary)))
    path = private_parent(tmp_path) / "diagnostic.jsonl"
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args(path))
    data = record(path)
    assert caught.value.exit_status == (3 if inject == "join" else 2 if inject == "signer" else 4)
    assert data["protocol"] == "mycelium.node_private_failure.v1"
    assert data["phase"] == phase
    assert data["exception_class"] == expected
    assert set(data) <= {"protocol", "phase", "exception_class", "seed_code", "http_status"}
    if inject == "join":
        assert data["seed_code"] == "invite_replayed"
        assert data["http_status"] == 409
    assert canary not in path.read_text()
    assert effects[0] == "preflight"


def test_unknown_remote_code_and_invalid_status_do_not_escape(tmp_path, sandbox):
    _, client, _ = sandbox
    client.join = lambda **kw: (_ for _ in ()).throw(node.SeedHTTPError("secret_canary_url_key", status=True))
    path = private_parent(tmp_path) / "diagnostic.jsonl"
    with pytest.raises(node._EntrypointFailure):
        node.run(args(path))
    data = record(path)
    assert data["exception_class"] == "SeedHTTPError"
    assert "seed_code" not in data and "http_status" not in data
    assert "secret_canary" not in path.read_text()


def test_success_leaves_empty_file_and_same_effect_order(tmp_path, sandbox):
    effects, _, _ = sandbox
    assert node.run(args()) == 0
    default_effects = effects.copy()
    effects.clear()
    path = private_parent(tmp_path) / "diagnostic.jsonl"
    result = node.run(args(path))
    assert result == 0
    assert path.read_bytes() == b""
    assert effects == default_effects == ["preflight", "cwd", "validate", "validate", "session", "request", "accept", "save", "validate", "close"]


def test_preflight_failure_and_state_scope_failure(tmp_path, sandbox, monkeypatch):
    effects, _, state = sandbox
    def fail_preflight(_args):
        effects.append("preflight")
        raise node._diagnosed(node._EntrypointFailure("node_preflight_failed", 2),
                             "preflight", ValueError("private-canary"))
    monkeypatch.setattr(node, "_preflight", fail_preflight)
    target = private_parent(tmp_path) / "preflight.jsonl"
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args(target))
    assert caught.value.exit_status == 2
    assert record(target)["phase"] == "preflight"
    assert effects == ["preflight"]

    # Restore only a fully fake preflight; entering the fake cwd fails before
    # identity, membership, or any other external effect is reachable.
    def scope_failure(_args):
        effects.append("preflight")
        return state, {}, {}, None, None, None, None
    class BadScope:
        def __enter__(self):
            raise OSError("private-canary")
        def __exit__(self, *_):
            return False
    state.working_directory = lambda: BadScope()
    monkeypatch.setattr(node, "_preflight", scope_failure)
    target = target.with_name("scope.jsonl")
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args(target))
    assert caught.value.exit_status == 4
    assert record(target)["phase"] == "state_scope"
    assert effects == ["preflight", "preflight", "close"]


def test_sink_write_failure_does_not_replace_primary(tmp_path, sandbox, monkeypatch):
    _, client, _ = sandbox
    client.identity = lambda **kw: (_ for _ in ()).throw(ValueError("private-canary"))
    def broken_write(*_):
        raise OSError("sink broken")
    monkeypatch.setattr(node, "write_failure", broken_write)
    target = private_parent(tmp_path) / "broken.jsonl"
    with pytest.raises(node._EntrypointFailure) as caught:
        node.run(args(target))
    assert (caught.value.code, caught.value.exit_status) == ("node_runtime_failed", 4)
    assert target.read_bytes() == b""


def test_unknown_exception_class_is_fixed_label(tmp_path, sandbox):
    _, client, _ = sandbox
    class PrivateCanaryError(Exception):
        pass
    client.identity = lambda **kw: (_ for _ in ()).throw(PrivateCanaryError("private-canary"))
    target = private_parent(tmp_path) / "class.jsonl"
    with pytest.raises(node._EntrypointFailure):
        node.run(args(target))
    assert record(target)["exception_class"] == "Other"
    assert "private-canary" not in target.read_text()


@pytest.mark.skipif(not Path("/dev/fd").is_dir(), reason="POSIX fd inventory")
def test_sink_descriptor_closed_on_success_and_refusal(tmp_path, sandbox):
    parent = private_parent(tmp_path)
    before = set(os.listdir("/dev/fd"))
    assert node.run(args(parent / "success.jsonl")) == 0
    with pytest.raises(node._EntrypointFailure):
        node.run(args(parent / "success.jsonl"))
    assert set(os.listdir("/dev/fd")) == before
