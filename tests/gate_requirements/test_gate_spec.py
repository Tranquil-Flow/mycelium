# SPDX-License-Identifier: AGPL-3.0-only
"""Gate spec format: versioned, strictly validated, digest-pinned."""
from __future__ import annotations

import json
import re
import shutil

import pytest

from mycelium_gate_requirements import spec as gs
from mycelium_gate_requirements.evidence import host_identifier_findings

PINNED = ("A1", "A5", "A6", "A7", "A10", "A11", "A12", "A14")


def test_every_required_gate_has_a_pinned_spec_whose_digest_matches_the_index() -> None:
    assert tuple(gs.PINNED_GATES) == PINNED
    index = json.loads((gs.SPEC_DIR / gs.INDEX_FILE).read_text("utf-8"))
    assert index["protocol"] == gs.INDEX_PROTOCOL
    assert sorted(index["specs"]) == sorted(PINNED)
    for gate_id in PINNED:
        loaded = gs.load_pinned_spec(gate_id)
        assert loaded.gate_id == gate_id
        assert loaded.digest == index["specs"][gate_id]["spec_digest"]
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", loaded.digest)


def test_digest_is_stable_under_key_order_and_whitespace_but_not_under_values() -> None:
    path = gs.SPEC_DIR / "a6.v1.json"
    document = json.loads(path.read_text("utf-8"))
    reordered = json.loads(json.dumps(document, indent=7, sort_keys=False),
                           object_pairs_hook=lambda pairs: dict(reversed(pairs)))
    assert gs.spec_digest(reordered) == gs.spec_digest(document) == gs.load_pinned_spec("A6").digest
    changed = json.loads(json.dumps(document))
    changed["roles"][0]["requirements"]["min_free_disk_bytes"] = 1
    assert gs.spec_digest(changed) != gs.spec_digest(document)


def test_a_tampered_spec_file_is_refused_against_the_pinned_index(tmp_path) -> None:
    for item in gs.SPEC_DIR.iterdir():
        shutil.copy(item, tmp_path / item.name)
    target = tmp_path / "a7.v1.json"
    document = json.loads(target.read_text("utf-8"))
    document["roles"][1]["requirements"]["max_link_rtt_ms"] = 10_000
    target.write_text(json.dumps(document, indent=2), "utf-8")
    with pytest.raises(gs.GateSpecError, match="gate_spec_digest_mismatch"):
        gs.load_pinned_spec("A7", spec_dir=tmp_path)
    assert gs.load_pinned_spec("A6", spec_dir=tmp_path).gate_id == "A6"
    with pytest.raises(gs.GateSpecError, match="gate_spec_unknown"):
        gs.load_pinned_spec("A99")


def _minimal() -> dict:
    return {
        "protocol": gs.SPEC_PROTOCOL, "gate_id": "A7", "spec_version": 1,
        "feature": "x", "claim_boundary": "y",
        "model": {"model_id": "m", "weight_bytes": 1000, "layer_count": 4, "weight_bytes_source": "s"},
        "resource_policy": {"usable_memory_fraction": 0.75, "memory_reserve_bytes": 0,
                            "kv_headroom_fraction": 0.0, "disk_reserve_bytes": 0},
        "freshness": {"max_report_age_seconds": 600},
        "roles": [{"role": "entry", "count": {"min": 1, "max": 1}, "fit": "full_model",
                   "requirements": {"os_families": ["macos"]},
                   "may_share_member_with": [], "same_backend_as": None}],
        "cases": [{"case_id": "c1", "requires_roles": {"entry": 1}}],
        "evidence": {"per_case_fields": ["case_id"], "binding_fields": ["device_class"]},
    }


@pytest.mark.parametrize(("mutate", "code"), [
    (lambda d: d.update(extra=1), "gate_spec_fields_invalid"),
    (lambda d: d.update(protocol="mycelium.other.v1"), "gate_spec_protocol_invalid"),
    (lambda d: d.update(gate_id="node-0"), "gate_spec_gate_id_invalid"),
    (lambda d: d["roles"].append(dict(d["roles"][0])), "gate_spec_role_duplicate"),
    (lambda d: d["roles"][0].update(count={"min": 2, "max": 1}), "gate_spec_role_count_invalid"),
    (lambda d: d["roles"][0].update(may_share_member_with=["ghost"]), "gate_spec_role_reference_invalid"),
    (lambda d: d["roles"][0].update(same_backend_as="ghost"), "gate_spec_role_reference_invalid"),
    (lambda d: d["roles"][0]["requirements"].update(ssh_target="x"), "gate_spec_requirement_unknown"),
    (lambda d: d["roles"][0]["requirements"].update(os_families=["beos"]), "gate_spec_requirement_invalid"),
    (lambda d: d["roles"][0].update(fit={"fraction_of_model": 2}), "gate_spec_role_fit_invalid"),
    (lambda d: d.update(model=None), "gate_spec_fit_requires_model"),
    (lambda d: d["cases"][0].update(requires_roles={"ghost": 1}), "gate_spec_case_invalid"),
    (lambda d: d["cases"][0].update(requires_roles={"entry": 2}), "gate_spec_case_invalid"),
])
def test_invalid_specs_fail_closed_with_named_codes(mutate, code) -> None:
    document = _minimal()
    assert gs.parse_spec(document).gate_id == "A7"
    mutate(document)
    with pytest.raises(gs.GateSpecError, match=code):
        gs.parse_spec(document)


def test_pinned_specs_name_requirements_never_hosts() -> None:
    for gate_id in PINNED:
        document = gs.load_pinned_spec(gate_id).document
        text = json.dumps(document)
        assert host_identifier_findings(document) == [], gate_id
        assert re.search(r"node-\d", text) is None, gate_id
        for forbidden in ("ssh", "Seagate", "laptop", "SB2", "Pixel", "evi" + "nova", "astra"):
            assert forbidden.lower() not in text.lower(), (gate_id, forbidden)
