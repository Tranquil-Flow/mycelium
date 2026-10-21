# SPDX-License-Identifier: AGPL-3.0-only
"""Evidence names device classes and capability digests, never hosts."""
from __future__ import annotations

import json

import pytest

from mycelium_gate_requirements import binding as gb
from mycelium_gate_requirements import evidence as ge
from mycelium_gate_requirements import spec as gs

from ._fleet import NOW, macs

HOSTY_NAMES = ("Evis-MacBook-Pro", "astra-sb2", "m4pro")


def _hosty_binding():
    members, reports, synthetic = macs(3, names=HOSTY_NAMES)
    binding = gb.bind_roles(gs.load_pinned_spec("A6"), members=members, reports=reports, now=NOW)
    return binding, members


def test_public_receipt_carries_classes_and_digests_but_no_host_identifier() -> None:
    binding, members = _hosty_binding()
    forbidden = ge.forbidden_values(members)
    assert set(HOSTY_NAMES) <= set(forbidden)
    assert "100.84.0.10:4433" in forbidden and "100.84.0.10" in forbidden
    # The private binding addresses members by node id: the scanner must see that.
    assert ge.host_identifier_findings(binding, forbidden_values=forbidden)
    receipt = ge.public_receipt(binding)
    assert ge.host_identifier_findings(receipt, forbidden_values=forbidden) == []
    text = json.dumps(receipt)
    for name in HOSTY_NAMES:
        assert name not in text
    assert receipt["protocol"] == ge.RECEIPT_PROTOCOL
    assert receipt["binding_digest"] == binding["binding_digest"]
    assert receipt["spec_digest"] == gs.load_pinned_spec("A6").digest
    entry = receipt["roles"]["entry"][0]
    assert set(entry) == {"device_class", "capability_digest", "member_digest"}
    assert entry["device_class"]["os_family"] == "macos"
    assert entry["device_class"]["backends"] == ["mlx"]
    assert entry["device_class"]["memory_class_gib"] == 48
    assert ge.verify_receipt(receipt) == receipt
    ge.assert_host_free(receipt, forbidden_values=forbidden)


@pytest.mark.parametrize("value", [
    "203.0.113.12", "fd7a:115c:a1e0::1", "member@203.0.113.12", "astra@sb2",
    "/Users/member/mycelium", "/home/astra/venv", "C:\\Users\\member", "laptop.local",
    "m4pro.tail1234.ts.net", "/Volumes/Seagate Portable Drive/x",
])
def test_scanner_flags_generic_host_identifiers(value) -> None:
    assert ge.host_identifier_findings({"nested": [{"field": value}]})
    with pytest.raises(ge.HostIdentifierLeak):
        ge.assert_host_free({"field": value})


def test_scanner_does_not_flag_digests_versions_and_class_labels() -> None:
    clean = {"digest": "sha256:" + "a" * 64, "os_version": "26.5.1", "chip": "Apple M4 Pro",
             "label": "macos-arm64-mlx", "protocol": "mycelium.gate_role_binding.v1",
             "ratio": 0.75, "case_id": "no_successor"}
    assert ge.host_identifier_findings(clean) == []
    assert ge.host_identifier_findings({"x": "serial 47031FDJG000RD"},
                                       forbidden_values=("47031FDJG000RD",))


def test_case_evidence_must_carry_the_spec_fields_and_stay_host_free() -> None:
    spec = gs.load_pinned_spec("A7")
    binding, members = _hosty_binding()
    fields = spec.document["evidence"]["per_case_fields"]
    record = {field: "x" for field in fields}
    record["case_id"] = "no_a6_successor_aborts"
    assert ge.check_case_evidence(spec, record) == []
    missing = dict(record)
    missing.pop(fields[-1])
    assert ge.check_case_evidence(spec, missing) == [f"case_evidence_field_missing:{fields[-1]}"]
    leaky = dict(record, note="killed on member@203.0.113.12")
    assert any(item.startswith("case_evidence_host_identifier")
               for item in ge.check_case_evidence(spec, leaky))
    unknown = dict(record, case_id="not_a_case")
    assert "case_evidence_case_unknown" in ge.check_case_evidence(spec, unknown)
