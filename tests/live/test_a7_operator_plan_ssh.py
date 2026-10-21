# SPDX-License-Identifier: AGPL-3.0-only
"""Remote A7 parity workers must not hang on a silently dead ssh link."""
from __future__ import annotations

from mycelium_a7_operator_plan import parity_worker_argv


def test_remote_parity_worker_ssh_fails_dead_links_with_keepalives() -> None:
    plan = {"controller": {
        "peers": [{"node_id": "node-1", "staging_root": "/tmp/stage"}],
        "run_plan": {"nodes": [{"node_id": "node-1", "configure": {
            "manifest_file": "control/manifest.json", "load_generation": 1}}]},
    }}
    argv = parity_worker_argv(
        plan, role_node="node-1", placement_id="placement-000",
        assignment_file="control/a.json", stage_pack_file="control/p.json",
        python="/usr/bin/python3", code_root=None, ssh={"target": "node@peer.example"})
    assert argv[0] == "ssh" and argv[-2] == "node@peer.example"
    assert "ServerAliveInterval=10" in argv
    assert "ServerAliveCountMax=6" in argv
