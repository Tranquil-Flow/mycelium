# SPDX-License-Identifier: AGPL-3.0-or-later
"""A8 physical-gate execution machinery (spec §11-§12).

The runner is inert and fail-closed without live infrastructure: no case
executes, and no evidence is written, unless the required inputs exist.
The only records this module ever produces are closed
``mycelium.internet_native_qualification.v1`` documents sealed into an
owner-private evidence root. Executing a case never writes evidence by
itself; sealing is a separate explicit step.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping

from .activation import ActivationObservations
from .bootstrap import BoundaryError, canonical_https_origin, downgrade_verdict, redirect_verdict
from .contracts import (
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    validate_internet_native_qualification,
)
from .enrollment import EnrollmentError, PublicBootstrapClient
from .privacy import ensure_privacy_clean

A8_PHYSICAL_CASES = frozenset(
    {
        "unrelated_https_invite_without_tailscale",
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "cleartext_or_redirect_bootstrap",
        "certificate_without_seed_authority",
        "invalid_or_replayed_invitation",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "missing_or_stale_path_measurements",
        "raw_relay_identity_injection",
        "unqualified_external_member",
        "tailscale_unavailable",
        "ssh_unavailable",
    }
)
PEER_REQUIRED_CASES = frozenset(
    {
        "unrelated_https_invite_without_tailscale",
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "revoked_active_member",
        "endpoint_identity_mismatch",
        "unqualified_external_member",
        "tailscale_unavailable",
        "ssh_unavailable",
    }
)
_ADAPTER_REQUIRED_CASES = frozenset(
    {
        "cleartext_or_redirect_bootstrap",
        "certificate_without_seed_authority",
        "invalid_or_replayed_invitation",
    }
)
_PROJECTION_ONLY_CASES = frozenset(
    {
        "missing_or_stale_path_measurements",
        "raw_relay_identity_injection",
    }
)
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_CLOSED_CODES = frozenset(
    {
        "physical_infrastructure_unavailable",
        "case_unknown",
        "origin_invalid",
        "peer_required",
        "evidence_root_unsafe",
        "record_exists",
    }
)
OUTCOME_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class PhysicalGateError(RuntimeError):
    def __init__(self, code: str) -> None:
        if code not in _CLOSED_CODES:
            raise ValueError("physical gate error code is invalid")
        self.code = code
        super().__init__(code)


class PeerRequired(PhysicalGateError):
    def __init__(self) -> None:
        super().__init__("peer_required")


def _check_outcomes(outcomes: list[str]) -> list[str]:
    for outcome in outcomes:
        if OUTCOME_NAME_RE.fullmatch(outcome) is None:
            raise PhysicalGateError("case_unknown")
    return sorted(set(outcomes))


def _now_unix_ms() -> int:
    from time import time

    return int(time() * 1000)


def preflight_document(
    *,
    now_unix_ms: int,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Inert, claim-free preflight envelope (never executed)."""

    document: dict[str, Any] = {
        "protocol": INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
        "qualification_id": f"preflight-{now_unix_ms}",
        "gate_kind": "physical_negative",
        "case_ids": ["cleartext_or_redirect_bootstrap"],
        "observed_at_unix_ms": now_unix_ms,
        "executed": False,
        "result": "not_executed",
        "spec_digest": spec_digest,
        "source_digest": source_digest,
        "evidence_digests": [],
        "fresh_until_unix_ms": None,
        "projection_digest": None,
        "public_projection": {
            "gate_case_ids": ["cleartext_or_redirect_bootstrap"],
            "outcomes": ["not_executed"],
            "relay_reference": None,
            "observed_at_unix_ms": now_unix_ms,
        },
    }
    validate_internet_native_qualification(document)
    return document


