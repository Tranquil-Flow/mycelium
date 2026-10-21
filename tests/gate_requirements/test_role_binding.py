# SPDX-License-Identifier: AGPL-3.0-only
"""Role binding from seed membership + signed capability reports."""
from __future__ import annotations

import json
import random
import sqlite3

import pytest

from mycelium_gate_requirements import binding as gb
from mycelium_gate_requirements import spec as gs

from ._fleet import GIB, NOW, SyntheticMember, macs


def _bind(gate_id, members, reports, **kwargs):
    return gb.bind_roles(gs.load_pinned_spec(gate_id), members=members, reports=reports,
                         now=NOW, **kwargs)


def _nodes(binding, role):
    return gb.role_node_ids(binding, role)


def test_two_macs_bind_a7_entry_source_and_a_standby_off_the_source() -> None:
    members, reports, _ = macs(2)
    binding = _bind("A7", members, reports)
    assert binding["protocol"] == gb.BINDING_PROTOCOL and binding["gate_id"] == "A7"
    assert binding["spec_digest"] == gs.load_pinned_spec("A7").digest
    # Ranked by usable memory: mac-0 (48 GiB) before mac-1 (40 GiB).
    assert _nodes(binding, "entry") == ["mac-0"]
    assert _nodes(binding, "source") == ["mac-1"]
    # The standby must not be the source; the spec lets it share the entry's member.
    assert _nodes(binding, "standby") == ["mac-0"]
    assert binding["placements"] == ["mac-0", "mac-1"]
    assert all(case["bindable"] for case in binding["cases"].values())
    assert gb.verify_binding(binding, spec=gs.load_pinned_spec("A7")) == binding


def test_binding_is_deterministic_regardless_of_input_order() -> None:
    members, reports, _ = macs(5)
    first = _bind("A6", members, reports)
    shuffled_members, shuffled_reports = list(members), list(reports)
    random.Random(7).shuffle(shuffled_members)
    random.Random(11).shuffle(shuffled_reports)
    assert _bind("A6", shuffled_members, shuffled_reports) == first


def test_three_macs_give_a6_two_disjoint_successors_and_full_case_coverage() -> None:
    members, reports, _ = macs(3)
    binding = _bind("A6", members, reports)
    assert _nodes(binding, "entry") == ["mac-0"]
    assert _nodes(binding, "successor") == ["mac-1", "mac-2"]
    assert binding["placements"] == ["mac-0", "mac-1", "mac-2"]
    assert binding["cases"]["exhausted_recovery_attempts"] == {"bindable": True, "reason": None}


def test_two_macs_bind_a6_but_name_the_case_that_needs_a_third_placement() -> None:
    members, reports, _ = macs(2)
    binding = _bind("A6", members, reports)
    assert _nodes(binding, "successor") == ["mac-1"]
    assert binding["cases"]["exhausted_recovery_attempts"] == {
        "bindable": False, "reason": "requires_roles:successor>=2"}
    assert binding["cases"]["ordinary_browser_source_failure_recovery"]["bindable"] is True


def test_five_mixed_members_bind_a5_and_a12_by_class() -> None:
    mac_members, mac_reports, _ = macs(3)
    linux = SyntheticMember("linux-a", peer_class="linux_native_iroh")
    phone = SyntheticMember("phone-a", peer_class="android_native_iroh")
    members = mac_members + [linux.seed_record(), phone.seed_record()]
    reports = mac_reports + [linux.v2_report(sustainable_gib=12), phone.v2_report()]
    a5 = _bind("A5", members, reports)
    assert _nodes(a5, "stage0") == ["mac-0"]
    assert len(_nodes(a5, "replica")) == 2 and "mac-0" not in _nodes(a5, "replica")
    assert "phone-a" not in a5["placements"]
    a12 = _bind("A12", members, reports)
    assert _nodes(a12, "phone") == ["phone-a"]
    assert _nodes(a12, "entry") == ["mac-0"]
    assert _nodes(a12, "final") and _nodes(a12, "final")[0] not in {"mac-0", "phone-a"}
    phone_class = a12["roles"]["phone"][0]["device_class"]
    assert phone_class["os_family"] == "android" and phone_class["peer_class"] == "android_native_iroh"


def _refusal(gate_id, members, reports):
    with pytest.raises(gb.GateRequirementsUnsatisfied) as caught:
        _bind(gate_id, members, reports)
    return caught.value


def test_a_single_mac_cannot_satisfy_a7_and_the_refusal_names_the_joint_problem() -> None:
    members, reports, _ = macs(1)
    refusal = _refusal("A7", members, reports)
    assert refusal.reasons == ["requirements_unsatisfied:joint_assignment"]
    assert refusal.detail["joint_assignment"]["roles"] == ["entry", "source", "standby"]
    assert refusal.detail["joint_assignment"]["first_unfillable_role"] == "source"
    assert str(refusal) == "requirements_unsatisfied:joint_assignment"


