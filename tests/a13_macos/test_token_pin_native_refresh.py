# SPDX-License-Identifier: AGPL-3.0-only
"""Already-running native child observes the parent's refreshed private pin.

The expired persisted lease is injected; membership, policy signing, application
constructors, managed TLS and native install are real. Compute remains the
existing CPU fixture. This is not a physical cold-compile or model benchmark.
"""
import json
import time

from mycelium_a13_macos import token_policy
from tests.live.test_token_path_admission import world
from tests.a13_macos import test_token_two_process_join as joined


def test_native_child_installs_after_authenticated_parent_refresh(tmp_path, monkeypatch, world):
    prepare = token_policy.prepare_host_pin
    captured = []

    def cold_pin(**kwargs):
        path = prepare(**kwargs)
        if not captured:
            assert path is not None
            pin = json.loads(path.read_bytes())
            original_deadline = pin['process_deadline_monotonic_s']
            pin['lease_expires_at_unix_ms'] = int(time.time()*1000)-1000
            from mycelium_a13_macos.authority import _atomic_private_json
            _atomic_private_json(path, pin)
            captured.append((path, original_deadline))
        return path

    monkeypatch.setattr(token_policy, 'prepare_host_pin', cold_pin)
    joined.test_owner_native_two_process_route(tmp_path, world, monkeypatch,
        True, 'signed', 'chat', startup=True, app_pair=True)
    assert len(captured) == 1
    pin = json.loads(captured[0][0].read_bytes())
    assert pin['lease_expires_at_unix_ms'] > int(time.time()*1000)
    assert pin['process_deadline_monotonic_s'] == captured[0][1]
