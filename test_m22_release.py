from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from mycelium_m22_release import (
    build_release_evidence,
    ui_audit_summary,
    validate_release_evidence,
    validate_ui_audit,
)


ROOT = Path(__file__).resolve().parent
DIGEST = "sha256:" + "a" * 64


def values() -> dict[str, object]:
    audit = json.loads(
        (ROOT / "docs" / "release" / "m22-ui-requirements.v1.json").read_text()
    )
    return {
        "generated_at_unix_ms": 1_786_420_000_000,
        "source": {
            "revision": "m22-release",
            "contract_manifest_digest": DIGEST,
            "sbom_digest": DIGEST,
            "clean_bootstrap": True,
        },
        "ui_audit": ui_audit_summary(audit),
        "services": {
            "package_count": 3,
            "roles": ["seed", "node", "supervisor"],
            "platform_classes": ["launchd", "systemd"],
            "continuous_renewal": True,
            "bounded_restart": True,
            "foreground_route_restart_verified": True,
            "restart_verified": True,
            "coordinator_restart_verified": True,
            "managed_restart_evidence_digest": "sha256:" + "e" * 64,
            "log_rotation": True,
            "graceful_drain": True,
        },
        "physical": {
            "simulated": False,
            "participant_count": 3,
            "runtime_class_count": 2,
            "activation_transport": "endpointid_authenticated_iroh",
            "tailscale_product_dependency": False,
            "frame_count_before": 10,
            "frame_count_after": 20,
            "output_token_count": 4,
            "request_completed": True,
        },
        "model": {
            "model_id": "Qwen/Qwen2.5-3B-Instruct",
            "revision": "aa8e72537993ba99e69dfaafa59ed015b17504d1",
            "parameter_class": "3B",
            "weight_bytes": 6_171_927_653,
            "architecture_adapter": "qwen2",
            "local_cache_reused": True,
            "network_download_performed": False,
            "qualified": True,
            "reason": "physical_usefulness_gate_passed",
        },
        "qwen3_8b": {
            "model_id": "Qwen/Qwen3-8B",
            "revision": "b968826d9c46dd6066d109eabc6255188de91218",
            "adapter_id": "qwen3",
            "local_snapshot_complete": True,
            "adapter_verified": True,
            "qualified": False,
            "reason": "insufficient_swarm_memory_and_disk",
        },
        "tests": {
            "python_passed": 3_725,
            "python_skipped": 13,
            "ui_passed": 433,
            "rust_passed": 20,
            "browser_engines": ["chromium", "firefox", "webkit"],
            "production_build": True,
            "accessibility": True,
            "performance": True,
            "privacy": True,
            "security": True,
            "claim_boundary": True,
        },
        "reviewer": {
            "bundle_version": "astras-macbook-m22-1",
            "preflight_idempotent": True,
            "surrogate_verified": True,
            "external_network": True,
            "assigned_stage": True,
            "inference_completed": True,
            "negative_case_verified": True,
        },
        "exclusions": [],
    }


def test_ui_audit_is_closed_complete_and_digestible() -> None:
    audit = json.loads(
        (ROOT / "docs" / "release" / "m22-ui-requirements.v1.json").read_text()
    )
    assert validate_ui_audit(audit) == audit
    summary = ui_audit_summary(audit)
    assert summary["requirement_count"] == 20
    assert summary["verified_count"] == 20
    assert summary["excluded_count"] == 0


def test_release_gate_is_derived_from_all_closure_dimensions() -> None:
    evidence = build_release_evidence(**values())
    assert evidence["gate_state"] == "qualified"
    assert validate_release_evidence(evidence) == evidence

    incomplete = values()
    incomplete["physical"]["participant_count"] = 2  # type: ignore[index]
    withheld = build_release_evidence(**incomplete)
    assert withheld["gate_state"] == "withheld"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda item: item.update(extra=True),
        lambda item: item["source"].update(clean_bootstrap="yes"),
        lambda item: item["physical"].update(frame_count_after=-1),
        lambda item: item["model"].update(network_download_performed="no"),
        lambda item: item["tests"].update(browser_engines=["chromium"]),
        lambda item: item.update(evidence_digest=DIGEST),
    ],
)
def test_release_evidence_rejects_drift_and_wrong_types(mutate) -> None:
    evidence = copy.deepcopy(build_release_evidence(**values()))
    mutate(evidence)
    with pytest.raises(ValueError, match="m22_release_evidence_invalid"):
        validate_release_evidence(evidence)


def test_release_sealer_writes_canonical_derived_gate(tmp_path: Path) -> None:
    claims = values()
    claims["reviewer"]["external_network"] = False  # type: ignore[index]
    claims["exclusions"] = ["external_reviewer_surrogate_not_executed"]
    claims_path = tmp_path / "claims.json"
    output_path = tmp_path / "m22-release.json"
    claims_path.write_text(json.dumps(claims), "utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "seal_m22_release.py"),
            "--claims",
            str(claims_path),
            "--output",
            str(output_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )

    evidence = validate_release_evidence(json.loads(output_path.read_text("utf-8")))
    assert evidence["gate_state"] == "withheld"
    assert evidence["exclusions"] == ["external_reviewer_surrogate_not_executed"]
    assert json.loads(completed.stdout)["gate_state"] == "withheld"
