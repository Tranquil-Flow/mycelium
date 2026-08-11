from __future__ import annotations

import copy

import pytest

from mycelium_service_restart_evidence import (
    build_managed_restart_evidence,
    validate_managed_restart_evidence,
)


def evidence() -> dict[str, object]:
    service = {
        "restart_limit": 3,
        "restart_window_seconds": 300,
        "child_replaced": True,
        "manager_continuous": True,
        "health_restored": True,
        "health_restored_within_seconds": 180,
    }
    return build_managed_restart_evidence(
        generated_at_unix_ms=1_786_428_200_000,
        platform_classes=["launchd", "systemd"],
        services=[
            {**service, "service_id": "m22-seed", "role": "seed", "manager": "launchd"},
            {**service, "service_id": "m22-node", "role": "node", "manager": "systemd"},
            {**service, "service_id": "m22-supervisor", "role": "supervisor", "manager": "launchd"},
        ],
        coordinator={
            "member_count": 3,
            "renewals_advanced": 3,
            "all_leases_fresh": True,
            "generation_preserved_or_advanced": True,
        },
        route={
            "simulated": False,
            "request_completed_after_restart": True,
            "frames_before": 18,
            "frames_after": 32,
            "fatal": False,
        },
    )


def test_managed_restart_evidence_is_closed_derived_and_privacy_reduced() -> None:
    result = evidence()
    assert result["verified"] is True
    assert validate_managed_restart_evidence(result) == result
    rendered = repr(result)
    assert "100.84." not in rendered
    assert "/Users/" not in rendered
    assert "Reply with" not in rendered


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value["services"][0].__setitem__("health_restored", False),
        lambda value: value["route"].__setitem__("frames_after", 18),
        lambda value: value.__setitem__("evidence_digest", "sha256:" + "0" * 64),
        lambda value: value.__setitem__("unexpected", True),
    ],
)
def test_managed_restart_evidence_rejects_tampering(mutate) -> None:
    value = copy.deepcopy(evidence())
    mutate(value)
    with pytest.raises(ValueError, match="managed_service_restart_evidence_invalid"):
        validate_managed_restart_evidence(value)