def test_refusals_name_the_role_and_every_member_reason() -> None:
    small = SyntheticMember("mac-small")
    no_mlx = SyntheticMember("mac-nomlx")
    hot = SyntheticMember("mac-hot")
    far = SyntheticMember("mac-far")
    stale = SyntheticMember("mac-stale")
    draining = SyntheticMember("mac-draining", lifecycle_state="DRAINING")
    lapsed = SyntheticMember("mac-lapsed", lease_expires_at=NOW - 1)
    silent = SyntheticMember("mac-silent")
    forged = SyntheticMember("mac-forged")
    old_generation = SyntheticMember("mac-oldgen")
    impostor = SyntheticMember("impostor")
    windows = SyntheticMember("win-a", peer_class="windows_native_iroh")
    members = [m.seed_record() for m in (small, no_mlx, hot, far, stale, draining, lapsed,
                                          silent, forged, old_generation, windows)]
    reports = [
        small.mac_report(memory_gib=8),
        no_mlx.mac_report(mlx=False),
        hot.mac_report(thermal="serious"),
        far.mac_report(rtt_ms=900.0),
        stale.mac_report(observed_at=NOW - 3600),
        draining.mac_report(), lapsed.mac_report(),
        impostor.mac_report(node_id="mac-forged"),
        old_generation.mac_report(generation=2),
        windows.v2_report(sustainable_gib=64),
    ]
    refusal = _refusal("A10", members, reports)
    assert refusal.reasons == ["requirements_unsatisfied:entry", "requirements_unsatisfied:final"]
    rejected = {refusal.members[digest]: reasons
                for digest, reasons in refusal.detail["entry"]["rejected"].items()}
    assert refusal.detail["entry"]["needed"] == 1 and refusal.detail["entry"]["eligible"] == 0
    assert rejected == {
        "mac-small": ["usable_memory_below_requirement"],
        "mac-nomlx": ["backend_not_allowed"],
        "mac-hot": ["thermal_state_not_allowed"],
        "mac-far": ["link_rtt_above_bound"],
        "mac-stale": ["capability_report_expired"],
        "mac-draining": ["member_not_current"],
        "mac-lapsed": ["member_not_current"],
        "mac-silent": ["capability_report_missing"],
        "mac-forged": ["capability_report_signature_invalid"],
        "mac-oldgen": ["capability_report_generation_mismatch"],
        "win-a": ["os_family_not_allowed", "backend_not_allowed", "link_measurement_missing"],
    }
    # The public refusal never carries the node ids, only member digests.
    assert "mac-small" not in json.dumps(refusal.detail)


def test_binding_digest_and_spec_pin_are_verified() -> None:
    members, reports, _ = macs(2)
    binding = _bind("A7", members, reports)
    tampered = json.loads(json.dumps(binding))
    tampered["roles"]["source"][0]["node_id"] = "mac-9"
    with pytest.raises(gb.GateBindingError, match="gate_role_binding_digest_mismatch"):
        gb.verify_binding(tampered)
    with pytest.raises(gb.GateBindingError, match="gate_role_binding_spec_mismatch"):
        gb.verify_binding(binding, spec=gs.load_pinned_spec("A6"))


def test_seed_database_rows_are_read_as_member_records(tmp_path) -> None:
    members, reports, _ = macs(2)
    database = tmp_path / "state.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute(
        "CREATE TABLE seed_members (node_id TEXT PRIMARY KEY, endpoint_id TEXT, "
        "endpoint_addrs_json TEXT, peer_class TEXT, runtime_capability_json TEXT, "
        "verification_key_digest TEXT, incarnation TEXT, generation INTEGER, "
        "lease_expires_at REAL, last_heartbeat_sequence INTEGER, last_liveness_at REAL, "
        "next_heartbeat_due_at REAL, last_activity_receipt_at REAL, active_requests INTEGER, "
        "lifecycle_state TEXT)")
    for record in members:
        connection.execute(
            "INSERT INTO seed_members VALUES (?,?,?,?,?,?,?,?,?,0,0,0,NULL,0,?)",
            (record["node_id"], record["endpoint_id"], json.dumps(record["endpoint_addrs"]),
             record["peer_class"], json.dumps(record["runtime_capability"]),
             record["verification_key_digest"], record["incarnation"], record["generation"],
             record["lease_expires_at"], record["lifecycle_state"]))
    connection.commit()
    connection.close()
    loaded = gb.members_from_seed_database(database)
    assert loaded == sorted(members, key=lambda item: item["node_id"])
    assert _bind("A7", loaded, reports) == _bind("A7", members, reports)


def test_usable_memory_follows_the_spec_resource_policy() -> None:
    spec = gs.load_pinned_spec("A10")
    policy = spec.document["resource_policy"]
    member = SyntheticMember("mac-x")
    facts = gb.member_facts(spec, member.seed_record(), [member.mac_report(memory_gib=16)], now=NOW)
    expected = int(16 * GIB * policy["usable_memory_fraction"]) - policy["memory_reserve_bytes"]
    assert facts["usable_memory_bytes"] == expected
    assert facts["capability_digest"].startswith("sha256:")


def test_every_pinned_gate_dry_runs_against_one_synthetic_inventory() -> None:
    mac_members, mac_reports, _ = macs(3)
    phone = SyntheticMember("phone-a", peer_class="android_native_iroh")
    members = mac_members + [phone.seed_record()]
    reports = mac_reports + [phone.v2_report()]
    for gate_id in gs.PINNED_GATES:
        binding = _bind(gate_id, members, reports)
        assert gb.verify_binding(binding, spec=gs.load_pinned_spec(gate_id)) == binding
        assert all(case["bindable"] for case in binding["cases"].values()), gate_id
