# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: app-process side - publish the setting to the node, read its status, meter the session."""
from __future__ import annotations

import json
import os

import pytest

from mycelium_idle import node_link
from mycelium_idle import policy as ip
from mycelium_idle.app_link import AppIdleLink


class Source:
    def __init__(self):
        self.values = {}

    def sample(self, pids):
        return {pid: self.values[pid] for pid in pids if pid in self.values}


def make(tmp_path, **kwargs):
    clock = {"t": 100.0}
    source = Source()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    link = AppIdleLink(app_root=tmp_path, source=source, chip="Apple M4 Pro", cpu_cores=14,
                       clock=lambda: clock["t"], energy_interval_s=5.0, **kwargs)
    return link, source, clock, runtime_root


def test_default_setting_is_ten_minutes_and_is_published_to_the_node(tmp_path):
    link, source, clock, root = make(tmp_path)
    assert link.status()["policy"]["unload_after_minutes"] == 10
    link.tick(root, [])
    assert ip.read_policy(root).unload_after_minutes == 10
    assert oct((root / ip.POLICY_FILENAME).stat().st_mode & 0o777) == "0o600"


def test_a_changed_setting_reaches_the_node_and_never_is_supported(tmp_path):
    link, source, clock, root = make(tmp_path)
    link.tick(root, [])
    link.set_policy("never")
    link.tick(root, [])
    assert ip.read_policy(root).unload_after_minutes is None
    assert link.status()["policy"]["unload_after_minutes"] == "never"
    link.set_policy(25)
    link.tick(root, [])
    assert ip.read_policy(root).unload_after_minutes == 25


def test_an_invalid_setting_is_refused_and_leaves_the_old_one(tmp_path):
    link, source, clock, root = make(tmp_path)
    with pytest.raises(ip.IdlePolicyError):
        link.set_policy(0)
    assert link.status()["policy"]["unload_after_minutes"] == 10


def test_an_unreadable_app_policy_publishes_never_and_says_so(tmp_path):
    link, source, clock, root = make(tmp_path)
    path = tmp_path / ip.POLICY_FILENAME
    path.write_text("garbage")
    path.chmod(0o600)
    link.tick(root, [])
    assert ip.read_policy(root).unload_after_minutes is None
    assert link.status()["policy"]["error_code"] == "idle_policy_invalid"


def test_the_node_status_file_is_surfaced_as_the_member_state(tmp_path):
    link, source, clock, root = make(tmp_path)
    assert link.status()["member"] is None
    doc = {"state": "idle", "line": "idle (weights unloaded)", "reload_count": 2, "last_reload_ms": 2400,
           "reload_ms_history": [2300, 2400], "rss_before_unload_bytes": 7_000_000_000,
           "rss_after_unload_bytes": 150_000_000, "rss_after_reload_bytes": 6_900_000_000,
           "unload_after_minutes": 10, "unloaded_for_s": 30.0, "error_code": None, "extra": "dropped"}
    path = root / node_link.STATUS_FILENAME
    path.write_text(json.dumps(doc))
    path.chmod(0o600)
    link.tick(root, [])
    member = link.status()["member"]
    assert member["state"] == "idle" and member["line"] == "idle (weights unloaded)"
    assert member["last_reload_ms"] == 2400 and member["rss_after_unload_bytes"] == 150_000_000
    assert "extra" not in member


def test_a_broken_node_status_file_means_unknown_not_a_crash(tmp_path):
    link, source, clock, root = make(tmp_path)
    (root / node_link.STATUS_FILENAME).write_text("{")
    link.tick(root, [])
    assert link.status()["member"] is None


def test_energy_is_metered_from_worker_pids_and_throttled(tmp_path):
    link, source, clock, root = make(tmp_path)
    source.values[7] = (0.0, 0.0)
    link.tick(root, [7])          # baseline
    source.values[7] = (3600.0, 1800.0)
    link.tick(root, [7])          # inside the interval: not sampled again
    assert link.status()["energy"]["estimate_wh"] == 0.0
    clock["t"] += 6
    link.tick(root, [7])
    energy = link.status()["energy"]
    assert energy["estimate_wh"] > 0 and energy["is_estimate"] is True
    assert energy["text"].startswith("Energy this session: about ") and energy["text"].endswith("(estimate)")
    assert "7" not in json.dumps({k: v for k, v in energy.items() if k != "estimate_wh"}).replace("1800", "")


def test_a_new_runtime_starts_a_new_session(tmp_path):
    link, source, clock, root = make(tmp_path)
    source.values[7] = (0.0, 0.0)
    link.tick(root, [7])
    clock["t"] += 6
    source.values[7] = (100.0, 10.0)
    link.tick(root, [7])
    assert link.status()["energy"]["estimate_wh"] > 0
    link.new_session()
    assert link.status()["energy"]["estimate_wh"] == 0.0


def test_status_never_exposes_paths_or_pids(tmp_path):
    link, source, clock, root = make(tmp_path)
    link.tick(root, [os.getpid()])
    text = json.dumps(link.status())
    assert str(tmp_path) not in text and str(os.getpid()) not in text
