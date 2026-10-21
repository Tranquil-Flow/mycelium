# SPDX-License-Identifier: AGPL-3.0-only
"""``run_a9_gate.py seed-cutover-transaction`` and its run-day readback/stop hooks.

A real seed database and durable members (the gate-driver fixture), a tiny fake
*fleet* script that owns only fake process facts, and the real readback/stop-check
hooks.  Fence tests use tmp decoy roots, never the production seed root.
"""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from mycelium_platform_neutral import seed_cutover_transaction as tx
from mycelium_seed import SqliteSeedState
from tests.a9_acceptance.test_a9_gate_driver import BUILD, _run, swarm  # noqa: F401  (fixture)

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="macOS mlx members; POSIX pid probes")

FLEET = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, {root!r})
sys.path.insert(0, {root!r} + "/scripts")
import run_a9_gate as gate

cfg = json.loads(Path({cfg!r}).read_text())
state_path = Path(cfg["state"])
state = json.loads(state_path.read_text())
log = Path(cfg["log"])


def note(entry):
    with log.open("a") as stream:
        stream.write(entry + "\n")


def readback():
    g = state["gen"]
    facts = {{"owned": cfg.get("owned", True), "listener_up": state["alive"], "seed_pid": 1000 + g, "seed_birth": f"b{{g}}",
             "members": {{n: {{"pid": 2000 + g, "birth": f"m{{g}}"}} for n in cfg["nodes"]}}}}
    path = Path(cfg["state"] + ".facts")
    path.write_text(json.dumps(facts))
    return gate.main(["seed-cutover-readback", "--seed-db", cfg["db"], "--seed-key", cfg["key"],
                      "--swarm-id", "swarm-a", "--process-facts", str(path)])


cmd = sys.argv[1]
note(" ".join(sys.argv[1:]))
if cmd == "readback":
    raise SystemExit(readback())
if cmd == "stop":
    state["alive"] = False
    state_path.write_text(json.dumps(state))
    pids = [os.getpid()] if cfg.get("stop_fails") else [2147483000]
    raise SystemExit(gate.main(["seed-cutover-stop-check", "--seed-port", str(cfg["port"]),
                                *[a for p in pids for a in ("--pid", str(p))]]))
if cmd == "start":
    assert Path(sys.argv[2]).is_dir()
    state.update(alive=True, gen=state["gen"] + 1)
    # Every owned restart re-registers the members (generation + 1, new incarnation), as real members do.  A
    # cutover that committed has already advanced them once (commit), so the restart makes it two.
    import sqlite3
    with sqlite3.connect(cfg["db"]) as conn:
        conn.execute("UPDATE seed_members SET generation = generation + 1, incarnation = incarnation || ?",
                     (f"-restart{{state['gen']}}",))
    state_path.write_text(json.dumps(state))
    raise SystemExit(readback())
