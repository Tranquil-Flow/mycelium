"""Authoritative executable contract registry used by generators and audit."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContractSpec:
    fixture_name: str
    protocol: str
    owner_sources: tuple[str, ...]


CONTRACT_SPECS = (
    ContractSpec(
        "route-plan-v2.json",
        "mycelium.route_plan.v2",
        (
            "mycelium_layer_planner/contracts.py",
            "mycelium_layer_planner/serialization.py",
            "mycelium_layer_planner/validation.py",
        ),
    ),
    ContractSpec(
        "manual-provisioning-route-v1.json",
        "mycelium.manual_provisioning_route.v1",
        ("route_contract.py",),
    ),
    ContractSpec(
        "layer-assignment-v2.json",
        "mycelium.layer_assignment.v2",
        ("layer_assignment.py", "model_manifest.py", "model_adapters.py", "route_contract.py"),
    ),
    ContractSpec(
        "artifact-verification-report-v1.json",
        "mycelium.artifact_verification_report.v1",
        ("weight_provisioning.py", "layer_assignment.py"),
    ),
    ContractSpec(
        "provisioning-audit-v1.json",
        "mycelium.provisioning_audit.v1",
        ("weight_provisioning.py", "layer_assignment.py", "route_contract.py"),
    ),
    ContractSpec(
        "gossip-router-view-v1.json",
        "mycelium.gossip.router_view.v1",
        ("mycelium_gossip/views.py",),
    ),
    ContractSpec(
        "gossip-allocator-view-v1.json",
        "mycelium.gossip.allocator_view.v1",
        ("mycelium_gossip/views.py",),
    ),
)

SPECS_BY_FIXTURE = {spec.fixture_name: spec for spec in CONTRACT_SPECS}
EXPECTED_FIXTURE_NAMES = frozenset(SPECS_BY_FIXTURE)
EXPECTED_PROTOCOLS = frozenset(spec.protocol for spec in CONTRACT_SPECS)

if len(SPECS_BY_FIXTURE) != len(CONTRACT_SPECS):
    raise RuntimeError("contract registry contains duplicate fixture names")
if len(EXPECTED_PROTOCOLS) != len(CONTRACT_SPECS):
    raise RuntimeError("contract registry contains duplicate protocol owners")
