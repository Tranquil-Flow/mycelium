# SPDX-License-Identifier: AGPL-3.0-only
"""Load-provenance and load-authority refusals at every promotion seam.

Synthetic member signatures in the native shapes; no phone readiness. The v1
descriptor is the retired producer's (it echoed the controller's generation);
the v2 descriptor is the native-observed one. Only the exact v2 form is ever
promoted, and never without current seed/packaged authority.
"""
import copy
import json
from types import SimpleNamespace

import pytest
from mycelium_qualification import phone as load
from mycelium_qualification.phone import phone_load_proof_digest
from mycelium_qualification import LiveRouteQualificationError
from scripts import qwen_phone_builder as phone
from test_phone_live_authority import (
    LOAD, fixture_authority, legacy_phone_digest, phone_attestation, phone_records, qualify)
from test_qwen_phone_builder import evidence, synthetic_snapshot, admit

ERROR = "phone_load_provenance_unsupported"
VARIANTS = ["v1", "missing", "malformed", "unknown", "applied", "claims", "basis", "no_basis", "verified"]


def marker(variant):
    m = copy.deepcopy(load.LOAD_IDENTITY_PROVENANCE_V2)
    if variant == "v1": return copy.deepcopy(load.LOAD_IDENTITY_PROVENANCE_V1)
    if variant == "missing": return None
    if variant == "malformed": return "observed"
    if variant == "unknown": m["limitation"] = "independently_observed"
    if variant == "applied": m["applied_fields"].append("manifest_digest")
    if variant == "claims": m["controller_claim_fields"] = []
    if variant == "basis": m["load_generation_basis"] = "seed_route_generation"
    if variant == "no_basis": m.pop("load_generation_basis")
    if variant == "verified": m["independently_bound"] = True
    return m


def remark(envelope, signer, variant):
    o = envelope["observation"]
    o["details"].pop("load_identity_provenance", None)
    if marker(variant) is not None: o["details"]["load_identity_provenance"] = marker(variant)
    envelope["signature"] = signer.sign(o)
    return o


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("seam", ["digest", "graph", "checkpoint", "retired_checkpoint", "qualifier"])
def test_unsupported_provenance_never_promotes(variant, seam):
    att, bindings, signers = phone_attestation()
    n = next(iter(bindings))
    envelope = phone_records(att, n, "configured")[0]
    o = remark(envelope, signers[n], variant)
    before = copy.deepcopy(envelope)
    with pytest.raises((ValueError, LiveRouteQualificationError)) as refused:
        if seam == "digest": phone_load_proof_digest(o)
        elif seam == "qualifier": qualify(att, bindings)
        elif seam == "checkpoint":
            phone.preparation_proof_digest(dict(protocol=phone.PROOF_PROTOCOL, configured=dict(result=envelope)))
        elif seam == "retired_checkpoint":
            phone.preparation_proof_digest(dict(protocol="mycelium.phone_authenticated_load_proof.v1",
                                                configured=dict(result=envelope)))
        else:
            from test_router_inprocess_mesh import three_device_graph
            graph = three_device_graph()
            node = graph.stages[0].placements[0].node_id
            phone.bind_phone_graph(graph, {node: SimpleNamespace(observations=[o])})
    assert str(refused.value) == ERROR
    assert envelope == before


def test_retired_proof_protocol_is_refused_even_around_a_v2_record():
    att, bindings, _ = phone_attestation()
    envelope = phone_records(att, next(iter(bindings)), "configured")[0]
    assert phone.preparation_proof_digest(dict(protocol=phone.PROOF_PROTOCOL, configured=dict(result=envelope))) \
        == phone_load_proof_digest(envelope["observation"])
    with pytest.raises(ValueError, match=ERROR):
        phone.preparation_proof_digest(dict(protocol="mycelium.phone_authenticated_load_proof.v1",
                                            configured=dict(result=envelope)))


@pytest.mark.parametrize("field,value", [("load_generation", 999), ("manifest_digest", "sha256:"+"b"*64)])
def test_coherent_v1_echo_laundering_still_refuses(field, value):
    """The controller picks a value and re-signs every host-side copy consistently.

    A producer that merely echoes (v1) can be made to agree with anything, so
    agreement is not evidence: the record is refused for what it is.
    """
    att, bindings, signers = phone_attestation()
    n = next(iter(bindings))
    for e in att["signed_observations"]:
        o = e["observation"]
        if o["node_id"] == n and o["event"] == "configured":
            o["details"].update(load_identity_provenance=copy.deepcopy(load.LOAD_IDENTITY_PROVENANCE_V1))
            o["details"][field] = value
            e["signature"] = signers[n].sign(o)
            att["execution_graph"]["stages"][1]["placements"][0]["load_proof_digest"] = legacy_phone_digest(o)
        if o["node_id"] == n and o["event"] == "snapshot" and field == "load_generation":
            o["details"]["load_identity"]["load_generation"] = value
            o["details"]["configured_load_generation"] = value
            e["signature"] = signers[n].sign(o)
    bindings[n]["configure"][field] = value
    if field == "manifest_digest":
        att["execution_graph"]["manifest_digest"] = value
        for e in att["signed_observations"]:
            if e["observation"]["event"] == "configured":
                e["observation"]["details"][field] = value
                e["signature"] = signers[e["observation"]["node_id"]].sign(e["observation"])
    with pytest.raises(LiveRouteQualificationError) as refused:
        qualify(json.loads(json.dumps(att)), bindings)
    assert str(refused.value) == ERROR


