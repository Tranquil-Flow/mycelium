"""R5 refusal at all promotion seams; synthetic signatures, no phone readiness."""
import copy
from types import SimpleNamespace

import pytest
from mycelium_qualification.phone import phone_load_proof_digest
from mycelium_qualification import LiveRouteQualificationError
from scripts import qwen_phone_builder as phone
from test_phone_live_authority import phone_attestation, qualify, legacy_phone_digest
from test_qwen_phone_builder import evidence, synthetic_snapshot, admit

ERROR = "phone_load_provenance_unsupported"


def marker(variant):
    m = copy.deepcopy(phone.LOAD_IDENTITY_PROVENANCE)
    if variant == "missing": return None
    if variant == "malformed": return "observed"
    if variant == "unknown": m["limitation"] = "independently_observed"
    if variant == "applied": m["applied_fields"].append("load_generation")
    if variant == "claims": m["controller_claim_fields"] = []
    if variant == "verified": m["independently_bound"] = True
    return m


@pytest.mark.parametrize("variant", ["native", "missing", "malformed", "unknown", "applied", "claims", "verified"])
@pytest.mark.parametrize("seam", ["digest", "graph", "checkpoint", "qualifier"])
def test_unsupported_provenance_never_promotes(variant, seam):
    att, bindings, signers = phone_attestation()
    n = next(iter(bindings))
    envelope = next(e for e in att["signed_observations"]
                    if e["observation"]["node_id"] == n and e["observation"]["event"] == "configured")
    o = envelope["observation"]
    if marker(variant) is not None: o["details"]["load_identity_provenance"] = marker(variant)
    envelope["signature"] = signers[n].sign(o)
    before = copy.deepcopy(envelope)
    with pytest.raises((ValueError, LiveRouteQualificationError), match=ERROR):
        if seam == "digest": phone_load_proof_digest(o)
        elif seam == "qualifier": qualify(att, bindings)
        elif seam == "checkpoint":
            phone.preparation_proof_digest(dict(protocol="mycelium.phone_authenticated_load_proof.v1",
                                                configured=dict(result=envelope)))
        else:
            from test_router_inprocess_mesh import three_device_graph
            graph = three_device_graph()
            node = graph.stages[0].placements[0].node_id
            phone.bind_phone_graph(graph, {node: SimpleNamespace(observations=[o])})
    assert envelope == before


@pytest.mark.parametrize("field,value", [("load_generation", 999), ("manifest_digest", "sha256:"+"b"*64)])
def test_coherent_controller_echo_and_reloaded_artifact_still_refuse(field, value):
    import json
    att, bindings, signers = phone_attestation()
    n = next(iter(bindings))
    for e in att["signed_observations"]:
        o = e["observation"]
        if o["node_id"] == n and o["event"] == "configured":
            o["details"].update(load_identity_provenance=copy.deepcopy(phone.LOAD_IDENTITY_PROVENANCE))
            o["details"][field] = value
            e["signature"] = signers[n].sign(o)
            att["execution_graph"]["stages"][1]["placements"][0]["load_proof_digest"] = legacy_phone_digest(o)
    bindings[n]["configure"][field] = value
    if field == "manifest_digest":
        att["execution_graph"]["manifest_digest"] = value
        for e in att["signed_observations"]:
            if e["observation"]["event"] == "configured":
                e["observation"]["details"][field] = value
                e["signature"] = signers[e["observation"]["node_id"]].sign(e["observation"])
    with pytest.raises(LiveRouteQualificationError, match=ERROR):
        qualify(json.loads(json.dumps(att)), bindings)


@pytest.mark.parametrize("variant", ["missing", "malformed", "unknown", "applied", "claims", "verified"])
def test_builder_rejects_malformed_provenance_before_preparation(evidence, variant):
    doc, _, member = evidence
    reply = doc["configured"]["result"]
    details = reply["observation"]["details"]
    details.pop("load_identity_provenance")
    if marker(variant) is not None: details["load_identity_provenance"] = marker(variant)
    reply["signature"] = member.sign(reply["observation"])
    original = copy.deepcopy(doc)
    with pytest.raises(ValueError, match=ERROR):
        admit(evidence)
    assert doc == original


def test_phone_evidence_preserves_preliminary_authority_but_refuses_proof(evidence, synthetic_snapshot):
    before = copy.deepcopy(evidence[0])
    e = admit(evidence)  # B1/B3 preliminary verification is not ready load authority.
    with pytest.raises((ValueError, RuntimeError), match=ERROR):
        e.bind_assignment(copy.deepcopy(e.statement["preparation_binding"]["assignment"]),
                          stage_id="stage-001", snapshot_root=synthetic_snapshot[0])
    assert evidence[0] == before
    assert e.document == before