'''


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _prepare(swarm, tmp_path, *, migrate=True):  # noqa: F811
    seed_home, database, data_dirs = swarm
    gate = tmp_path / "gate"
    seed_key = seed_home / "identity" / "seed.key"
    assert _run("seed-export", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a", "--build-digest", BUILD, "--software-version", "1.0") == 0
    profiles = tmp_path / "profiles"
    for node_id, data_dir in data_dirs.items():
        assert _run("member-sign", "--request", gate / "requests" / f"{node_id}.json", "--data-dir", data_dir,
                    "--new-incarnation", "incarnation-v2", "--out", profiles / f"{node_id}.json") == 0
    if migrate:
        assert _run("migrate", "--gate-dir", gate, "--profiles", profiles) == 0
    return gate, profiles, database, seed_key, data_dirs


def _fleet(tmp_path, database, seed_key, data_dirs, *, remote=None, **config):
    script = tmp_path / "fleet.py"
    cfg = tmp_path / "fleet-config.json"
    state = tmp_path / "fleet-state.json"
    log = tmp_path / "fleet-calls.log"
    state.write_text(json.dumps({"alive": True, "gen": 0}))
    cfg.write_text(json.dumps({"state": str(state), "log": str(log), "db": str(database), "key": str(seed_key),
                               "nodes": sorted(data_dirs), "port": _free_port(), **config}))
    script.write_text(FLEET.format(root=str(ROOT), cfg=str(cfg)))
    commands = {name: [sys.executable, str(script), name] for name in ("readback", "stop", "start")}
    if remote:
        commands["adopt_remote"] = remote
    path = tmp_path / "fleet-cmds.json"
    path.write_text(json.dumps(commands))
    return path, log


def _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path, *extra, local=True):
    owned = tmp_path / "run-root"
    owned.mkdir(exist_ok=True)
    adopt = []
    if local:
        for node, data_dir in data_dirs.items():
            adopt += ["--adopt-local", node, str(profiles / f"{node}.json"), str(data_dir)]
    return _run("seed-cutover-transaction", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a", "--fleet-commands", commands, "--owned-root", owned,
                "--epoch-parent", owned / "restarts", *adopt, *extra)


def test_readback_hook_reads_identity_contract_and_generations_from_the_real_seed(swarm, tmp_path, capsys):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    facts = tmp_path / "facts.json"
    facts.write_text(json.dumps({"owned": True, "listener_up": True, "seed_pid": 7, "seed_birth": "b",
                                 "members": {n: {"pid": 9, "birth": "m"} for n in data_dirs}}))
    capsys.readouterr()
    assert _run("seed-cutover-readback", "--seed-db", database, "--seed-key", seed_key, "--swarm-id", "swarm-a",
                "--process-facts", facts) == 0
    doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert {"owned", "listener_up", "seed_pid", "seed_birth", "contract", "seed_key_digest", "swarm_id",
            "incumbent_digest", "members"} <= set(doc)
    assert doc["contract"] == "v1" and doc["swarm_id"] == "swarm-a"
    assert set(doc["members"]) == set(data_dirs)
    assert all({"generation", "pid", "birth", "key_digest"} <= set(m) for m in doc["members"].values())
    # A wrong seed key or swarm is refused, not read back.
    assert _run("seed-cutover-readback", "--seed-db", database, "--seed-key", seed_key, "--swarm-id", "other",
                "--process-facts", facts) == 2
    # Process facts naming a member the seed does not know are refused.
    facts.write_text(json.dumps({"owned": True, "listener_up": True, "seed_pid": 7, "seed_birth": "b",
                                 "members": {"ghost": {"pid": 1, "birth": "x"}}}))
    assert _run("seed-cutover-readback", "--seed-db", database, "--seed-key", seed_key, "--swarm-id", "swarm-a",
                "--process-facts", facts) == 2


def test_stop_check_hook_reports_absent_listener_and_live_pids(tmp_path, capsys):
    port = _free_port()
    capsys.readouterr()
    assert _run("seed-cutover-stop-check", "--seed-port", port, "--pid", 2147483000) == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1]) == {
        "seed_listener_absent": True, "owned_pids_alive": []}
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        assert _run("seed-cutover-stop-check", "--seed-port", port, "--pid", os.getpid()) == 0
        doc = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert doc == {"seed_listener_absent": False, "owned_pids_alive": [os.getpid()]}


def test_transaction_commits_over_a_real_seed_and_writes_receipts(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    commands, log = _fleet(tmp_path, database, seed_key, data_dirs)
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path) == 0
    assert SqliteSeedState(database).membership_contract()["version"] == "v2"
    receipt = json.loads((gate / "06-seed-cutover-transaction.json").read_text())
    assert receipt["kind"] == "seed_cutover_transaction"
    assert receipt["transaction"]["status"] == "COMMITTED_VERIFIED"
    assert all(receipt["transaction"]["checks"].values())
    assert receipt["transaction"]["restart_epoch"] == "restart-1"
    assert (gate / "05-seed-cutover.json").exists()
    assert log.read_text().split("\n")[:3] == ["readback", "stop", "start " + str(tmp_path / "run-root" / "restarts" / "restart-1")]
    # No stop-confirmation flag exists on the CLI: the commit ran only after the stop evidence.
    from scripts import run_a9_gate
    parser = run_a9_gate.build_parser()
    sub = next(a for a in parser._actions if getattr(a, "choices", None)).choices["seed-cutover-transaction"]
    assert "--seed-stopped" not in sub.format_help()


def test_negative_mode_preserves_the_incumbent_and_a_real_failure_rolls_back(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path, migrate=False)  # no migration record
    commands, log = _fleet(tmp_path, database, seed_key, data_dirs)
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path, "--expect-refusal") == 0
    negative = json.loads((gate / "06-seed-cutover-negative.json").read_text())
    assert negative["transaction"]["status"] == "NEGATIVE_PRESERVED"
    assert negative["transaction"]["checks"]["incumbent_unchanged"] is True
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"
    # Same failing cutover without the negative expectation is a rollback, exit 1.
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path) == 1
    real = json.loads((gate / "06-seed-cutover-transaction.json").read_text())
    assert real["transaction"]["status"] == "ROLLED_BACK"
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"


def test_negative_mode_fails_loudly_when_the_cutover_was_not_refused(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    commands, _ = _fleet(tmp_path, database, seed_key, data_dirs)
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path, "--expect-refusal") == 1
    negative = json.loads((gate / "06-seed-cutover-negative.json").read_text())
    assert negative["transaction"]["status"] == "NEGATIVE_NOT_REFUSED"


def test_unconfirmed_stop_refuses_with_a_receipt_and_no_commit(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    commands, log = _fleet(tmp_path, database, seed_key, data_dirs, stop_fails=True)
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path) == 2
    receipt = json.loads((gate / "06-seed-cutover-transaction.json").read_text())
    assert receipt["transaction"]["status"] == "REFUSED" and receipt["transaction"]["reason"] == "stop_not_confirmed"
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"
    assert not (gate / "05-seed-cutover.json").exists()
    assert not any(line.startswith("start") for line in log.read_text().split("\n"))


def test_remote_member_adoption_is_a_fleet_argv_given_the_adoption_envelope(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    marker = tmp_path / "remote-adopt.txt"
    script = f"import sys,pathlib; pathlib.Path({str(marker)!r}).write_text(pathlib.Path(sys.argv[2]).name)"
    commands, _ = _fleet(tmp_path, database, seed_key, data_dirs, remote={"node-b": [sys.executable, "-c", script, "x"]})
    # node-b is adopted by the fleet's remote hook, node-a locally.
    owned = tmp_path / "run-root"
    owned.mkdir()
    assert _run("seed-cutover-transaction", "--gate-dir", gate, "--seed-db", database, "--seed-key", seed_key,
                "--swarm-id", "swarm-a", "--fleet-commands", commands, "--owned-root", owned,
                "--epoch-parent", owned / "restarts",
                "--adopt-local", "node-a", profiles / "node-a.json", data_dirs["node-a"]) == 0
    assert marker.read_text() == "node-b.json"
    receipt = json.loads((gate / "06-seed-cutover-transaction.json").read_text())
    assert "adopted:node-a" in receipt["transaction"]["phases"] and "adopted:node-b" in receipt["transaction"]["phases"]


@pytest.mark.parametrize("which", ["owned-root", "seed-db", "gate-dir", "epoch-parent"])
def test_production_root_fence_covers_every_path_the_command_writes(swarm, tmp_path, monkeypatch, which):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    commands, log = _fleet(tmp_path, database, seed_key, data_dirs)
    decoy = tmp_path / "decoy-production"
    decoy.mkdir()
    link = tmp_path / "decoy-link"
    link.symlink_to(decoy)
    monkeypatch.setattr(tx, "PRODUCTION_ROOTS", (decoy,))
    owned = tmp_path / "run-root"
    owned.mkdir()
    paths = {"owned-root": owned, "seed-db": database, "gate-dir": gate, "epoch-parent": owned / "restarts"}
    paths[which] = (link / "inside") if which != "seed-db" else link / "seed.sqlite3"
    assert _run("seed-cutover-transaction", "--gate-dir", paths["gate-dir"], "--seed-db", paths["seed-db"],
                "--seed-key", seed_key, "--swarm-id", "swarm-a", "--fleet-commands", commands,
                "--owned-root", paths["owned-root"], "--epoch-parent", paths["epoch-parent"]) == 2
    assert not log.exists() or log.read_text() == ""
    assert not any(decoy.iterdir())
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"


def test_invalid_fleet_commands_are_refused(swarm, tmp_path):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"readback": "echo hi", "stop": [], "start": []}))
    assert _transaction(gate, profiles, database, seed_key, data_dirs, bad, tmp_path) == 2
    assert SqliteSeedState(database).membership_contract()["version"] == "v1"


def test_summary_lists_the_product_piece_receipts(swarm, tmp_path, capsys):
    gate, profiles, database, seed_key, data_dirs = _prepare(swarm, tmp_path)
    commands, _ = _fleet(tmp_path, database, seed_key, data_dirs)
    assert _transaction(gate, profiles, database, seed_key, data_dirs, commands, tmp_path) == 0
    (gate / "13-mac-benchmark.json").write_text(json.dumps({"kind": "mac_stage_benchmark"}))
    assert _run("summary", "--gate-dir", gate) == 0
    summary = json.loads((gate / "99-summary.json").read_text())
    pieces = summary["product_pieces"]
    assert pieces["seed cutover transaction (owned stop/commit/adopt/restart)"] is True
    assert pieces["Mac MLX/INT8 stage benchmark"] is True
    assert pieces["adjacent-host codec emit"] is False
    assert pieces["two-session browser withdrawal"] is False
    (gate / "30-withdrawal-browser.json").write_text(json.dumps({"kind": "a9_two_session_withdrawal"}))
    assert _run("summary", "--gate-dir", gate) == 0
    assert json.loads((gate / "99-summary.json").read_text())["product_pieces"]["two-session browser withdrawal"] is True
    assert pieces["adjacent-host codec verify"] is False
    assert pieces["seed cutover negative (incumbent preserved)"] is False
    assert summary["status"]["migration + cutover + reader removal"] is True  # existing keys unchanged
