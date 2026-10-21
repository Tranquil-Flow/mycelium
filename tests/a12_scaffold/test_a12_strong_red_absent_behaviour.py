# SPDX-License-Identifier: AGPL-3.0-only
"""A12 strong-RED acceptance suite: behaviour that is ABSENT at the frozen base.

Every test in this module imports only surfaces that already exist at the frozen
acceptance base ``c573e0f`` -- ``mycelium_mobile`` (the Pixel/Termux clean-room
adapters), ``mycelium_membership.contracts`` and the frozen A12 acceptance files.
None of them imports this lane's scaffold ``mycelium_mobile.a12_activation``; the
production scan below skips that single path explicitly, so this suite reports the
same failures with or without the scaffold present.

Reproduce against the untouched base (no scaffold, no A12 production module):

    git -C <repository root> \
        archive c573e0f | tar -x -C /tmp/a12-base
    mkdir -p /tmp/a12-base/tests/a12_scaffold
    cp tests/a12_scaffold/__init__.py \
       tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py \
       /tmp/a12-base/tests/a12_scaffold/
    cd /tmp/a12-base && env -u PYTHONPATH python3.14 -m pytest \
        tests/a12_scaffold/test_a12_strong_red_absent_behaviour.py -q

(Use any Python 3.14 interpreter with pytest.  Clear PYTHONPATH if it is set: this
host's PYTHONPATH points at a 3.11 virtualenv whose numpy cannot load under 3.14.)

Expected at the base and with the scaffold present: 9 failed, 2 passed.  The nine
failures name absent baseline behaviour; the two passing tests are guards that must
never regress.

Frozen inputs this module binds:
  docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md
  tests/a12_acceptance/inventory.v1.json
  tests/a12_acceptance/scenarios.v1.json
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

import mycelium_evidence
import mycelium_mobile
from mycelium_membership import contracts as membership_contracts
from mycelium_mobile import android_sidecar, pixel_runtime, pixel_stage, termux_bridge

STRONG_RED = "STRONG RED (absent at c573e0f)"
ROOT = Path(__file__).resolve().parents[2]
BRIDGE_TOKEN = "a12-strong-red-probe-token-0123456789"

# Production source only: no tests, no preview compiler sources, no UI bundle, no
# generated artefacts.  ``LANE_OWN_ARTEFACTS`` is this lane's own scaffold, which
# is measured separately by tests/a12_scaffold/test_a12_scaffold_boundaries.py.
EXCLUDED_DIRS = frozenset(
    {
        "tests",
        "build",
        "ui",
        "outputs",
        "runs",
        "release",
        "scenarios",
        "node_modules",
        ".git",
        "__pycache__",
        "dist",
        ".build-native",
        "docs",
        "contracts",
        "tolerances",
    }
)
LANE_OWN_ARTEFACTS = frozenset({"mycelium_mobile/a12_activation.py"})


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _production_python_files() -> list[Path]:
    files: list[Path] = []
    for path in sorted(ROOT.rglob("*.py")):
        relative = path.relative_to(ROOT)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if relative.as_posix() in LANE_OWN_ARTEFACTS:
            continue
        files.append(path)
    return files


def _scan(pattern: str) -> list[str]:
    return [
        _relative(path)
        for path in _production_python_files()
        if re.search(pattern, path.read_text("utf-8"), re.I)
    ]


def _pixel_stage_evidence_keys() -> list[str]:
    """Read the exact keys of the mobile evidence record this base writes."""

    source = (ROOT / "mycelium_mobile" / "pixel_stage.py").read_text("utf-8")
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "evidence"
            and isinstance(node.value, ast.Dict)
        ):
            return [
                key.value
                for key in node.value.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
    pytest.fail(
        f"{STRONG_RED}: the evidence record of mycelium_mobile/pixel_stage.py could "
        "not be read at all, so the suite cannot measure it.  Treat this as an "
        "unverified finding, not as absence evidence."
    )


# ---------------------------------------------------------------------------
# Records: the three capability-named A12 records do not exist anywhere
# ---------------------------------------------------------------------------


def test_strong_red_no_a12_capability_named_records_exist() -> None:
    """Specification section 5 names three records; none of them exists."""

    declared = (
        "mycelium.mobile_runtime_observation.v1",
        "mycelium.mobile_role_qualification.v1",
        "mycelium.mobile_activation_qualification.v1",
    )
    hits = {protocol: _scan(re.escape(protocol)) for protocol in declared}
    missing = sorted(protocol for protocol, found in hits.items() if not found)

    assert not missing, (
        f"{STRONG_RED}: {len(missing)} of the three capability-named A12 records are "
        f"declared nowhere in {len(_production_python_files())} production modules: "
        f"{missing}.  The only mobile evidence record the base can produce is "
        f"{pixel_stage.STAGE_EVIDENCE_PROTOCOL!r} from "
        "mycelium_mobile/pixel_stage.py, so no rung proof, role qualification or "
        "owner-private activation qualification can be authored, bound or rejected "
        "(specification section 5)."
    )


def test_strong_red_mobile_evidence_record_carries_no_source_kind() -> None:
    """The mobile evidence record cannot label native versus synthetic evidence."""

    keys = _pixel_stage_evidence_keys()
    required = {
        "source_kind",
        "observation_authority",
        "measurement_duration_ms",
        "sample_count",
    }
    absent = sorted(required - set(keys))

    assert not absent, (
        f"{STRONG_RED}: the only mobile evidence record in the tree "
        f"({pixel_stage.STAGE_EVIDENCE_PROTOCOL!r}) has exactly {len(keys)} fields "
        f"{keys}.  It carries no {absent}; it cannot separate native device "
        "observation from simulator, fixture or Termux conformance output, and it "
        f"records no sustainable thermal/power/memory/lifecycle/network metric with "
        "duration, sample count, freshness and authority.  The repository already "
        "owns a source-kind vocabulary in mycelium_evidence.SOURCE_KINDS "
        f"({sorted(mycelium_evidence.SOURCE_KINDS)}), and the mobile "
        "path does not use it (specification section 5)."
    )


def test_strong_red_no_mobile_role_or_rung_record_surface_exists() -> None:
    """No production module names an A12 rung, role or per-rung record."""

    rung_hits = _scan(r"\brung")
    role_hits = _scan(
        r"probe_contributor|speculative_draft_worker|qualified_model_stage"
    )

    assert rung_hits and role_hits, (
        f"{STRONG_RED}: rung vocabulary appears in {len(rung_hits)} production "
        f"modules and the three frozen mobile roles "
        "(probe_contributor, speculative_draft_worker, qualified_model_stage) "
        f"appear in {len(role_hits)}.  The inventory's four eligibility rungs "
        "(signed_membership, capability_eligibility, artifact_readiness, "
        "operation_qualification) are separately authored and generation bound, so "
        "the base cannot represent even one of them "
        "(tests/a12_acceptance/inventory.v1.json, eligibility_rungs)."
    )


def test_strong_red_mobile_activation_eligibility_is_a_hardcoded_static_allow_list() -> None:
    """The one eligibility predicate hardcodes its eligible peer classes."""

    capability = {
        "runtime_backend": "pixel-stdlib",
        "transport": "iroh",
        "activation_protocol": membership_contracts.ROUTER_ACTIVATION_PROTOCOL,
    }
    eligible = membership_contracts.peer_runtime_is_activation_eligible(
        "android_termux_iroh", capability
    )
    pixel_eligible = membership_contracts.peer_runtime_is_activation_eligible(
        "pixel_http",
        {"runtime_backend": "android", "transport": "http", "activation_protocol": None},
    )

    assert eligible is True and pixel_eligible is True, (
        f"{STRONG_RED}: the membership contract's only activation-eligibility "
        f"predicate returns {eligible!r} for the Android Iroh peer class and "
        f"{pixel_eligible!r} for the Pixel HTTP peer class.  Both mobile peer "
        "classes are statically ineligible because the predicate hardcodes its "
        "eligible set to the desktop classes; A12 requires the exact registered "
        "platform/runtime/transport operation to earn eligibility from fresh native "
        f"evidence instead.  PEER_CLASSES={sorted(membership_contracts.PEER_CLASSES)}, "
        f"the only Android entries are 'pixel_http' (brand named) and "
        f"'android_termux_iroh' (a Termux path the inventory excludes from the "
        f"normal product path)."
    )


# ---------------------------------------------------------------------------
# Native product path: only Termux/ADB-class development tools exist
# ---------------------------------------------------------------------------


def test_strong_red_native_product_path_absent_termux_bridge_is_the_android_surface() -> None:
    """The only non-local Android control surface is a tailnet Termux bridge."""

    with pytest.raises(termux_bridge.TermuxBridgeError) as refused:
        termux_bridge.TermuxBridgeClient("https://seed.example.com", token=BRIDGE_TOKEN)
    assert refused.value.args[0] == "termux_bridge_url_invalid"

    bridge = termux_bridge.TermuxBridgeClient(
        "http://203.0.113.18:9020", token=BRIDGE_TOKEN
    )
    assert bridge.base_url == "http://203.0.113.18:9020"

    native_surfaces = sorted(
        name for name in mycelium_mobile.__all__ if "native" in name.lower()
    )
    modules = sorted(path.name for path in (ROOT / "mycelium_mobile").glob("*.py"))

    assert native_surfaces, (
        f"{STRONG_RED}: mycelium_mobile exposes {list(mycelium_mobile.__all__)} from "
        f"{modules}.  The only Android command surface accepts a tailnet-only bridge "
        f"URL (100.64.0.0/10 port 9020) and refuses any ordinary product origin with "
        f"'termux_bridge_url_invalid'; there is no signed native application, no "
        "embedded authenticated transport and no enrollment surface.  A12 forbids a "
        "shell, Termux, ADB or SSH dependency on the ordinary external-user path "
        "(specification sections 3 and 6, inventory decision native_product_path_only)."
    )


def test_strong_red_no_native_mobile_qualification_surface_exists() -> None:
    """The only Android deployment artefact is a software-only Termux sidecar."""

    assert "no Pixel execution, physical qualification" in android_sidecar.CLAIM_BOUNDARY

    probes = {
        name: getattr(module, name, None)
        for module in (mycelium_mobile, membership_contracts)
        for name in (
            "qualify_mobile_operation",
            "mobile_role_qualification",
            "native_role_qualifier",
            "qualify_native_role",
        )
    }
    present = sorted(name for name, value in probes.items() if value is not None)

    assert present, (
        f"{STRONG_RED}: no native qualification entry point exists on any "
        "production module "
        f"(probed {sorted(probes)}; the A12 scaffold of this lane is excluded from "
        "this module by construction).  The only Android deployment artefact is "
        f"{android_sidecar.PACKAGE_PROTOCOL!r} targeting "
        f"{android_sidecar.ANDROID_TARGET!r}, whose own claim boundary reads "
        f"{android_sidecar.CLAIM_BOUNDARY!r}, and whose Termux process records state "
        "'physical_execution': False (mycelium_mobile/android_sidecar.py:429,464).  "
        "No physical positive or negative mobile gate can be recorded, let alone "
        "passed (specification sections 6, 7 and 11)."
    )


def test_strong_red_no_apple_mobile_surface_exists() -> None:
    """Nothing in production source mentions iOS or iPadOS."""

    files = _production_python_files()
    hits = _scan(r"\b(ios|ipados)\b")

    assert hits, (
        f"{STRONG_RED}: zero of {len(files)} production python modules mention iOS "
        "or iPadOS.  A12 requires a signed native Apple-mobile application with "
        "foreground-active initial eligibility, exact-platform binding, suspension "
        "drain/fencing and simulator exclusion "
        "(tests/a12_acceptance/inventory.v1.json -> platform_requirements."
        "apple_mobile: platform_families ['ios', 'ipados'], simulator_may_qualify "
        "false).  No Apple-mobile implementation, contract or evidence surface "
        "exists at this base."
    )


def test_strong_red_android_family_identity_exists_only_behind_adb() -> None:
    """Two-family proof needs pseudonymous family references; none are recorded."""

    hits = _scan(r"\b(manufacturer|family_ref|device_family)\b")

    assert not hits, (
        f"{STRONG_RED}: the inventory's two_android_family_proof decision requires "
        "two independently manufactured families recorded as pseudonymous references "
        "in owner-private evidence.  The only manufacturer or family identity "
        f"anywhere in production source is {hits} -- an ADB probe that shells "
        "'getprop ro.product.manufacturer' (probe_android_adb.py:169), which is an "
        "excluded development tool and records a raw manufacturer string, not a "
        "pseudonymous family reference.  No production surface can record, compare "
        "or expose Android device-family diversity."
    )


def test_strong_red_product_projection_has_no_four_rung_ladder() -> None:
    """The product surfaces project no rung, role, source kind or blocker ladder."""

    product_files = sorted((ROOT / "mycelium_product_spine").glob("*.py")) + sorted(
        (ROOT / "mycelium_ui_gateway").glob("*.py")
    )
    product_hits = [path.name for path in product_files if "rung" in path.read_text("utf-8").lower()]
    ui_files = list((ROOT / "ui" / "web" / "src").rglob("*.ts")) + list(
        (ROOT / "ui" / "web" / "src").rglob("*.tsx")
    )
    ui_hits = [path.name for path in ui_files if "rung" in path.read_text("utf-8").lower()]
    projector = (ROOT / "mycelium_product_spine" / "projector.py").read_text("utf-8")
    mobile_label = [
        line.strip() for line in projector.splitlines() if "Mobile conformance" in line
    ]

    assert product_hits and ui_hits, (
        f"{STRONG_RED}: of {len(product_files)} product-spine/ui-gateway modules "
        f"{len(product_hits)} mention a rung, and of {len(ui_files)} UI source "
        f"modules {len(ui_hits)} do.  The inventory's device_lab projection "
        "requires 'four_rung_ladder', role qualification, source kind, freshness, "
        "constraints and blockers; the only mobile handling in the product "
        f"projector is a hard-coded peer-class branch projecting {mobile_label} "
        "(mycelium_product_spine/projector.py:453), which is the brand/implementation "
        "detail the specification forbids in UI copy (specification section 9, "
        "decision dynamic_mobile_projection)."
    )


# ---------------------------------------------------------------------------
# Guards: honesty invariants that hold at the base and must keep holding
# ---------------------------------------------------------------------------


def test_guard_this_suite_measures_the_baseline_not_the_scaffold() -> None:
    """This module must never import this lane's own scaffold surface."""

    imported = sorted(
        name
        for name, value in globals().items()
        if "a12_activation" in getattr(value, "__name__", "")
    )
    assert imported == []
    assert "mycelium_mobile/a12_activation.py" in LANE_OWN_ARTEFACTS
    assert 1 <= len(LANE_OWN_ARTEFACTS) <= 3


def test_guard_frozen_acceptance_inputs_stay_design_only() -> None:
    """The frozen A12 acceptance inputs must not be promoted by this lane."""

    import json

    inventory = json.loads(
        (ROOT / "tests" / "a12_acceptance" / "inventory.v1.json").read_text("utf-8")
    )
    scenarios = json.loads(
        (ROOT / "tests" / "a12_acceptance" / "scenarios.v1.json").read_text("utf-8")
    )

    assert inventory["protocol"] == "mycelium.a12_mobile_acceptance_inventory.v1"
    assert inventory["gate"] == "A12"
    assert inventory["state"] == "design_only"
    assert inventory["claim_boundary"] == (
        "frozen mobile authority, platform, native-path, qualification, and "
        "projection acceptance inputs only; no application, runtime, device "
        "evidence, eligibility, readiness, or completion claim"
    )
    assert scenarios["protocol"] == "mycelium.a12_acceptance_scenarios.v1"
    assert scenarios["claim_boundary"] == (
        "frozen acceptance inputs only; no mobile implementation or qualification claim"
    )
