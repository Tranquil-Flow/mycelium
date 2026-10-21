# SPDX-License-Identifier: AGPL-3.0-only
"""The seam's source incident may carry the failure's next cancellation generation.

The route records the scoped incident at whichever cancellation generation is
current when its reader observes the failure: before or after the failure
joins the command to the next generation. The armed binding was captured
while the source executed, so it may trail the incident, never lead it.
"""
from __future__ import annotations

import pytest

from mycelium_a6_product_fault_seams import _incident_identity_matches
from tests.a6_acceptance.test_product_fault_seams import _binding

_IDENTITY = {key: value for key, value in _binding().items() if key not in {
    "source_node_id", "source_attempt", "absolute_deadline_ms", "path_manifest_digest"}}


def _incident(**overrides):
    return {"incident_id": "i-1", "request_id": "r-1", "subject_id": "node-0",
            "scope": "request", "fatal_accepted": False, "reason": "node_process_exited",
            **_IDENTITY, **overrides}


@pytest.mark.parametrize(("incident_generation", "matches"), [
    (0, True), (1, True), (5, True), (-1, False), ("1", False), (None, False)])
def test_incident_generation_may_trail_but_never_precede_the_armed_one(
        incident_generation, matches):
    identity = dict(_IDENTITY, cancellation_generation=0)
    incident = _incident(cancellation_generation=incident_generation)
    assert _incident_identity_matches(incident, identity) is matches


def test_incident_naming_another_command_never_matches():
    identity = dict(_IDENTITY, cancellation_generation=0)
    for field in ("command_id", "request_attempt", "path_digest", "qualification_digest"):
        assert _incident_identity_matches(_incident(**{field: "other"}), identity) is False
    # Fields the incident does not carry are not compared.
    sparse = {key: value for key, value in _incident().items() if key != "command_id"}
    assert _incident_identity_matches(sparse, identity) is True
