# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-side managed native hello observation producer.

Two honest layers, clearly labeled:

- REAL: actual loopback signed transport (authority TLS + owner client + native
  worker subprocess) for the positive single-member identity observation and
  for capturing the genuine native hello envelope. Never inference; never a
  claim of physical/full A13 acceptance.
- CONFORMANCE: narrow labeled fakes injected ONLY at the session boundary via
  monkeypatch of ``ManagedSessionFactory.__call__`` (production itself never
  accepts an arbitrary factory). They pin producer-side rejections for shapes
  the local single-member world cannot produce (two nodes, duplicate hosts).

Retrospective checks inherited from the previous draft (argument validation,
rejection matrix core) are labeled ``retrospective``; everything else is new.
"""
from __future__ import annotations

import hashlib
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import pytest

from test_control_pump import _world
from test_control_session import owner
from test_managed_control import DEPLOYMENT_ID
from test_managed_runtime import runtime as _runtime
from mycelium_a13_macos.managed_control import CONTROL_COMMAND_PROTOCOL
from mycelium_a13_macos.managed_observations import (
    ManagedObservationsError,
    collect_managed_hello_observations,
)
from mycelium_a13_macos.session_factory import ManagedSessionFactory
from mycelium_physical_runner.remote_probe import derive_local_run_scoped_identity

# Pinned clock for fixed-date tests: producer clock AND builder clock must both
# be pinned to the run timestamp (never a fixed historical date + real clock).
PINNED_RUN_ID = "macdemo-qwen-20260913T090000Z"
PINNED_NOW = datetime(2026, 9, 13, 9, 0, tzinfo=timezone.utc).timestamp()
_STALE_RUN_ID = "macdemo-qwen-20200101T000000Z"


def _fresh_run_id() -> str:
    """Builder-contract run id whose timestamp is current wall time."""
    return "macdemo-qwen-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@contextmanager
def _owner_world(tmp_path) -> Iterator[tuple[dict[str, Any], Any]]:
    with _world(tmp_path / "world") as world:
        managed = owner(tmp_path / "owner", world)
        try:
            yield world, managed
        finally:
            managed.close()


def _factory(world, managed, run_id=PINNED_RUN_ID):
    member = world["authority"].coordinator.member(world["member"]["node_id"])
    return ManagedSessionFactory(
        owner=managed, run_id=run_id, deployment_id=DEPLOYMENT_ID, members=[member]
    )


# --- labeled conformance fakes (session boundary only) ------------------------


def _result(
    node_id: str,
    *,
    host: str | None = None,
    boot: str | None = None,
    run_id: str = PINNED_RUN_ID,
    deployment_id: str = DEPLOYMENT_ID,
    state: str = "NEW",
) -> dict[str, Any]:
    host = host or ("host-" + hashlib.sha256(node_id.encode()).hexdigest()[:32])
    boot = boot or ("boot-" + hashlib.sha256(("boot:" + node_id).encode()).hexdigest()[:32])
    return {
        "protocol": CONTROL_COMMAND_PROTOCOL,
        "run_id": run_id,
        "deployment_id": deployment_id,
        "node_id": node_id,
        "host_id": host,
        "boot_id": boot,
        "process_id": 4242,
        "endpoint_id": "endpoint-x",
        "peer_generation": 0,
        "state": state,
        "route_ready": False,
    }


def _envelope(result: dict[str, Any], *, ok: bool = True, node_id: str | None = None,
              error: dict[str, Any] | None = None) -> dict[str, Any]:
    envelope: dict[str, Any] = {
        "protocol": CONTROL_COMMAND_PROTOCOL,
        "command_id": None,  # echoed per send unless replay mode
        "node_id": node_id if node_id is not None else result["node_id"],
        "ok": ok,
        "route_ready": False,
    }
    envelope["result" if ok else "error"] = result if ok else (error or {"code": "invalid_state"})
    return envelope


class _FakeSession:
    """Replays one envelope; echoes the generated command_id unless replay=True."""

    def __init__(self, envelope: dict[str, Any], *, replay: bool = False) -> None:
        self._envelope = envelope
        self._replay = replay
        self.closed = False
        self.stop_response: dict[str, Any] | None = None
        self.cleanup_error: Any = None
        self.close_error: Any = None

    def send(self, *, command_id: str, command: str, payload: dict[str, Any]):
        assert command == "hello" and payload == {}
        envelope = dict(self._envelope)
        if not self._replay and envelope["command_id"] is None:
            envelope["command_id"] = command_id
        return envelope

    def close(self):
        self.closed = True
        if self.close_error is not None:
            raise self.close_error
        if self.cleanup_error is None and self.stop_response is None:
            self.stop_response = {"protocol": CONTROL_COMMAND_PROTOCOL, "ok": True}


def _patch_session_provider(monkeypatch, provider) -> list[_FakeSession]:
    created: list[_FakeSession] = []

    def fake_call(self, *, argv, node_id, run_id, deployment_id, timeout_seconds):
        assert argv == () and run_id == self.run_id and deployment_id == self.deployment_id
        session = provider(node_id)
        created.append(session)
        return session

    monkeypatch.setattr(ManagedSessionFactory, "__call__", fake_call)
    return created


def _patch_replies(monkeypatch, envelopes_by_node: dict[str, dict[str, Any]]) -> list[_FakeSession]:
    return _patch_session_provider(
        monkeypatch, lambda node_id: _FakeSession(envelopes_by_node[node_id])
    )


def _collect(factory, *, node_ids, run_id=PINNED_RUN_ID, clock=lambda: PINNED_NOW):
    return collect_managed_hello_observations(
        factory, run_id=run_id, deployment_id=DEPLOYMENT_ID, node_ids=node_ids, clock=clock
    )


# --- argument validation (retrospective: inherited from previous draft) -------


def test_rejects_invalid_run_id_before_any_session(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        ManagedSessionFactory, "__call__",
        lambda self, **kwargs: calls.append(kwargs),
    )
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed)
        for bad in ("nope", "macdemo-qwen-20990101T000000Z-x", "", None, 7,
                    "macdemo-qwen-20260913T090000z"):  # lowercase z
            with pytest.raises(ManagedObservationsError, match="run_id_invalid"):
                _collect(factory, node_ids=(world["member"]["node_id"],), run_id=bad)
    assert calls == []


def test_rejects_stale_run_id_before_any_session(tmp_path, monkeypatch):
    # Historical run stamp with the REAL clock: deterministically stale.
    calls = []
    monkeypatch.setattr(
        ManagedSessionFactory, "__call__",
        lambda self, **kwargs: calls.append(kwargs),
    )
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed, run_id=_STALE_RUN_ID)
        with pytest.raises(ManagedObservationsError, match="run_id_stale"):
            _collect(factory, node_ids=(world["member"]["node_id"],),
                     run_id=_STALE_RUN_ID, clock=time.time)
    assert calls == []


def test_rejects_node_set_not_matching_current_enrollment(tmp_path):
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed)
        node = world["member"]["node_id"]
        for nodes in ((), ("unbound-node",), (node, node), (node, "other")):
            with pytest.raises(ManagedObservationsError):
                _collect(factory, node_ids=nodes)
        with pytest.raises(ManagedObservationsError, match="nodes_invalid"):
            _collect(factory, node_ids=(7,))
        with pytest.raises(ManagedObservationsError, match="deployment_invalid"):
            collect_managed_hello_observations(
                factory, run_id=PINNED_RUN_ID, deployment_id="",
                node_ids=(node,), clock=lambda: PINNED_NOW,
            )


def test_rejects_caller_supplied_host_boot_as_observations(tmp_path):
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed)
        with pytest.raises(TypeError):
            collect_managed_hello_observations(
                factory,
                run_id=PINNED_RUN_ID,
                deployment_id=DEPLOYMENT_ID,
                node_ids=(world["member"]["node_id"],),
                host_id="host-" + "0" * 32,
                boot_id="boot-" + "0" * 32,
            )


# --- NEW: production factory type/scope enforcement ---------------------------


def test_rejects_arbitrary_factory_objects(tmp_path):
    class _ArbitraryFactory:
        def __init__(self):
            self.run_id = PINNED_RUN_ID
            self.deployment_id = DEPLOYMENT_ID
            self._bindings = {"node-0": object()}
            self.owner = object()

        def __call__(self, **kwargs):
            raise AssertionError("must never be called")

    for fake in (_ArbitraryFactory(), object(), None):
        with pytest.raises(ManagedObservationsError, match="factory_invalid"):
            collect_managed_hello_observations(
                fake, run_id=PINNED_RUN_ID, deployment_id=DEPLOYMENT_ID,
                node_ids=("node-0",), clock=lambda: PINNED_NOW,
            )


def test_rejects_factory_scope_mismatch(tmp_path):
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed, run_id=_fresh_run_id())
        other = "macdemo2-qwen-" + _fresh_run_id().rsplit("-qwen-", 1)[1]
        with pytest.raises(ManagedObservationsError, match="factory_scope_mismatch"):
            _collect(factory, node_ids=(world["member"]["node_id"],), run_id=other)
        with pytest.raises(ManagedObservationsError, match="factory_scope_mismatch"):
            collect_managed_hello_observations(
                factory, run_id=factory.run_id, deployment_id="other-deployment",
                node_ids=(world["member"]["node_id"],), clock=lambda: PINNED_NOW,
            )


# --- REAL signed transport (positive + captured-envelope negative fake) -------


def test_real_signed_transport_positive_single_member(tmp_path):
    """Real TLS owner session -> real native hello (model-free) -> document.

    One real current member is permitted; both host and boot identities come
    from the native reply and must equal the independently derived run-scoped
    identity. The document must also be admitted by the actual builder
    validator with the real clock.
    """
    run_id = _fresh_run_id()
    with _world(tmp_path / "world") as world:
        node = world["member"]["node_id"]
        runtime = _runtime(tmp_path / "rt", world, run_id=run_id)
        runtime.start()
        managed = None
        try:
            managed = owner(tmp_path / "owner", world)
            factory = _factory(world, managed, run_id=run_id)
            document = collect_managed_hello_observations(
                factory, run_id=run_id, deployment_id=DEPLOYMENT_ID, node_ids=(node,)
            )
            assert document["protocol"] == "mycelium.managed_hello_observations.v1"
            assert document["run_id"] == run_id
            assert document["deployment_id"] == DEPLOYMENT_ID
            assert time.time() - document["captured_at_unix_ms"] / 1000 < 60
            [observed] = document["observations"]
            assert observed["node_id"] == node
            expected_host, expected_boot = derive_local_run_scoped_identity(run_id)
            assert observed["host_id"] == expected_host
            assert observed["boot_id"] == expected_boot
            assert re.fullmatch(r"host-[0-9a-f]{32}", observed["host_id"])
            assert re.fullmatch(r"boot-[0-9a-f]{32}", observed["boot_id"])

            from scripts import build_qwen_live_route as builder

            admitted, observations = builder._managed_hello_observations(
                document, (node,), deployment_id=DEPLOYMENT_ID
            )
            assert admitted == run_id
            assert observations == {node: (expected_host, expected_boot)}
        finally:
            runtime.close()
            if managed is not None:
                managed.close()


def test_real_reply_without_boot_id_fails_closed_via_labeled_fake(tmp_path, monkeypatch):
    """Capture the genuine native hello, strip boot_id, replay via labeled fake.

    Pins both facts: the real native hello now exposes boot_id, and the
    producer still fails closed when it is missing (never fabricates it).
    """
    run_id = _fresh_run_id()
    with _world(tmp_path / "world") as world:
        node = world["member"]["node_id"]
        runtime = _runtime(tmp_path / "rt", world, run_id=run_id)
        runtime.start()
        managed = None
        try:
            managed = owner(tmp_path / "owner", world)
            real = managed.session(
                node_id=node, run_id=run_id, deployment_id=DEPLOYMENT_ID, timeout_seconds=10
            )
            reply = real.send(command_id="capture-real-hello", command="hello", payload={})
            real.close()
            assert reply["ok"] is True
            assert re.fullmatch(r"boot-[0-9a-f]{32}", reply["result"]["boot_id"])
            stripped = {k: v for k, v in reply["result"].items() if k != "boot_id"}
            factory = _factory(world, managed, run_id=run_id)
            _patch_replies(monkeypatch, {node: _envelope(stripped)})
            with pytest.raises(ManagedObservationsError, match="boot_identity_missing"):
                collect_managed_hello_observations(
                    factory, run_id=run_id, deployment_id=DEPLOYMENT_ID, node_ids=(node,)
                )
        finally:
            runtime.close()
            if managed is not None:
                managed.close()


# --- conformance rejections (session-boundary fakes; retrospective core) ------


@pytest.mark.parametrize(
    "envelope,match",
    [
        # failed native response (retrospective)
        (_envelope(_result("node-0"), ok=False, error={"code": "invalid_state_for_start"}),
         "response_failed"),
        # malformed envelope: wrong node (retrospective)
        (_envelope(_result("node-0"), node_id="other-node"), "response_malformed"),
        # identity mismatch: wrong run (retrospective)
        (_envelope(_result("node-0", run_id="other-run")), "identity_mismatch"),
        # identity mismatch: wrong deployment (retrospective)
        (_envelope(_result("node-0", deployment_id="other")), "identity_mismatch"),
        # stale state, not NEW (retrospective)
        (_envelope(_result("node-0", state="RUNNING")), "identity_mismatch"),
        # synthetic host alias (retrospective)
        (_envelope(_result("node-0", host="macbook-pro.local")), "identity_synthetic"),
        # swapped prefixes (retrospective)
        (_envelope(_result(
            "node-0",
            host="boot-" + hashlib.sha256(b"x").hexdigest()[:32],
            boot="host-" + hashlib.sha256(b"x").hexdigest()[:32],
        )), "identity_synthetic"),
        # missing boot identity (retrospective shape; real-path variant above)
        (_envelope({k: v for k, v in _result("node-0").items() if k != "boot_id"}),
         "boot_identity_missing"),
    ],
)
def test_conformance_rejection_matrix(tmp_path, monkeypatch, envelope, match):
    with _owner_world(tmp_path) as (world, managed):
        node = world["member"]["node_id"]
        factory = _factory(world, managed)
        import copy
        envelope = copy.deepcopy(envelope)
        # Parametrized node-0 is a fixture placeholder, not this enrolled key.
        if envelope['node_id'] == 'node-0':
            envelope['node_id'] = node
        if isinstance(envelope.get('result'), dict):
            envelope['result']['node_id'] = node
        _patch_replies(monkeypatch, {node: envelope})
        with pytest.raises(ManagedObservationsError, match=match):
            _collect(factory, node_ids=(node,))


def test_conformance_command_id_must_be_echoed(tmp_path, monkeypatch):
    with _owner_world(tmp_path) as (world, managed):
        node = world["member"]["node_id"]
        factory = _factory(world, managed)
        wrong = _envelope(_result(node))
        wrong["command_id"] = "someone-elses-command"
        _patch_replies(monkeypatch, {node: wrong})
        with pytest.raises(ManagedObservationsError, match="response_malformed"):
            _collect(factory, node_ids=(node,))


def test_conformance_rejects_duplicate_hosts_and_boots(tmp_path, monkeypatch):
    with _owner_world(tmp_path) as (world, managed):
        real = world["authority"].coordinator.member(world["member"]["node_id"])
        other = dict(real)
        other["node_id"] = "node-conformance-b"
        nodes = (real["node_id"], other["node_id"])
        factory = ManagedSessionFactory(
            owner=managed, run_id=PINNED_RUN_ID, deployment_id=DEPLOYMENT_ID,
            members=[real, other],
        )
        members = {real["node_id"]: real, other["node_id"]: other}
        monkeypatch.setattr(factory.owner, "current_member", lambda node_id: members[node_id])

        shared_host = "host-" + "ab" * 16
        _patch_replies(monkeypatch, {
            nodes[0]: _envelope(_result(nodes[0], host=shared_host)),
            nodes[1]: _envelope(_result(nodes[1], host=shared_host)),
        })
        with pytest.raises(ManagedObservationsError, match="hosts_not_distinct"):
            _collect(factory, node_ids=nodes)

        shared_boot = "boot-" + "cd" * 16
        _patch_replies(monkeypatch, {
            nodes[0]: _envelope(_result(nodes[0], boot=shared_boot)),
            nodes[1]: _envelope(_result(nodes[1], boot=shared_boot)),
        })
        with pytest.raises(ManagedObservationsError, match="boots_not_distinct"):
            _collect(factory, node_ids=nodes)


def test_conformance_document_satisfies_builder_contract(tmp_path, monkeypatch):
    from scripts import build_qwen_live_route as builder

    from types import SimpleNamespace
    monkeypatch.setattr(builder, "time", SimpleNamespace(time=lambda: PINNED_NOW))
    with _owner_world(tmp_path) as (world, managed):
        real = world["authority"].coordinator.member(world["member"]["node_id"])
        other = dict(real)
        other["node_id"] = "node-conformance-b"
        nodes = (real["node_id"], other["node_id"])
        factory = ManagedSessionFactory(
            owner=managed, run_id=PINNED_RUN_ID, deployment_id=DEPLOYMENT_ID,
            members=[real, other],
        )
        members = {real["node_id"]: real, other["node_id"]: other}
        monkeypatch.setattr(factory.owner, "current_member", lambda node_id: members[node_id])
        sessions = _patch_replies(monkeypatch, {
            node: _envelope(_result(node)) for node in nodes
        })
        document = _collect(factory, node_ids=nodes)
        assert document["captured_at_unix_ms"] == int(PINNED_NOW * 1000)
        admitted, observations = builder._managed_hello_observations(
            document, nodes, deployment_id=DEPLOYMENT_ID
        )
        assert admitted == PINNED_RUN_ID
        assert observations == {
            node: (
                "host-" + hashlib.sha256(node.encode()).hexdigest()[:32],
                "boot-" + hashlib.sha256(("boot:" + node).encode()).hexdigest()[:32],
            )
            for node in nodes
        }
        assert all(session.closed for session in sessions)


# --- NEW: fail-closed stop/close, membership recheck, clock discipline ---------


@pytest.mark.parametrize("mode", ["raise", "stop_rejected", "cleanup_error"])
def test_close_failure_fails_closed_preserving_error(tmp_path, monkeypatch, mode):
    with _owner_world(tmp_path) as (world, managed):
        node = world["member"]["node_id"]
        factory = _factory(world, managed)
        session = _FakeSession(_envelope(_result(node)))
        if mode == "raise":
            session.close_error = RuntimeError("stop pipe broke")
        elif mode == "stop_rejected":
            session.stop_response = {
                "protocol": CONTROL_COMMAND_PROTOCOL, "ok": False,
                "error": {"code": "invalid_state_for_stop"},
            }
        else:
            session.cleanup_error = "managed_owner_closed"
        _patch_session_provider(monkeypatch, lambda node_id: session)
        with pytest.raises(
            ManagedObservationsError, match="session_close_failed"
        ) as error:
            _collect(factory, node_ids=(node,))
        if mode == "raise":
            assert isinstance(error.value.__cause__, RuntimeError)


def test_membership_recheck_after_collection_fails_closed(tmp_path, monkeypatch):
    with _owner_world(tmp_path) as (world, managed):
        node = world["member"]["node_id"]
        factory = _factory(world, managed)
        _patch_replies(monkeypatch, {node: _envelope(_result(node))})
        monkeypatch.setattr(factory.owner, "current_member", lambda node_id: None)
        with pytest.raises(ManagedObservationsError, match="member_not_current"):
            _collect(factory, node_ids=(node,))


def test_clock_invalid_and_captured_stale(tmp_path, monkeypatch):
    with _owner_world(tmp_path) as (world, managed):
        node = world["member"]["node_id"]
        factory = _factory(world, managed)
        _patch_replies(monkeypatch, {node: _envelope(_result(node))})
        for bad in (float("nan"), float("inf"), float("-inf"), "not-a-clock", None):
            with pytest.raises(ManagedObservationsError, match="clock_invalid"):
                _collect(factory, node_ids=(node,), clock=lambda b=bad: b)
        reads = iter([PINNED_NOW, PINNED_NOW - 7200.0])
        with pytest.raises(ManagedObservationsError, match="captured_stale"):
            _collect(factory, node_ids=(node,), clock=lambda: next(reads))


def test_session_failure_maps_to_sanitized_error(tmp_path):
    # retrospective: real factory over revoked member; error code passthrough.
    with _owner_world(tmp_path) as (world, managed):
        factory = _factory(world, managed)
        node = world["member"]["node_id"]
        world["authority"].revoke(node, world["member"]["generation"])
        with pytest.raises(ManagedObservationsError) as error:
            _collect(factory, node_ids=(node,))
        assert isinstance(error.value.code, str) and error.value.code
