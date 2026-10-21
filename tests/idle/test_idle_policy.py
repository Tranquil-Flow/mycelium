# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the owner/member idle-unload setting (default 10 minutes, 'never' allowed)."""
from __future__ import annotations

import json
import os

import pytest

from mycelium_idle import policy as ip


def test_default_is_ten_minutes_when_no_file(tmp_path):
    value = ip.read_policy(tmp_path)
    assert value.unload_after_minutes == 10 and value.unload_after_s == 600.0
    assert not (tmp_path / ip.POLICY_FILENAME).exists()


def test_never_is_allowed_and_means_no_deadline(tmp_path):
    ip.write_policy(tmp_path, "never")
    value = ip.read_policy(tmp_path)
    assert value.unload_after_minutes is None and value.unload_after_s is None
    assert json.loads((tmp_path / ip.POLICY_FILENAME).read_text())["unload_after_minutes"] == "never"


@pytest.mark.parametrize("raw,minutes", [(1, 1), (10, 10), ("15", 15), (1440, 1440), ("never", None)])
def test_parse_setting_accepts_minutes_or_never(raw, minutes):
    assert ip.parse_setting(raw) == minutes


@pytest.mark.parametrize("raw", [0, -1, 1441, 1.5, True, None, "", "soon", "0", [10], {"m": 10}])
def test_parse_setting_refuses_everything_else(raw):
    with pytest.raises(ip.IdlePolicyError) as error:
        ip.parse_setting(raw)
    assert error.value.code == "idle_policy_invalid"


def test_write_is_private_atomic_and_round_trips(tmp_path):
    ip.write_policy(tmp_path, 25)
    path = tmp_path / ip.POLICY_FILENAME
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert ip.read_policy(tmp_path).unload_after_minutes == 25
    assert not [p for p in tmp_path.iterdir() if p.name != ip.POLICY_FILENAME]


def test_unreadable_policy_is_a_named_error_not_a_default(tmp_path):
    path = tmp_path / ip.POLICY_FILENAME
    path.write_text("{not json")
    path.chmod(0o600)
    with pytest.raises(ip.IdlePolicyError):
        ip.read_policy(tmp_path)
    path.write_text(json.dumps({"schema": 1, "unload_after_minutes": 10, "extra": 1}))
    with pytest.raises(ip.IdlePolicyError):
        ip.read_policy(tmp_path)
    path.write_text(json.dumps({"schema": 1, "unload_after_minutes": 10}))
    path.chmod(0o644)
    with pytest.raises(ip.IdlePolicyError):
        ip.read_policy(tmp_path)


def test_symlinked_policy_is_refused(tmp_path):
    target = tmp_path / "real.json"
    target.write_text(json.dumps({"schema": 1, "unload_after_minutes": 5}))
    target.chmod(0o600)
    os.symlink(target, tmp_path / ip.POLICY_FILENAME)
    with pytest.raises(ip.IdlePolicyError):
        ip.read_policy(tmp_path)


def test_node_side_reader_fails_safe_to_never_on_a_bad_file(tmp_path):
    """The node must keep serving when the file is broken: unreadable == do not unload."""
    (tmp_path / ip.POLICY_FILENAME).write_text("garbage")
    assert ip.unload_after_s_or_never(tmp_path) is None
    ip.write_policy(tmp_path, 3)
    assert ip.unload_after_s_or_never(tmp_path) == 180.0
