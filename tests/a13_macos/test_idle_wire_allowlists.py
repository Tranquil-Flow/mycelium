# SPDX-License-Identifier: AGPL-3.0-or-later
"""PV25: the owner may ask a member to reload (idle_reload) over the managed channel; nothing else idle-related."""
from __future__ import annotations

import pytest

from mycelium_a13_macos import managed_control as mc
from mycelium_a13_macos import worker as wk


def _command(name, payload):
    return {"protocol": mc.CONTROL_COMMAND_PROTOCOL, "command_id": "c-1", "run_id": "run-1",
            "deployment_id": "dep-1", "command": name, "payload": payload}


def test_idle_reload_is_a_closed_empty_payload_operation_on_the_managed_channel():
    assert mc.validate_native_command(_command("idle_reload", {}))["command"] == "idle_reload"
    with pytest.raises(mc.ManagedControlError):
        mc.validate_native_command(_command("idle_reload", {"unload": True}))


def test_the_owner_cannot_unload_or_set_policy_remotely():
    for name in ("idle_unload", "idle_status", "idle_policy"):
        with pytest.raises(mc.ManagedControlError):
            mc.validate_native_command(_command(name, {}))


def test_the_worker_forwards_idle_reload_only_with_an_empty_payload():
    assert frozenset() in wk._OPERATION_PAYLOAD_FIELDS["idle_reload"]
    assert "idle_unload" not in wk._ALLOWED and "idle_status" not in wk._ALLOWED