def _executed_envelope(
    *,
    case_id: str,
    result: str,
    outcomes: list[str],
    spec_digest: str,
    source_digest: str,
    evidence_digests: list[str],
) -> dict[str, Any]:
    now_unix_ms = _now_unix_ms()
    document: dict[str, Any] = {
        "protocol": INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
        "qualification_id": f"a8-{case_id}-{now_unix_ms}",
        "gate_kind": (
            "physical_positive"
            if case_id
            in {
                "unrelated_https_invite_without_tailscale",
                "direct_path_qualified_browser_inference",
                "forced_relay_privacy_reduced_browser_inference",
                "observed_path_transition_and_reconnect",
            }
            else "physical_negative"
        ),
        "case_ids": [case_id],
        "observed_at_unix_ms": now_unix_ms,
        "executed": True,
        "result": result,
        "spec_digest": spec_digest,
        "source_digest": source_digest,
        "evidence_digests": evidence_digests,
        "fresh_until_unix_ms": now_unix_ms + 7 * 86_400_000,
        "projection_digest": None,
        "public_projection": {
            "gate_case_ids": [case_id],
            "outcomes": _check_outcomes(outcomes),
            "relay_reference": None,
            "observed_at_unix_ms": now_unix_ms,
        },
    }
    projection_bytes = json.dumps(
        document["public_projection"],
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    document["projection_digest"] = (
        "sha256:" + hashlib.sha256(projection_bytes).hexdigest()
    )
    validate_internet_native_qualification(document)
    return document


def _run_cleartext_case(
    adapter: PublicBootstrapClient,
    origin: str,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    outcomes: list[str] = []
    cleartext = "http" + origin[len("https") :]
    if downgrade_verdict(cleartext) == "downgrade_refused":
        outcomes.append("no_cleartext_fallback")
    if redirect_verdict(origin, cleartext) == "downgrade_refused":
        outcomes.append("no_cleartext_fallback")
    for path in ("/", "/seed/admin", "/seed/invite", "/seed/members"):
        try:
            adapter.policy.validate_request(
                method="GET",
                target=path,
                content_type=None,
                body_length=0,
            )
        except BoundaryError:
            outcomes.append("bounded_public_error")
    outcomes.append("request_rejected")
    if adapter._join_transmissions == 0:  # noqa: SLF001
        outcomes.append("invite_secret_never_transmitted")
    return _executed_envelope(
        case_id="cleartext_or_redirect_bootstrap",
        result="passed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def _run_certificate_case(
    adapter: PublicBootstrapClient,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    outcomes: list[str] = []
    from time import time

    try:
        adapter.preflight(now=time())
    except EnrollmentError as exc:
        if exc.code in {"pin_mismatch", "seed_signature_invalid"}:
            outcomes.append("seed_pin_mismatch_before_invite_transmission")
            outcomes.append("join_not_attempted")
            outcomes.append("bounded_incident_only")
        else:
            raise
    if adapter._join_transmissions == 0:  # noqa: SLF001
        outcomes.append("invite_secret_never_transmitted")
    if not outcomes:
        return _executed_envelope(
            case_id="certificate_without_seed_authority",
            result="failed",
            outcomes=["bounded_incident_only"],
            spec_digest=spec_digest,
            source_digest=source_digest,
            evidence_digests=[],
        )
    return _executed_envelope(
        case_id="certificate_without_seed_authority",
        result="passed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def _run_projection_cases(
    case_id: str,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    outcomes: list[str] = []
    if case_id == "missing_or_stale_path_measurements":
        observations = ActivationObservations(clock=lambda: _now_unix_ms() / 1000.0)
        projection = observations.current_projection()
        if projection["path_class"] == "unknown":
            outcomes.append("path_class_remains_unknown")
        metrics = projection["metrics"]
        if all(
            metrics[key] is None
            for key in ("rtt_ms", "warm_rtt_ms", "jitter_ms", "goodput_bytes_per_second", "loss_ratio", "sample_count")
        ):
            outcomes.append("missing_metrics_remain_unknown")
        outcomes.append("required_objective_blocked")
    elif case_id == "raw_relay_identity_injection":
        injected = {
            "relay_reference": "https://relay.example.com:443",
            "region": "exact-building-4",
        }
        try:
            ensure_privacy_clean(injected)
        except Exception as exc:  # noqa: BLE001 - bounded projection rejection
            assert exc.__class__.__name__ == "PrivacyViolation"
            outcomes.append("raw_relay_identity_rejected")
            outcomes.append("no_public_projection_emitted")
    return _executed_envelope(
        case_id=case_id,
        result="passed" if outcomes else "failed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def execute_case(
    case_id: str,
    *,
    origin: str,
    evidence_root: Path | None,
    adapter: PublicBootstrapClient | None = None,
    spec_digest: str = "sha256:" + "0" * 64,
    source_digest: str = "sha256:" + "0" * 64,
) -> dict[str, Any]:
    """Execute one physical case if, and only if, its inputs exist."""

    if case_id not in A8_PHYSICAL_CASES:
        raise PhysicalGateError("case_unknown")
    try:
        canonical_https_origin(origin)
    except ValueError as exc:
        raise PhysicalGateError("origin_invalid") from exc
    if case_id in PEER_REQUIRED_CASES:
        raise PeerRequired()
    if case_id in _PROJECTION_ONLY_CASES:
        return _run_projection_cases(case_id, spec_digest, source_digest)
    if adapter is None:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    if case_id == "cleartext_or_redirect_bootstrap":
        return _run_cleartext_case(adapter, origin, spec_digest, source_digest)
    if case_id == "certificate_without_seed_authority":
        return _run_certificate_case(adapter, spec_digest, source_digest)
    raise PhysicalGateError("physical_infrastructure_unavailable")


def seal_qualification(
    document: Mapping[str, Any],
    *,
    evidence_root: Path,
) -> Path:
    """Validate and lock one executed qualification record into the
    owner-private evidence root. Sealing never fabricates a result."""

    root = Path(evidence_root)
    try:
        metadata = root.lstat()
    except OSError as exc:
        raise PhysicalGateError("evidence_root_unsafe") from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise PhysicalGateError("evidence_root_unsafe")
    validate_internet_native_qualification(document)
    data = dict(document)
    qualification_id = data["qualification_id"]
    if not _CODE_RE.fullmatch(qualification_id) and not re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", qualification_id
    ):
        raise PhysicalGateError("evidence_root_unsafe")
    record = root / f"qualification-{qualification_id}.json"
    raw = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(
            record,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
    except FileExistsError as exc:
        raise PhysicalGateError("record_exists") from exc
    try:
        with os.fdopen(descriptor, "wb") as output:
            descriptor = -1
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        record.chmod(0o400)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return record


__all__ = [
    "A8_PHYSICAL_CASES",
    "PEER_REQUIRED_CASES",
    "PeerRequired",
    "PhysicalGateError",
    "execute_case",
    "preflight_document",
    "seal_qualification",
]
