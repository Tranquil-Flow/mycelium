# SPDX-License-Identifier: AGPL-3.0-only
"""A12 mobile rung projection across the eight workspaces.

``project_mobile_rungs`` renders one member's four separately authored rungs and its
roles from one public generation of the rung ledger.  It never infers a rung from
another, never shows non-native evidence as an eligible operation, keeps Apple
members deferred (ruling R2) and carries no endpoint, key or device material.
"""

from __future__ import annotations

import json

import pytest

from mycelium_mobile import a12_activation as a12

ELIGIBLE = {"decision": "eligible", "blockers": [], "source_kind": "native_device_observation"}


def _source(members, *, generation=4, freshness="live"):
   return {"generation": generation, "freshness": freshness, "members": members}


def _android(**overrides):
   member = {
      "member_ref": "member:phone",
      "platform_family": "android",
      "endpoint_id": "a" * 64,
      "rungs": {
         "signed_membership": dict(ELIGIBLE),
         "capability_eligibility": dict(ELIGIBLE),
         "artifact_readiness": {"decision": "not_applicable", "blockers": [], "source_kind": "native_device_observation"},
      },
      "roles": {"probe_contributor": dict(ELIGIBLE, verification_key={"secret": "x"})},
   }
   member.update(overrides)
   return member


@pytest.mark.parametrize("workspace", a12.WORKSPACES)
def test_every_workspace_reconstructs_the_same_generation(workspace):
   projection = a12.project_mobile_rungs(
      workspace=workspace, member_ref="member:phone", source=_source([_android()])
   )
   assert projection["workspace"] == workspace
   assert projection["generation"] == 4
   assert tuple(projection["rungs"]) == a12.RUNG_IDS
   assert tuple(projection["roles"]) == a12.MOBILE_ROLES
   assert projection["route_ready"] is False


def test_rungs_render_only_from_their_own_records():
   projection = a12.project_mobile_rungs(
      workspace="readiness", member_ref="member:phone", source=_source([_android()])
   )
   assert projection["rungs"]["signed_membership"]["decision"] == "eligible"
   assert projection["rungs"]["operation_qualification"] == {
      "decision": "unknown",
      "blockers": ["rung_record_absent"],
      "source_kind": "unknown",
   }
   assert projection["roles"]["qualified_model_stage"]["decision"] == "unknown"


def test_non_native_evidence_never_shows_an_eligible_operation():
   member = _android(
      rungs={
         **_android()["rungs"],
         "operation_qualification": {"decision": "eligible", "blockers": [], "source_kind": "termux_conformance"},
      }
   )
   projection = a12.project_mobile_rungs(workspace="device_lab", member_ref="member:phone", source=_source([member]))
   operation = projection["rungs"]["operation_qualification"]
   assert operation["decision"] == "withheld"
   assert "non_native_source_cannot_qualify" in operation["blockers"]


def test_apple_members_stay_deferred():
   member = _android(member_ref="member:tablet", platform_family="ipados")
   projection = a12.project_mobile_rungs(workspace="nodes", member_ref="member:tablet", source=_source([member]))
   assert projection["platform_state"] == "deferred"
   assert all(item["blockers"] == [a12.APPLE_DEFERRED_CODE] for item in projection["rungs"].values())
   assert all(item["decision"] == "withheld" for item in projection["roles"].values())


def test_the_projection_carries_no_private_material():
   projection = a12.project_mobile_rungs(workspace="network", member_ref="member:phone", source=_source([_android()]))
   encoded = json.dumps(projection)
   assert "endpoint_id" not in encoded
   assert "verification_key" not in encoded
   assert "a" * 64 not in encoded


def test_missing_generation_members_and_bad_inputs_fail_closed():
   with pytest.raises(a12.A12NotImplementedError):
      a12.project_mobile_rungs(workspace="nodes", member_ref="member:phone")
   with pytest.raises(a12.A12ContractError) as absent:
      a12.project_mobile_rungs(workspace="nodes", member_ref="member:other", source=_source([_android()]))
   assert absent.value.code == "mobile_projection_member_absent"
   with pytest.raises(a12.A12ContractError) as stale:
      a12.project_mobile_rungs(
         workspace="nodes", member_ref="member:phone", source=_source([_android()], freshness="current-ish")
      )
   assert stale.value.code == "mobile_projection_freshness_invalid"
   bad = _android(rungs={"signed_membership": {"decision": "healthy", "blockers": [], "source_kind": "native_device_observation"}})
   with pytest.raises(a12.A12ContractError):
      a12.project_mobile_rungs(workspace="nodes", member_ref="member:phone", source=_source([bad]))