@pytest.mark.parametrize("binding", ["plan_copy", "mapping", "verified_flag", "namespace", "absent"])
def test_final_qualifier_requires_validated_authority_not_a_claim_of_it(binding):
    """API bypass: the old binding shape, a look-alike or a flag is not authority."""
    att, bindings, _ = phone_attestation()
    n = next(iter(bindings))
    authority = bindings[n]["load_authority"]
    fields = {k: getattr(authority, k) for k in authority.__dataclass_fields__ if k != "_seal"}
    if binding in ("plan_copy", "absent"): bindings[n].pop("load_authority")
    if binding == "plan_copy": bindings[n]["configure"] = dict(bindings[n]["configure"])
    elif binding == "mapping": bindings[n]["load_authority"] = dict(fields)
    elif binding == "verified_flag": bindings[n].update(load_authority=True, verified=True, source_verified=True)
    elif binding == "namespace": bindings[n]["load_authority"] = SimpleNamespace(**fields, expected_details=authority.expected_details)
    with pytest.raises(LiveRouteQualificationError) as refused:
        qualify(att, bindings)
    assert str(refused.value) == "live_phone_load_authority_required"
    with pytest.raises(TypeError, match="phone_load_authority_unsealed"):
        load.PhoneLoadAuthority(**fields)


@pytest.mark.parametrize("field,value", [
    ("assignment_id", "assignment-B"), ("load_generation", LOAD+1), ("pack_digest", "sha256:"+"0"*64),
    ("manifest_digest", "sha256:"+"0"*64), ("runtime_build_digest", "sha256:"+"0"*64),
    ("golden_output_digest", "sha256:"+"0"*64), ("process_id", "inc-restarted"),
    ("member_key_digest", "sha256:"+"0"*64), ("load_proof_digest", "sha256:"+"0"*64),
    ("endpoint_id", "ff"*32), ("node_id", "another-node")])
def test_native_record_must_equal_current_authority_exactly(field, value):
    """The seed's current authority moved (replacement, reload, new key or incarnation)."""
    att, bindings, _ = phone_attestation()
    n = next(iter(bindings))
    configured = phone_records(att, n, "configured")[0]["observation"]
    bindings[n]["load_authority"] = fixture_authority(configured,
        **{"member_key_digest": bindings[n]["member_key_digest"], field: value})
    with pytest.raises(LiveRouteQualificationError) as refused:
        qualify(att, bindings)
    assert str(refused.value) == "live_phone_load_authority_mismatch"


@pytest.mark.parametrize("attack", ["reloaded", "replaced", "unloaded", "no_identity", "unconfigured", "bool"])
def test_old_configured_record_is_refused_against_a_newer_snapshot(attack):
    """Stale generation: the configured signature is intact and matches authority."""
    att, bindings, signers = phone_attestation()
    n = next(iter(bindings))
    envelope = phone_records(att, n, "snapshot")[0]
    d = envelope["observation"]["details"]
    if attack == "reloaded": d["load_identity"]["load_generation"] = LOAD+1
    elif attack == "replaced": d["load_identity"]["assignment_id"] = "assignment-B"
    elif attack == "unloaded": d["load_identity"] = None
    elif attack == "no_identity": d.pop("load_identity")
    elif attack == "unconfigured": d["configured_load_generation"] = None
    else: d["configured_load_generation"] = True
    envelope["signature"] = signers[n].sign(envelope["observation"])
    with pytest.raises(LiveRouteQualificationError) as refused:
        qualify(att, bindings)
    assert str(refused.value) == "live_phone_load_stale"


def test_tampered_native_record_is_a_signature_refusal_not_a_provenance_one():
    att, bindings, _ = phone_attestation()
    n = next(iter(bindings))
    phone_records(att, n, "configured")[0]["observation"]["details"]["load_generation"] = LOAD+1
    with pytest.raises(LiveRouteQualificationError) as refused:
        qualify(att, bindings)
    assert str(refused.value) == "live_signature_invalid"


