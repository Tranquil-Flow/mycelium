# SPDX-License-Identifier: AGPL-3.0-only
"""Synthetic-only admission geometry; never stages or contacts a physical host."""
from copy import deepcopy
from pathlib import Path
import subprocess

from scripts.experimental_candidate_isolation import check


OLD = "961409e34fe2bec0c80c81e8d6c4ee6fe6199919"
NEW = "a" * 40


def fixture(tmp_path):
    live = tmp_path / "live"
    candidate = tmp_path / "candidate"
    (live / "source").mkdir(parents=True, exist_ok=True)
    (candidate / "source").mkdir(parents=True, exist_ok=True)
    protected = {key: str(live / key) for key in
                 ("source", "seed", "product", "controller", "worker_stage")}
    protected["public_proxy"] = str(live / "proxy")
    for root in protected.values():
        Path(root).mkdir(exist_ok=True)
    roots = {key: str(candidate / key) for key in
             ("source", "seed", "product", "controller", "worker_stage")}
    roots["state"] = str(candidate / "state")
    doc = {
        "schema": "candidate_isolation_preflight.v1",
        "synthetic": True,
        "protected": {"candidate_id": OLD, "run_id": "w6-m1-exp-r8-961409e",
                      "roots": protected, "ports": [4350, 4353, 14351]},
        "candidate": {"candidate_id": NEW, "source_commit": NEW,
                      "run_id": "new-run-1", "roots": roots, "ports": [53999],
                      "authorization_status": "not_signed", "attempt_status": "not_started",
                      "binding": {"candidate_id": NEW, "source_commit": NEW,
                                  "issued_at_unix_ms": 1000, "expires_at_unix_ms": 999999,
                                  "status": "unconsumed"}},
        "remote": {"host_id": "remote-synthetic", "protected_root": "/srv/live/stage",
                   "candidate_root": "/srv/new/stage", "protected_ports": [8877],
                   "candidate_ports": [58999],
                   "inventory": {"method": "read_only_inventory", "host_id": "remote-synthetic",
                                 "captured_at_unix_ms": 2000,
                                 "protected_root_exists": True, "candidate_root_exists": False,
                                 "candidate_listeners": {"58999": "free"},
                                 "roots": {"/srv/live/stage": "/srv/live/stage",
                                           "/srv/new/stage": "/srv/new/stage"}}},
    }
    return doc


def assess(doc, *, now=3000, dirty=False, ports=None):
    return check(doc, now_unix_ms=now,
                 source_probe=lambda root: (NEW, dirty),
                 listener_probe=lambda port: (ports or {}).get(port, "free"))


def test_synthetic_clean_is_separation_only_without_live_authority(tmp_path):
    doc = fixture(tmp_path)
    before = sorted(str(p) for p in tmp_path.rglob("*"))
    result = assess(doc)
    assert result["verdict"] == "preflight_separation_only"
    assert result["live_access"] is False
    assert result["authorized"] is False
    assert result["synthetic"] is True
    assert "physical_inventory_and_signed_qualification" in result["future_gates"]
    assert before == sorted(str(p) for p in tmp_path.rglob("*"))


def test_consumed_r8_identity_and_binding_are_rejected(tmp_path):
    doc = fixture(tmp_path)
    doc["candidate"]["candidate_id"] = OLD
    doc["candidate"]["binding"]["candidate_id"] = OLD
    assert "consumed_identity" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["candidate"]["run_id"] = doc["protected"]["run_id"]
    assert "consumed_identity" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["candidate"]["binding"]["status"] = "consumed"
    assert "invalid_binding" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["candidate"]["authorization_ref"] = "r8/AUTHORIZATION.json"
    assert "prior_authority_or_attempt" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["candidate"]["attempt_ref"] = "r8/attempt"
    assert "prior_authority_or_attempt" in assess(doc)["reasons"]


def test_reuse_of_each_frozen_local_root_rejected(tmp_path):
    doc = fixture(tmp_path)
    for key in ("seed", "worker_stage", "product", "controller", "source"):
        altered = deepcopy(doc)
        altered["candidate"]["roots"][key] = doc["protected"]["roots"][key]
        assert "root_overlap" in assess(altered)["reasons"], key


