# SPDX-License-Identifier: AGPL-3.0-only
"""The Windows managed host CLI: identity, consent-gated pairing request, refusals."""

from __future__ import annotations

import json

import pytest

from mycelium_a13_windows.managed_host.__main__ import main

POLICY = {"peer_visibility": True, "power": "ac_only", "storage_bytes": 200_000_000,
          "network": "metered_allowed", "background": False, "diagnostics": False,
          "thermal": "stop_on_serious"}


def test_status_and_pairing_request_need_a_valid_policy(tmp_path, capsys):
    root = tmp_path / "host"
    assert main(["--root", str(root), "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["identity_state"] == "present" and status["membership_state"] == "absent"
    bad = tmp_path / "bad-policy.json"
    bad.write_text(json.dumps({**POLICY, "background": True}))
    with pytest.raises(SystemExit, match="resource policy invalid"):
        main(["--root", str(root), "pair-request", "--policy", str(bad), "--out", str(tmp_path / "r.json")])
    good = tmp_path / "policy.json"
    good.write_text(json.dumps(POLICY))
    out = tmp_path / "request.json"
    assert main(["--root", str(root), "pair-request", "--policy", str(good), "--out", str(out)]) == 0
    request = json.loads(out.read_text())
    assert request["peer_class"] == "windows_native_iroh"


def test_descriptor_and_work_refuse_without_membership_or_consent(tmp_path):
    root = tmp_path / "host"
    with pytest.raises(Exception):
        main(["--root", str(root), "descriptor", "--run-id", "run-one", "--deployment-id", "dep-one",
              "--manifest-digest", "sha256:" + "a" * 64, "--out", str(tmp_path / "card.json")])
    policy = tmp_path / "policy.json"
    policy.write_text(json.dumps(POLICY))
    limits = tmp_path / "limits.json"
    limits.write_text(json.dumps({"max_rss_bytes": 1 << 30, "max_disk_bytes": 100_000_000,
                                  "max_wall_seconds": 600, "max_commands": 100}))
    with pytest.raises(SystemExit, match="--acknowledge"):
        main(["--root", str(root), "run", "--policy", str(policy), "--limits", str(limits),
              "--preview", str(tmp_path / "missing.json")])