@pytest.mark.parametrize("variant", VARIANTS)
def test_builder_rejects_unsupported_provenance_before_preparation(evidence, variant):
    doc, _, member = evidence
    remark(doc["configured"]["result"], member, variant)
    original = copy.deepcopy(doc)
    with pytest.raises(ValueError) as refused:
        admit(evidence)
    assert str(refused.value) == ERROR
    assert doc == original


@pytest.mark.parametrize("attack", ["reloaded", "no_identity"])
def test_builder_refuses_preparation_evidence_whose_snapshot_shows_another_load(evidence, attack):
    doc, _, member = evidence
    reply = doc["snapshot"]["result"]
    d = reply["observation"]["details"]
    if attack == "reloaded": d["load_identity"]["load_generation"] += 1
    else: d.pop("load_identity")
    reply["signature"] = member.sign(reply["observation"])
    with pytest.raises(RuntimeError, match="phone_load_stale"):
        admit(evidence)


def test_seed_route_generation_is_recorded_and_never_equated_with_the_load(evidence):
    doc, seed, _ = evidence
    e = admit(evidence)
    binding = e.statement["preparation_binding"]
    assert binding["route_generation"] != e.native_load_generation
    assert binding["provenance"] == phone.PREPARATION_PROVENANCE
    assert "not_native_observation" not in binding["provenance"]
    # The retired statement, which asserted route generation == load generation.
    statement = doc["authorization"]["message"]
    statement["preparation_binding"]["provenance"] = "seed_compiled_assignment_and_route_generation_not_native_observation"
    doc["authorization"]["signature"] = seed.sign(statement)
    with pytest.raises(RuntimeError, match="phone_preparation_binding_mismatch"):
        admit(evidence)
    statement["preparation_binding"]["provenance"] = phone.PREPARATION_PROVENANCE
    statement["protocol"] = "mycelium.phone_builder_authorization.v1"
    doc["authorization"]["signature"] = seed.sign(statement)
    with pytest.raises(RuntimeError, match="phone_authority_invalid"):
        admit(evidence)


def test_final_authority_ignores_the_preparation_window_only_when_told(evidence):
    """The 600 s window is a preparation check; content and signature still bind."""
    import time
    doc, seed, _ = evidence
    kw = dict(seed_key_digest=seed.verification_key_digest, node=__import__("test_qwen_phone_builder").topology()[1],
              run_id="synthetic-run", deployment_id="11111111-1111-4111-8111-111111111111")
    later = time.time() + 3600
    with pytest.raises(RuntimeError, match="phone_authority_stale"):
        phone.PhoneEvidence(doc, now=later, **kw)
    assert phone.PhoneEvidence(doc, now=later, preparation_window=False, **kw).native_load_generation == 3
    with pytest.raises(RuntimeError, match="phone_authority_stale"):  # beyond the clock allowance
        phone.PhoneEvidence(doc, now=doc["authorization"]["message"]["issued_at"] - 31, preparation_window=False, **kw)
    doc["authorization"]["message"]["node_id"] = "another"
    with pytest.raises(RuntimeError, match="phone_authority_signature_invalid"):
        phone.PhoneEvidence(doc, now=later, preparation_window=False, **kw)


@pytest.mark.parametrize("field,value", [("load_generation", 99), ("manifest_digest", "sha256:"+"0"*64),
                                         ("assignment_id", "assignment-B"), ("checked_at", 1.0)])
def test_authority_seal_binds_its_content(field, value):
    """A copied-and-changed or hand-assembled authority is not the issuer's result."""
    import dataclasses
    att, bindings, _ = phone_attestation()
    n = next(iter(bindings))
    authority = bindings[n]["load_authority"]
    assert "_seal" not in repr(authority) and authority._seal not in repr(authority)
    assert dataclasses.replace(authority) == authority  # an unchanged copy is the same content
    with pytest.raises(TypeError, match="phone_load_authority_unsealed"):
        dataclasses.replace(authority, **{field: value})
    fields = {k: getattr(authority, k) for k in authority.__dataclass_fields__}
    with pytest.raises(TypeError, match="phone_load_authority_unsealed"):
        load.PhoneLoadAuthority(**{**fields, field: value})
    # Assembled without the constructor, carrying the genuine seal of other content.
    forged = object.__new__(load.PhoneLoadAuthority)
    for key, item in {**fields, field: value}.items():
        object.__setattr__(forged, key, item)
    assert type(forged) is load.PhoneLoadAuthority
    # Make the forgery agree with the native record so only the seal can refuse it.
    if field == "checked_at":
        bindings[n]["load_authority"] = forged
        with pytest.raises(LiveRouteQualificationError) as refused:
            qualify(att, bindings)
        assert str(refused.value) == "live_phone_load_authority_required"
    with pytest.raises(TypeError, match="phone_load_authority_unsealed"):
        forged.verify_seal()
    bindings[n]["load_authority"] = authority
    assert qualify(att, bindings).route_ready is True