def test_ancestor_and_symlink_alias_rejected(tmp_path):
    doc = fixture(tmp_path)
    doc["candidate"]["roots"]["seed"] = str(tmp_path / "live")
    assert "root_overlap" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path / "live", target_is_directory=True)
    doc["candidate"]["roots"]["seed"] = str(alias / "seed")
    assert "root_overlap" in assess(doc)["reasons"]


def test_remote_inventory_is_required_and_must_match_host_and_paths(tmp_path):
    doc = fixture(tmp_path)
    del doc["remote"]["inventory"]
    assert "remote_not_verified" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["inventory"]["host_id"] = "other-host"
    assert "remote_not_verified" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["inventory"]["roots"].pop("/srv/new/stage")
    assert "remote_not_verified" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["inventory"]["roots"]["/srv/new/stage"] = "/srv/live/stage/subdir"
    assert "root_overlap" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["inventory"]["candidate_root_exists"] = True
    assert "remote_not_verified" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["candidate_ports"] = [8877]
    assert "protected_port" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["remote"]["inventory"]["candidate_listeners"] = {"58999": "occupied"}
    assert "remote_not_verified" in assess(doc)["reasons"]


def test_listener_protected_port_and_unknown_probe_fail_closed(tmp_path):
    doc = fixture(tmp_path)
    doc["candidate"]["ports"] = [4353]
    assert "protected_port" in check(
        doc, now_unix_ms=3000, source_probe=lambda _root: (NEW, False),
        listener_probe=lambda _port: (_ for _ in ()).throw(AssertionError("protected port probed")))["reasons"]
    doc = fixture(tmp_path)
    assert "listener_occupied" in assess(doc, ports={53999: "occupied"})["reasons"]
    assert "listener_not_verified" in assess(doc, ports={53999: "unknown"})["reasons"]


def test_dirty_source_and_missing_or_expired_binding(tmp_path):
    doc = fixture(tmp_path)
    assert "source_dirty_or_mismatch" in assess(doc, dirty=True)["reasons"]
    doc = fixture(tmp_path)
    del doc["candidate"]["binding"]
    assert "invalid_binding" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    assert "invalid_binding" in assess(doc, now=1000000)["reasons"]


def test_existing_candidate_state_or_stage_and_malformed_input(tmp_path):
    doc = fixture(tmp_path)
    Path(doc["candidate"]["roots"]["state"]).mkdir()
    assert "candidate_root_exists" in assess(doc)["reasons"]
    doc = fixture(tmp_path)
    doc["candidate"]["roots"]["worker_stage"] = "relative/path"
    assert "invalid_root" in assess(doc)["reasons"]


def test_synthetic_checkout_uses_actual_read_only_git_probe(tmp_path):
    doc = fixture(tmp_path)
    source = Path(doc["candidate"]["roots"]["source"])
    scrub = ("env", "-u", "GIT_DIR", "-u", "GIT_WORK_TREE", "-u", "GIT_COMMON_DIR",
             "-u", "GIT_OBJECT_DIRECTORY", "-u", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
             "-u", "PYTHONPATH", "-u", "PYTHONHOME", "-u", "VIRTUAL_ENV",
             "PYTHONDONTWRITEBYTECODE=1", "git", "-C", str(source))
    def git(*args):
        return subprocess.run((*scrub, *args), capture_output=True, text=True,
                              check=True, timeout=5).stdout.strip()
    git("init", "-q")
    (source / "tracked.txt").write_text("synthetic only\n")
    git("add", "tracked.txt")
    git("-c", "user.name=Synthetic", "-c", "user.email=synthetic@invalid.example",
        "commit", "-qm", "synthetic snapshot")
    commit = git("rev-parse", "HEAD")
    doc["candidate"]["candidate_id"] = commit
    doc["candidate"]["source_commit"] = commit
    doc["candidate"]["binding"]["candidate_id"] = commit
    doc["candidate"]["binding"]["source_commit"] = commit
    assert check(doc, now_unix_ms=3000, listener_probe=lambda _port: "free")["verdict"] == "preflight_separation_only"
    (source / "tracked.txt").write_text("dirty synthetic only\n")
    assert "source_dirty_or_mismatch" in check(
        doc, now_unix_ms=3000, listener_probe=lambda _port: "free")["reasons"]
