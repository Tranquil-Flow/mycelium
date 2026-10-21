# SPDX-License-Identifier: AGPL-3.0-only
"""Owned private-fleet stop/commit/adopt/restart transaction with readback.

Deterministic fakes only: no fleet, no seed, no devices.  The fence tests use tmp
decoy roots, never the production seed root.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from mycelium_platform_neutral import seed_cutover_transaction as tx

BEFORE = {"owned": True, "seed_pid": 100, "seed_birth": "b1", "listener_up": True,
          "contract": "v1", "seed_key_digest": "sha256:" + "a" * 64, "swarm_id": "swarm",
          "incumbent_digest": "sha256:" + "1" * 64, "authority_digest": "sha256:" + "9" * 64,
          "authority_epoch": 3,
          "members": {"node-0": {"generation": 4, "pid": 101, "birth": "m0", "key_digest": "k0", "incarnation": "i0-a"},
                      "node-2": {"generation": 7, "pid": 102, "birth": "m2", "key_digest": "k2", "incarnation": "i2-a"}}}


def _restarted_incumbent(**over):
    """The unchanged incumbent after an owned restart: members re-register (generation + 1, new incarnation)."""
    doc = json.loads(json.dumps(BEFORE))
    doc.update(seed_pid=300, seed_birth="b3")
    for node, member in doc["members"].items():
        member.update(generation=member["generation"] + 1, pid=member["pid"] + 200, birth=member["birth"] + "r",
                      incarnation=member["incarnation"].replace("-a", "-b"))
    doc.update(over)
    return doc


def _after(**over):
    """A committed cutover after its restart: the commit binds generation + 1 and the owned restart re-registers (+ 1)."""
    doc = json.loads(json.dumps(BEFORE))
    doc.update(seed_pid=200, seed_birth="b2", contract="v2", incumbent_digest="sha256:" + "2" * 64)
    for node in doc["members"].values():
        node["generation"] += 2
        node["pid"] += 100
    doc.update(over)
    return doc


class FakeFleet:
    def __init__(self, tmp_path, *, stopped_ok=True, after=None, owned=True, rollback_after=None):
        self.calls, self.stopped_ok, self.owned = [], stopped_ok, owned
        self.after, self.rollback_after = after or _after(), rollback_after
        self.committed = False
        self.root = tmp_path / "fleet"

    def readback(self):
        self.calls.append("readback")
        base = json.loads(json.dumps(BEFORE)) | {"owned": self.owned}
        return json.loads(json.dumps(self.after)) if self.committed else base

    def stop(self):
        self.calls.append("stop")
        return {"seed_listener_absent": self.stopped_ok, "owned_pids_alive": [] if self.stopped_ok else [100]}

    def start(self, epoch):
        self.calls.append(f"start:{epoch.name}")
        assert epoch.is_dir()
        return self.readback() if self.committed else (self.rollback_after or _restarted_incumbent())


def _run(fleet, tmp_path, cutover=None, adopt=None, **kw):
    def commit():
        fleet.calls.append("cutover")
        if cutover:
            cutover()
        fleet.committed = True
    return tx.run_seed_cutover(
        fleet, cutover=commit, adopt=adopt or {"node-0": lambda: fleet.calls.append("adopt:node-0"),
                                               "node-2": lambda: fleet.calls.append("adopt:node-2")},
        epoch_parent=tmp_path / "epochs", forbidden_roots=(tmp_path / "production",), owned_root=fleet.root, **kw)


def test_happy_path_orders_stop_commit_adopt_restart_and_reads_back_identity(tmp_path):
    fleet = FakeFleet(tmp_path)
    receipt = _run(fleet, tmp_path)
    assert fleet.calls == ["readback", "stop", "cutover", "adopt:node-0", "adopt:node-2", "start:restart-1", "readback"] or \
        fleet.calls == ["readback", "stop", "cutover", "adopt:node-0", "adopt:node-2", "start:restart-1"]
    assert receipt["status"] == "COMMITTED_VERIFIED"
    assert receipt["after"]["seed_pid"] != receipt["before"]["seed_pid"]
    assert receipt["checks"]["member_generations_advanced_by_commit_and_restart"] is True
    assert receipt["checks"]["identity_preserved"] is True
    assert receipt["restart_epoch"] == "restart-1"


def test_unconfirmed_stop_refuses_before_any_cutover(tmp_path):
    fleet = FakeFleet(tmp_path, stopped_ok=False)
    receipt = _run(fleet, tmp_path)
    assert receipt["status"] == "REFUSED" and receipt["reason"] == "stop_not_confirmed"
    assert "cutover" not in fleet.calls and not any(c.startswith("start") for c in fleet.calls)


def test_unowned_fleet_is_never_stopped(tmp_path):
    fleet = FakeFleet(tmp_path, owned=False)
    receipt = _run(fleet, tmp_path)
    assert receipt["status"] == "REFUSED" and receipt["reason"] == "fleet_not_owned"
    assert "stop" not in fleet.calls


def test_cutover_failure_restarts_the_unchanged_incumbent_and_proves_it(tmp_path):
    fleet = FakeFleet(tmp_path)
    def boom():
        raise RuntimeError("cutover refused")
    receipt = _run(fleet, tmp_path, cutover=boom)
    assert receipt["status"] == "ROLLED_BACK" and receipt["reason"] == "cutover_failed:RuntimeError:cutover refused"
    assert receipt["checks"]["incumbent_unchanged"] is True
    assert fleet.calls[-1].startswith("start:") and "adopt:node-0" not in fleet.calls


def _boom():
    raise RuntimeError("x")


def test_rollback_that_diverges_from_the_incumbent_is_reported_not_hidden(tmp_path):
    fleet = FakeFleet(tmp_path, rollback_after=_restarted_incumbent(contract="v2"))
    receipt = _run(fleet, tmp_path, cutover=_boom)
    assert receipt["status"] == "ROLLBACK_DIVERGED" and receipt["checks"]["incumbent_unchanged"] is False


def test_rollback_requires_each_restarted_member_generation_to_advance_by_exactly_one(tmp_path):
    ok = _run(FakeFleet(tmp_path), tmp_path, cutover=_boom)
    assert ok["status"] == "ROLLED_BACK" and ok["checks"]["member_generations_advanced_by_one"] is True
    for bad_generation in (7, 9):  # unchanged (not restarted) and jumped by 2
        after = _restarted_incumbent()
        after["members"]["node-2"]["generation"] = bad_generation
        receipt = _run(FakeFleet(tmp_path / str(bad_generation), rollback_after=after), tmp_path / str(bad_generation),
                       cutover=_boom)
        assert receipt["status"] == "ROLLBACK_DIVERGED"
        assert receipt["checks"]["incumbent_unchanged"] is False
        assert receipt["checks"]["member_generations_advanced_by_one"] is False


@pytest.mark.parametrize("mutation, failed", [
    (lambda d: d.update(authority_digest="sha256:" + "8" * 64), "incumbent_authority_preserved"),
    (lambda d: d.update(authority_epoch=4), "incumbent_authority_preserved"),
    (lambda d: d.update(incumbent_digest="sha256:" + "2" * 64), "incumbent_authority_preserved"),
    (lambda d: d.update(contract="v2"), "incumbent_authority_preserved"),
    (lambda d: d.update(swarm_id="other"), "incumbent_authority_preserved"),
    (lambda d: d["members"]["node-0"].update(key_digest="kx"), "incumbent_authority_preserved"),
    (lambda d: d["members"].pop("node-2"), "incumbent_authority_preserved"),
    (lambda d: d["members"]["node-0"].update(incarnation="i0-a"), "member_incarnations_restarted"),
    (lambda d: d["members"]["node-0"].pop("incarnation"), "member_incarnations_restarted"),
])
def test_rollback_divergence_is_named_per_check(tmp_path, mutation, failed):
    after = _restarted_incumbent()
    mutation(after)
    receipt = _run(FakeFleet(tmp_path, rollback_after=after), tmp_path, cutover=_boom)
    assert receipt["status"] == "ROLLBACK_DIVERGED"
    assert receipt["checks"][failed] is False and receipt["checks"]["incumbent_unchanged"] is False


@pytest.mark.parametrize("mutation, failed", [
    (lambda d: d.update(seed_pid=100, seed_birth="b1"), "seed_process_replaced"),
    (lambda d: d.update(contract="v1"), "contract_v2"),
    (lambda d: d.update(seed_key_digest="sha256:" + "f" * 64), "identity_preserved"),
    (lambda d: d["members"]["node-2"].update(generation=7), "member_generations_advanced_by_commit_and_restart"),
    (lambda d: d["members"]["node-2"].update(generation=8), "member_generations_advanced_by_commit_and_restart"),
    (lambda d: d["members"]["node-2"].update(generation=10), "member_generations_advanced_by_commit_and_restart"),
    (lambda d: d["members"]["node-0"].update(generation=True), "member_generations_advanced_by_commit_and_restart"),
    (lambda d: d["members"]["node-2"].pop("generation"), "member_generations_advanced_by_commit_and_restart"),
    (lambda d: d.update(listener_up=False), "seed_listening"),
])
def test_post_restart_readback_failures_are_named(tmp_path, mutation, failed):
    bad = _after()
    mutation(bad)
    fleet = FakeFleet(tmp_path, after=bad)
    receipt = _run(fleet, tmp_path)
    assert receipt["status"] == "POST_RESTART_READBACK_FAILED"
    assert receipt["checks"][failed] is False


def test_negative_cutover_expects_failure_and_proves_the_incumbent_untouched(tmp_path):
    fleet = FakeFleet(tmp_path)
    def refused():
        raise RuntimeError("tampered chain")
    receipt = _run(fleet, tmp_path, cutover=refused, expect_refusal=True)
    assert receipt["status"] == "NEGATIVE_PRESERVED" and receipt["checks"]["incumbent_unchanged"] is True
    assert all(receipt["after"]["members"][n]["generation"] == BEFORE["members"][n]["generation"] + 1
               for n in BEFORE["members"])
    ok = _run(FakeFleet(tmp_path / "other"), tmp_path / "other2", expect_refusal=True)
    assert ok["status"] == "NEGATIVE_NOT_REFUSED"


def test_restart_never_reuses_a_spent_epoch_root(tmp_path):
    parent = tmp_path / "epochs"
    first = tx.claim_restart_epoch(parent)
    second = tx.claim_restart_epoch(parent)
    assert (first.name, second.name) == ("restart-1", "restart-2")
    assert oct(first.stat().st_mode & 0o777) == "0o700"
    (parent / "restart-3").symlink_to(parent / "restart-1")
    with pytest.raises(tx.CutoverRefused, match="restart_epoch_exists"):
        tx.claim_restart_epoch(parent, number=3)


def test_production_root_and_decoys_are_refused_before_anything_runs(tmp_path):
    production = tmp_path / "production"
    production.mkdir()
    decoy = tmp_path / "decoy"
    decoy.symlink_to(production)
    for candidate in (production, decoy, production / "seed"):
        with pytest.raises(tx.CutoverRefused, match="owned_root_forbidden"):
            tx.require_owned_root(candidate, forbidden_roots=(production,))
    fleet = FakeFleet(tmp_path)
    fleet.root = decoy
    receipt = _run(fleet, tmp_path)
    assert receipt["status"] == "REFUSED" and receipt["reason"] == "owned_root_forbidden"
    assert fleet.calls == []


def test_default_forbidden_root_is_the_production_physical_run(tmp_path):
    assert any(str(p).endswith("mycelium-physical-run") for p in tx.PRODUCTION_ROOTS)


def test_default_forbidden_root_is_derived_from_the_home_not_a_developer_path(tmp_path):
    assert tx.default_production_roots(home=tmp_path) == (tmp_path / "mycelium-physical-run",)
    assert Path.home() / "mycelium-physical-run" in tx.PRODUCTION_ROOTS
    assert "evi" + "nova-self" not in Path(tx.__file__).read_text()


def test_forbidden_roots_accept_an_explicit_environment_list(tmp_path):
    extra = tmp_path / "prod-a"
    other = tmp_path / "prod-b"
    env = {"MYCELIUM_PRODUCTION_SEED_ROOTS": os.pathsep.join([str(extra), str(other), "relative/ignored", ""])}
    roots = tx.default_production_roots(home=tmp_path, environ=env)
    assert roots == (tmp_path / "mycelium-physical-run", extra, other)


def test_cutover_failure_reason_keeps_the_error_code_and_bounds_the_message(tmp_path):
    fleet = FakeFleet(tmp_path)
    def refused():
        raise tx.CutoverRefused("seed_key_mismatch")
    receipt = _run(fleet, tmp_path, cutover=refused)
    assert receipt["reason"] == "cutover_failed:CutoverRefused:seed_key_mismatch"
    long = FakeFleet(tmp_path)
    def verbose():
        raise ValueError("x" * 500)
    receipt = _run(long, tmp_path, cutover=verbose)
    assert receipt["reason"] == "cutover_failed:ValueError:" + "x" * 120


def test_command_fleet_adapter_runs_listed_argv_only_and_parses_json(tmp_path):
    marker = tmp_path / "marker"
    script = f"import json,sys,pathlib; pathlib.Path({str(marker)!r}).write_text(sys.argv[1]); print(json.dumps({{'owned': True}}))"
    fleet = tx.CommandOwnedFleet({"readback": [sys.executable, "-c", script, "rb"],
                                  "stop": [sys.executable, "-c", script, "st"],
                                  "start": [sys.executable, "-c", script, "go"]}, timeout_s=20)
    assert fleet.readback() == {"owned": True} and marker.read_text() == "rb"
    assert fleet.stop() == {"owned": True}
    epoch = tx.claim_restart_epoch(tmp_path / "epochs")
    assert fleet.start(epoch) == {"owned": True} and marker.read_text() == "go"
    with pytest.raises(tx.CutoverRefused, match="fleet_command_invalid"):
        tx.CommandOwnedFleet({"readback": "echo hi", "stop": [], "start": []})
    bad = tx.CommandOwnedFleet({k: [sys.executable, "-c", "print('not json')"] for k in ("readback", "stop", "start")})
    with pytest.raises(tx.CutoverRefused, match="fleet_command_output_invalid"):
        bad.readback()


def test_committed_cutover_judges_the_hardware_70_to_72_advance_and_refuses_unexplained_jumps(tmp_path):
    """A9 hardware: adopt + restart took both members 70 -> 72 (commit n+1, restart n+2) and is correct."""
    for advance, status in ((2, "COMMITTED_VERIFIED"), (0, "POST_RESTART_READBACK_FAILED"),
                            (1, "POST_RESTART_READBACK_FAILED"), (3, "POST_RESTART_READBACK_FAILED")):
        before = json.loads(json.dumps(BEFORE))
        after = _after()
        for node, member in after["members"].items():
            member["generation"] = before["members"][node]["generation"] + advance
        receipt = _run(FakeFleet(tmp_path / str(advance), after=after), tmp_path / str(advance))
        assert receipt["status"] == status, advance
        assert receipt["checks"]["member_generations_advanced_by_commit_and_restart"] is (advance == 2), advance
