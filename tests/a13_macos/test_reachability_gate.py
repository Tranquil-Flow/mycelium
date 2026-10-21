# SPDX-License-Identifier: AGPL-3.0-only
"""Pin the operator-side peer->owner reachability gate (round 54, job (c)).

Why this exists: a run that dies at the post-rotation reconnect has spent its one
bounded C5 attempt for nothing.  Measured 2026-09-15: the peer resolved its
reconnect once, then its rotated origin went unreachable and it claimed nothing at
all for the rest of the run -- 114/114 laptop `health` commands and the final
`stop` expired unclaimed against 117/117 `completed` on the local node.  So
`peer_owner_read` in `scripts/a13_operator/round14_bootstrap_compile.py`
gates the chain on bounded, repeated, non-mutating peer->owner reads.

This is a *pin*, not a RED: the guard already exists.  Its teeth are proven by
mutation instead -- see the round-54 evidence for the matrix.  What is worth
pinning is asymmetric, because the guard fails in two directions and only one of
them is loud:

  * too *lenient* (one success, or a streak that survives a failure) lets a
    channel that is still flapping be mistaken for a reachable one;
  * too *eager at retrying* turns a product refusal into a retry loop, converting
    a fail-closed rejection into a timeout and hiding the named cause.  A
    non-transient error must raise on the FIRST failure.

Also pinned: the instrument is the product's own read (`/api/staging/check`), not
`curl` -- this laptop cannot resolve `*.tail...cloudflare.com` names through system
DNS at all (20/20 probes returned `000`), so an operator-side HTTP probe would be
a false-negative instrument by construction.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / "scripts" / "a13_operator" / "round14_bootstrap_compile.py"
PRODUCT_READ = "/api/staging/check"


def _load(tmp_path: pathlib.Path):
    """Import the operator tool with its RPC clients stubbed out."""

    stub = types.ModuleType("round14_rpc")
    setattr(stub, "owner", lambda *args, **kwargs: {})
    setattr(stub, "target", lambda *args, **kwargs: {})
    setattr(stub, "PRIVATE", tmp_path)
    setattr(stub, "REMOTE", "/nonexistent/a13-remote-root")
    saved = sys.modules.get("round14_rpc")
    sys.modules["round14_rpc"] = stub
    try:
        spec = importlib.util.spec_from_file_location("a13_bootstrap_compile", TOOL)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if saved is None:
            sys.modules.pop("round14_rpc", None)
        else:
            sys.modules["round14_rpc"] = saved
    return module


def _fast(monkeypatch, module, *, budget: float = 120.0) -> None:
    monkeypatch.setattr(module, "REACHABILITY_GATE_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(module, "REACHABILITY_GATE_BUDGET_SECONDS", budget)


def test_two_consecutive_product_reads_are_required(tmp_path, monkeypatch) -> None:
    module = _load(tmp_path)
    _fast(monkeypatch, module)
    calls: list[str] = []

    def target(path, payload=None):
        calls.append(path)
        return {"offer": {"offer_id": "o"}}

    monkeypatch.setattr(module, "target", target)
    observations: list[dict] = []

    assert module.peer_owner_read(observations, label="t") == {"offer": {"offer_id": "o"}}
    # One success is not enough: the gate requires the configured streak.
    assert calls == [PRODUCT_READ, PRODUCT_READ]
    assert [entry["streak"] for entry in observations] == [1, 2]
    assert all(entry["outcome"] == "ok" for entry in observations)


def test_a_transient_failure_resets_the_streak(tmp_path, monkeypatch) -> None:
    module = _load(tmp_path)
    _fast(monkeypatch, module)
    sequence = iter([{"ok": 1}, RuntimeError("seed_http_unreachable"), {"ok": 2}, {"ok": 3}])

    def target(path, payload=None):
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(module, "target", target)
    observations: list[dict] = []

    assert module.peer_owner_read(observations, label="t") == {"ok": 3}
    # A failure in the middle means the streak restarts from zero.
    assert [entry["streak"] for entry in observations] == [1, 0, 1, 2]
    failed = observations[1]
    assert failed["outcome"] == "failed" and failed["transient"] is True
    assert "seed_http_unreachable" in failed["detail"]


def test_a_product_refusal_is_never_retried(tmp_path, monkeypatch) -> None:
    """Fail closed on the first non-transient error; do not spin it into a timeout."""

    module = _load(tmp_path)
    _fast(monkeypatch, module)
    calls: list[str] = []

    def target(path, payload=None):
        calls.append(path)
        raise RuntimeError("local_http_400:some_product_refusal")

    monkeypatch.setattr(module, "target", target)

    with pytest.raises(RuntimeError, match="peer_reachability_gate_failed"):
        module.peer_owner_read([], label="t")
    assert len(calls) == 1


def test_the_budget_bounds_transient_retries(tmp_path, monkeypatch) -> None:
    module = _load(tmp_path)
    _fast(monkeypatch, module, budget=0.05)
    calls: list[str] = []

    def target(path, payload=None):
        calls.append(path)
        raise RuntimeError("seed_http_unreachable")

    monkeypatch.setattr(module, "target", target)
    observations: list[dict] = []

    with pytest.raises(RuntimeError, match="peer_reachability_gate_failed"):
        module.peer_owner_read(observations, label="t")
    # Retried at least once, then bounded -- never an unbounded loop.
    assert len(calls) >= 2
    assert all(entry["streak"] == 0 for entry in observations)


def test_a_late_recovery_still_passes_the_gate(tmp_path, monkeypatch) -> None:
    """Flapping that settles must pass; the gate bounds time, it does not forbid retry."""

    module = _load(tmp_path)
    _fast(monkeypatch, module, budget=30.0)
    sequence = iter([
        RuntimeError("seed_http_remote_error"),
        RuntimeError("peer_transport_unreachable"),
        {"ok": 1},
        {"ok": 2},
    ])

    def target(path, payload=None):
        item = next(sequence)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(module, "target", target)

    assert module.peer_owner_read([], label="t") == {"ok": 2}
