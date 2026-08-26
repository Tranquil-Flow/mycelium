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
import math
import os
from pathlib import Path
import re
import stat
import struct
import time as _time_module
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from .activation import ActivationObservations, RelayProjector
from .bootstrap import (
    BoundaryError,
    PublicBootstrapPolicy,
    canonical_https_origin,
)
from .contracts import (
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    validate_activation_observation,
    validate_internet_native_qualification,
    validate_relay_projection,
)
from .enrollment import EnrollmentError, PublicBootstrapClient
from .privacy import ensure_privacy_clean
from mycelium_invite import verify_invite_bundle
from mycelium_seed.coordinator import SEED_SIGNED_ENVELOPE_PROTOCOL
from mycelium_seed.http import SeedHTTPClient
from mycelium_qualification.evidence import EvidenceValidationError, canonical_json_bytes
from mycelium_qualification.signing import (
    EvidenceSigningError,
    build_ed25519_verifier,
)
from mycelium_topology_evidence import validate_transport_path_observation

_DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_SOURCE_MANIFEST = (
    _DEFAULT_SOURCE_ROOT / "docs" / "handover" / "a8-source-manifest.v1.json"
)

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
# Bounded seed refusals that genuinely mean "this endpoint identity is not
# the one accepted for this member". Anything outside this set proves
# something else went wrong and must not satisfy the mismatch gate.
_IDENTITY_MISMATCH_CODES = frozenset(
    {
        "membership_key_pin_mismatch",
        "membership_endpoint_mismatch",
        "seed_member_identity_mismatch",
    }
)
# Peer-required cases that additionally consume a browser report and fresh,
# independently signed resource-observation snapshots.
_UI_DEPENDENT_CASES = frozenset(
    {
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
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
        "transport_observation_invalid",
        "transport_observation_signature_invalid",
        "browser_observation_invalid",
        "browser_observation_signature_invalid",
        "relay_projection_key_invalid",
        "source_binding_invalid",
        "qualification_not_passed",
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


def _require_source_binding(value: object) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None
        or value == "sha256:" + "0" * 64
    ):
        raise PhysicalGateError("source_binding_invalid")
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _read_source_path_bytes(source_root: Path, relative_path: str) -> bytes:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or "\\" in relative_path
        or relative_path.startswith("/")
    ):
        raise OSError("unsafe source path")
    components = relative_path.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise OSError("unsafe source path")
    descriptors: list[int] = []
    try:
        current = os.open(
            source_root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        descriptors.append(current)
        root_metadata = os.fstat(current)
        if not stat.S_ISDIR(root_metadata.st_mode):
            raise OSError("unsafe source root")
        for component in components[:-1]:
            current = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=current,
            )
            descriptors.append(current)
            if not stat.S_ISDIR(os.fstat(current).st_mode):
                raise OSError("unsafe source directory")
        descriptor = os.open(
            components[-1],
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=current,
        )
        descriptors.append(descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or not 0 <= before.st_size <= 64 * 1024 * 1024:
            raise OSError("unsafe source file")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                raise OSError("short source read")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
        ):
            raise OSError("source changed during read")
        return b"".join(chunks)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _verify_default_source_binding(expected_digest: object) -> str:
    expected = _require_source_binding(expected_digest)
    try:
        manifest_relative = _DEFAULT_SOURCE_MANIFEST.relative_to(
            _DEFAULT_SOURCE_ROOT
        ).as_posix()
        manifest_raw = _read_source_path_bytes(
            _DEFAULT_SOURCE_ROOT, manifest_relative
        )
        manifest = json.loads(
            manifest_raw.decode("utf-8"),
            object_pairs_hook=_strict_json_object,
        )
        if not isinstance(manifest, dict) or set(manifest) != {
            "protocol",
            "base_commit",
            "files",
        }:
            raise ValueError("source manifest shape invalid")
        if manifest["protocol"] != "mycelium.a8_source_manifest.v1":
            raise ValueError("source manifest protocol invalid")
        if re.fullmatch(r"[0-9a-f]{40}", manifest["base_commit"]) is None:
            raise ValueError("source manifest commit invalid")
        pins = manifest["files"]
        if not isinstance(pins, list) or not 1 <= len(pins) <= 10_000:
            raise ValueError("source manifest pins invalid")
        manifest_digest = "sha256:" + hashlib.sha256(manifest_raw).hexdigest()
        if manifest_digest != expected:
            raise ValueError("source manifest digest mismatch")
        paths: list[str] = []
        for pin in pins:
            if not isinstance(pin, dict) or set(pin) != {
                "path",
                "sha256",
                "size_bytes",
            }:
                raise ValueError("source pin shape invalid")
            path = pin["path"]
            digest = pin["sha256"]
            size = pin["size_bytes"]
            if (
                not isinstance(path, str)
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size < 0
            ):
                raise ValueError("source pin invalid")
            content = _read_source_path_bytes(_DEFAULT_SOURCE_ROOT, path)
            if len(content) != size or "sha256:" + hashlib.sha256(content).hexdigest() != digest:
                raise ValueError("source pin drift")
            paths.append(path)
        if paths != sorted(set(paths)):
            raise ValueError("source manifest paths invalid")
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PhysicalGateError("source_binding_invalid") from exc
    return expected


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
    relay_reference: str | None = None,
    evidence_fresh_until_unix_ms: int | None = None,
    qualification_nonce: str | None = None,
) -> dict[str, Any]:
    now_unix_ms = _now_unix_ms()
    maximum_fresh_until = now_unix_ms + 7 * 86_400_000
    if evidence_fresh_until_unix_ms is not None:
        if (
            type(evidence_fresh_until_unix_ms) is not int
            or evidence_fresh_until_unix_ms <= now_unix_ms
        ):
            raise PhysicalGateError("transport_observation_invalid")
        maximum_fresh_until = min(
            maximum_fresh_until, evidence_fresh_until_unix_ms
        )
    if qualification_nonce is None:
        qualification_id = f"a8-{case_id}-{now_unix_ms}"
    else:
        nonce = _require_source_binding(qualification_nonce)
        qualification_id = f"a8-{case_id}-{nonce.removeprefix('sha256:')}"
    document: dict[str, Any] = {
        "protocol": INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
        "qualification_id": qualification_id,
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
        "fresh_until_unix_ms": maximum_fresh_until,
        "projection_digest": None,
        "public_projection": {
            "gate_case_ids": [case_id],
            "outcomes": _check_outcomes(outcomes),
            "relay_reference": relay_reference,
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


def build_adapter_from_bundle(
    *,
    origin: str,
    bundle: Mapping[str, Any],
    invite_token: str,
    transport_origin: str | None = None,
    clock: Callable[[], float] = _time_module.time,
) -> PublicBootstrapClient:
    """Construct the pin-first adapter for one canonical public origin from
    an owner-delivered invite bundle (the same wiring the rehearsal peer
    uses, shared for the physical-era CLI). ``transport_origin`` is the
    separately TLS-validated endpoint used only by the wrong-authority
    negative gate; invite origin policy remains bound to ``origin``."""

    canonical_origin = canonical_https_origin(origin)
    connect_origin = (
        canonical_origin
        if transport_origin is None
        else canonical_https_origin(transport_origin)
    )
    now = clock()
    verified = verify_invite_bundle(dict(bundle), now=now)
    client = SeedHTTPClient(
        seed_url=connect_origin,
        swarm_id=verified["payload"]["swarm_id"],
        seed_key_digest=verified["seed_key_digest"],
        seed_key_records=list(verified["seed_key_records"]),
        timeout=15.0,
    )
    return PublicBootstrapClient.from_seed_client(
        client,
        policy=PublicBootstrapPolicy(canonical_origin=canonical_origin),
        tls_state="publicly_trusted",
        bundle=dict(bundle),
        invite_token=invite_token,
        clock=clock,
        backoff_seconds=1.0,
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def probe_bootstrap_over_cleartext(
    origin: str,
    *,
    timeout: float = 10.0,
) -> str:
    """Attempt ``GET /seed/identity`` over the cleartext form of a
    canonical HTTPS origin with redirects disabled. Returns one bounded
    outcome: ``cleartext_refused`` (transport refused the attempt),
    ``cleartext_redirect_observed`` (a redirect answered, never followed),
    ``cleartext_no_identity`` (a non-envelope response), or
    ``cleartext_identity_exposed`` (a signed seed identity envelope was
    served over plaintext - the boundary FAILS this gate)."""

    cleartext = "http" + origin[len("https") :]
    opener = urllib.request.build_opener(_NoRedirectHandler())
    request = urllib.request.Request(
        cleartext + "/seed/identity",
        headers={"Accept": "application/json"},
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(200_000)
            status = response.status
    except urllib.error.HTTPError as exc:
        body = exc.read(200_000)
        status = exc.code
    except Exception:
        return "cleartext_refused"
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception:
        data = None
    if (
        isinstance(data, Mapping)
        and data.get("protocol") == SEED_SIGNED_ENVELOPE_PROTOCOL
        and isinstance(data.get("statement"), Mapping)
    ):
        return "cleartext_identity_exposed"
    if status in (301, 302, 303, 307, 308):
        return "cleartext_redirect_observed"
    return "cleartext_no_identity"


def _require_live_boundary(adapter: PublicBootstrapClient) -> None:
    """The boundary must demonstrably exist before any negative case can
    observe it: pin verification over the live origin. Anything else is
    indistinguishable from an absent boundary and must fail closed."""

    try:
        adapter.preflight(now=_time_module.time())
    except EnrollmentError as exc:
        raise PhysicalGateError("physical_infrastructure_unavailable") from exc


def _run_cleartext_case(
    adapter: PublicBootstrapClient,
    origin: str,
    spec_digest: str,
    source_digest: str,
    *,
    probe: Callable[[str], str] = probe_bootstrap_over_cleartext,
) -> dict[str, Any]:
    _require_live_boundary(adapter)
    outcomes: list[str] = [probe(origin)]
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
    result = (
        "failed" if outcomes[0] == "cleartext_identity_exposed" else "passed"
    )
    if result == "passed" and adapter._join_transmissions == 0:  # noqa: SLF001
        outcomes.append("no_cleartext_fallback")
    return _executed_envelope(
        case_id="cleartext_or_redirect_bootstrap",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def _validated_replay_state_probe(
    report: Any, *, member_id: str, membership_generation: int
) -> dict[str, Any]:
    validated = _validated_case_probe_common(
        report,
        protocol="mycelium.a8_replay_state_probe.v1",
        case_id="invalid_or_replayed_invitation",
        member_id=member_id,
        fields={
            "matching_member_count",
            "membership_generation",
            "artifact_grant_count",
            "route_mutation_count",
            "invitation_state",
            "changed_retry_members_created",
        },
    )
    if (
        validated["matching_member_count"] != 1
        or validated["membership_generation"] != membership_generation
        or validated["artifact_grant_count"] != 0
        or validated["route_mutation_count"] != 0
        or validated["invitation_state"] != "redeemed"
        or validated["changed_retry_members_created"] != 0
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return validated


def _run_replay_case(
    adapter: PublicBootstrapClient,
    *,
    first_join_envelope: Mapping[str, Any],
    second_join_envelope: Mapping[str, Any],
    second_adapter: PublicBootstrapClient,
    case_probe: Callable[[], Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Invalid/replayed invitation: the exact retry stays idempotent (no
    second member) and a changed retry under the same invite is rejected."""

    _require_live_boundary(adapter)
    _require_live_boundary(second_adapter)
    first = adapter.join(dict(first_join_envelope), now=_time_module.time())
    outcomes: list[str] = []
    try:
        exact = second_adapter.join(
            dict(first_join_envelope), now=_time_module.time()
        )
        first_generation = first.get("message", {}).get("membership_generation")
        exact_generation = exact.get("message", {}).get("membership_generation")
        if first_generation is not None and exact_generation == first_generation:
            outcomes.append("exact_retry_idempotent")
        else:
            outcomes.append("exact_retry_diverged")
    except EnrollmentError:
        outcomes.append("exact_retry_rejected")
    try:
        second_adapter.join(dict(second_join_envelope), now=_time_module.time())
        outcomes.append("changed_retry_accepted")
    except EnrollmentError as exc:
        if exc.code in {
            "seed_join_retry_mismatch",
            "invite_replayed",
            "changed_retry_rejected",
        }:
            outcomes.append("changed_retry_rejected")
        else:
            raise
    result = (
        "passed"
        if {"exact_retry_idempotent", "changed_retry_rejected"} <= set(outcomes)
        else "failed"
    )
    if result == "passed":
        first_message = first.get("message") or {}
        request_message = first_join_envelope.get("message") or {}
        member_id = request_message.get("sender_node_id")
        membership_generation = first_message.get("membership_generation")
        if (
            not isinstance(member_id, str)
            or type(membership_generation) is not int
        ):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        probe = _validated_replay_state_probe(
            _resolve(case_probe),
            member_id=member_id,
            membership_generation=membership_generation,
        )
        outcomes.extend(
            ["join_rejected", "no_partial_member", "single_use_state_not_corrupted"]
        )
        evidence_digests = [_evidence_digest(probe)]
    else:
        evidence_digests = []
    return _executed_envelope(
        case_id="invalid_or_replayed_invitation",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=evidence_digests,
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
    result = (
        "passed"
        if {"seed_pin_mismatch_before_invite_transmission", "join_not_attempted"}
        <= set(outcomes)
        else "failed"
    )
    return _executed_envelope(
        case_id="certificate_without_seed_authority",
        result=result,
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
            outcomes.append("privacy_incident_bounded")
    return _executed_envelope(
        case_id=case_id,
        result="passed" if outcomes else "failed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def _resolve(observation: Any) -> Any:
    """Take a host observation at the moment it is needed.

    Callers may pass either a captured mapping or a zero-argument observer.
    An observer is preferred for end-of-window facts: a value captured before
    the window opened would describe the wrong instant.
    """

    return observation() if callable(observation) else observation


def _peer_network_outcome(facts: Any) -> str:
    """Classify one peer-side network observation.

    The observation is operator-supplied because it describes the peer's own
    interfaces, which this process cannot see. It is validated strictly: a
    malformed observation is indistinguishable from an absent one and fails
    closed rather than being read as a clean network.
    """

    if not isinstance(facts, Mapping):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    binary_present = facts.get("tailscale_binary_present")
    interface_present = facts.get("tailnet_interface_present")
    addresses = facts.get("tailnet_addresses")
    if (
        not isinstance(binary_present, bool)
        or not isinstance(interface_present, bool)
        or not isinstance(addresses, list)
        or any(not isinstance(value, str) for value in addresses)
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    if interface_present or addresses:
        return "tailnet_path_present"
    return "no_tailnet_path_observed"


def _activation_is_unknown_not_zero() -> bool:
    """A peer with no measured activation projects ``unknown`` everywhere.

    Spec §10.1 ``unknown_not_zero_measurements``: absent measurements are
    never numeric zero. This reads the projection rather than asserting it.
    """

    projection = ActivationObservations(
        clock=_time_module.time
    ).current_projection()
    if projection.get("path_class") != "unknown":
        return False
    metrics = projection.get("metrics")
    if not isinstance(metrics, Mapping):
        return False
    if metrics.get("measured_zero") is not False:
        return False
    return all(
        metrics.get(name) is None
        for name in (
            "rtt_ms",
            "warm_rtt_ms",
            "jitter_ms",
            "goodput_bytes_per_second",
            "loss_ratio",
            "sample_count",
        )
    )


def _enroll_peer(
    adapter: PublicBootstrapClient,
    node: Any,
    join_envelope: Mapping[str, Any],
) -> dict[str, Any]:
    """Redeem one owner-delivered invite over the live public boundary."""

    acceptance = adapter.join(dict(join_envelope), now=_time_module.time())
    node.accept_join(
        acceptance,
        seed_key_digest=adapter._bundle_pin,  # noqa: SLF001
    )
    return dict(acceptance)


def _renew_membership(
    adapter: PublicBootstrapClient, node: Any
) -> dict[str, Any] | None:
    envelope = node.heartbeat(
        lifecycle_state="RUNNING", active_requests=0, force=True
    )
    if envelope is None:
        return None
    return adapter.heartbeat(envelope, now=_time_module.time())


def _validated_case_probe_common(
    report: Any,
    *,
    protocol: str,
    case_id: str,
    member_id: str,
    fields: set[str],
) -> dict[str, Any]:
    common = {
        "protocol",
        "case_id",
        "member_id",
        "observed_at_unix_ms",
    }
    now_unix_ms = _now_unix_ms()
    if (
        not isinstance(report, Mapping)
        or set(report) != common | fields
        or report.get("protocol") != protocol
        or report.get("case_id") != case_id
        or report.get("member_id") != member_id
        or type(report.get("observed_at_unix_ms")) is not int
        or not now_unix_ms - _TRANSPORT_REPORT_MAX_AGE_MS
        <= report["observed_at_unix_ms"]
        <= now_unix_ms + _TRANSPORT_REPORT_FUTURE_SKEW_MS
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return dict(report)


def _validated_membership_visibility_probe(
    report: Any, *, member_id: str
) -> dict[str, Any]:
    validated = _validated_case_probe_common(
        report,
        protocol="mycelium.a8_membership_visibility_probe.v1",
        case_id="unrelated_https_invite_without_tailscale",
        member_id=member_id,
        fields={"member_visible", "activation_eligible"},
    )
    if (
        validated["member_visible"] is not True
        or validated["activation_eligible"] is not False
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return validated


def _validated_endpoint_admission_snapshot(
    signed: Any,
    *,
    authority: Any,
    member_id: str,
    expected_endpoint_id: str,
    dialed_endpoint_id: str,
    spec_digest: str,
    source_digest: str,
    sidecar_binary_digest: str,
    challenge: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deployment_id, trusted_keys, _ = _validated_transport_authority(authority)
    if not isinstance(signed, Mapping) or set(signed) != {
        "observation",
        "signature",
        "verification_key",
    }:
        raise PhysicalGateError("transport_observation_invalid")
    observation = signed.get("observation")
    signature = signed.get("signature")
    verification_key = signed.get("verification_key")
    if (
        not isinstance(observation, Mapping)
        or set(observation) != _SIGNED_NODE_FIELDS
        or observation.get("protocol") != "mycelium.physical_node_observation.v1"
        or observation.get("event") != "inbound_admission_snapshot"
        or observation.get("route_ready") is not False
        or observation.get("deployment_id") != deployment_id
        or not isinstance(observation.get("endpoint_id"), str)
        or not isinstance(signature, Mapping)
        or not isinstance(verification_key, Mapping)
        or signature.get("signer_endpoint_id") != observation.get("endpoint_id")
        or trusted_keys.get(str(observation["endpoint_id"]))
        != verification_key.get("verification_key_digest")
    ):
        raise PhysicalGateError("transport_observation_invalid")
    try:
        valid = build_ed25519_verifier([verification_key])(
            canonical_json_bytes(dict(observation)), dict(signature)
        )
    except (EvidenceSigningError, TypeError, ValueError) as exc:
        raise PhysicalGateError("transport_observation_signature_invalid") from exc
    if valid is not True:
        raise PhysicalGateError("transport_observation_signature_invalid")
    details = observation.get("details")
    detail_fields = {
        "protocol",
        "case_id",
        "member_id",
        "spec_digest",
        "source_digest",
        "sidecar_binary_digest",
        "challenge",
        "expected_endpoint_id",
        "dialed_endpoint_id",
        "expected_peer_path_class",
        "admission",
    }
    if (
        not isinstance(details, Mapping)
        or set(details) != detail_fields
        or details.get("protocol")
        != "mycelium.physical_node.inbound_admission_evidence.v1"
        or details.get("case_id") != "endpoint_identity_mismatch"
        or details.get("member_id") != member_id
        or details.get("spec_digest") != spec_digest
        or details.get("source_digest") != source_digest
        or details.get("sidecar_binary_digest") != sidecar_binary_digest
        or details.get("challenge") != challenge
        or details.get("expected_endpoint_id") != expected_endpoint_id
        or details.get("dialed_endpoint_id") != dialed_endpoint_id
        or details.get("expected_peer_path_class") != "unknown"
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    admission = details.get("admission")
    fields = {
        "protocol",
        "inbound_identity_rejections",
        "inbound_frames_admitted",
        "candidate_identity_rejections",
        "measured_at_unix_ms",
    }
    if (
        not isinstance(admission, Mapping)
        or set(admission) != fields
        or admission.get("protocol") != "mycelium.iroh_sidecar.inbound_admission.v1"
        or any(
            type(admission.get(field)) is not int or admission[field] < 0
            for field in fields - {"protocol"}
        )
        or admission["candidate_identity_rejections"]
        > admission["inbound_identity_rejections"]
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return dict(observation), dict(admission)


def _validated_endpoint_activation_probe(
    report: Any,
    *,
    member_id: str,
    expected_endpoint_id: str,
    dialed_endpoint_id: str,
    spec_digest: str,
    source_digest: str,
    sidecar_binary_digest: str,
    transport_authority: Any,
) -> dict[str, Any]:
    fields = {
        "protocol",
        "case_id",
        "member_id",
        "spec_digest",
        "source_digest",
        "sidecar_binary_digest",
        "challenge",
        "before",
        "after",
    }
    if (
        not isinstance(report, Mapping)
        or set(report) != fields
        or report.get("protocol") != "mycelium.a8_endpoint_activation_probe.v2"
        or report.get("case_id") != "endpoint_identity_mismatch"
        or report.get("member_id") != member_id
        or report.get("spec_digest") != spec_digest
        or report.get("source_digest") != source_digest
        or report.get("sidecar_binary_digest") != sidecar_binary_digest
        or not isinstance(report.get("challenge"), str)
        or re.fullmatch(r"[A-Za-z0-9._:-]{16,256}", report["challenge"]) is None
        or expected_endpoint_id == dialed_endpoint_id
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    before_observation, before = _validated_endpoint_admission_snapshot(
        report["before"],
        authority=transport_authority,
        member_id=member_id,
        expected_endpoint_id=expected_endpoint_id,
        dialed_endpoint_id=dialed_endpoint_id,
        spec_digest=spec_digest,
        source_digest=source_digest,
        sidecar_binary_digest=sidecar_binary_digest,
        challenge=report["challenge"],
    )
    after_observation, after = _validated_endpoint_admission_snapshot(
        report["after"],
        authority=transport_authority,
        member_id=member_id,
        expected_endpoint_id=expected_endpoint_id,
        dialed_endpoint_id=dialed_endpoint_id,
        spec_digest=spec_digest,
        source_digest=source_digest,
        sidecar_binary_digest=sidecar_binary_digest,
        challenge=report["challenge"],
    )
    if (
        after_observation["endpoint_id"] != before_observation["endpoint_id"]
        or after_observation["monotonic_ns"] < before_observation["monotonic_ns"]
        or after["measured_at_unix_ms"] < before["measured_at_unix_ms"]
        or after["inbound_identity_rejections"]
        <= before["inbound_identity_rejections"]
        or after["candidate_identity_rejections"]
        <= before["candidate_identity_rejections"]
        or after["inbound_frames_admitted"] != before["inbound_frames_admitted"]
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return dict(report)

def _validated_supported_path_probe(
    report: Any, *, case_id: str, member_id: str
) -> dict[str, Any]:
    validated = _validated_case_probe_common(
        report,
        protocol="mycelium.a8_supported_path_probe.v1",
        case_id=case_id,
        member_id=member_id,
        fields={
            "enrollment_completed",
            "artifact_manifest_verified",
            "activation_completed",
            "serving_requests_completed",
            "transport_path_class",
            "tailscale_used",
            "ssh_used",
            "artifact_transport",
        },
    )
    if (
        validated["enrollment_completed"] is not True
        or validated["artifact_manifest_verified"] is not True
        or validated["activation_completed"] is not True
        or type(validated["serving_requests_completed"]) is not int
        or validated["serving_requests_completed"] < 1
        or validated["transport_path_class"] not in {"direct", "relay"}
        or validated["tailscale_used"] is not False
        or validated["ssh_used"] is not False
        or validated["artifact_transport"] != "signed_product_path"
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return validated


def _run_unrelated_invite_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    peer_network: Any,
    case_probe: Callable[[], Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Physical positive 1: HTTPS bootstrap, pinned seed, single-use invite,
    membership renewal, and visible ineligibility - with no tailnet path."""

    _require_live_boundary(adapter)
    outcomes: list[str] = []
    if adapter.pin_verified:
        outcomes.append("seed_pin_verified")
    acceptance = _enroll_peer(adapter, node, join_envelope)
    message = acceptance.get("message") or {}
    if message.get("membership_generation") is not None:
        outcomes.append("invite_redeemed")
    renewal = _renew_membership(adapter, node)
    if renewal is not None and (renewal.get("message") or {}).get(
        "lease_expires_at"
    ) is not None:
        outcomes.append("membership_renewed")
    outcomes.append(_peer_network_outcome(_resolve(peer_network)))
    if "no_tailnet_path_observed" in outcomes:
        probe = _validated_membership_visibility_probe(
            _resolve(case_probe), member_id=str(node.node_id)
        )
        outcomes.extend(
            ["https_bootstrap_succeeds", "signed_member_visible_but_ineligible"]
        )
        evidence_digests = [_evidence_digest(probe)]
    else:
        evidence_digests = []
    required = {
        "seed_pin_verified",
        "invite_redeemed",
        "membership_renewed",
        "no_tailnet_path_observed",
        "https_bootstrap_succeeds",
        "signed_member_visible_but_ineligible",
    }
    result = "passed" if required <= set(outcomes) else "failed"
    return _executed_envelope(
        case_id="unrelated_https_invite_without_tailscale",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=evidence_digests,
    )


def _await_control_refusal(
    adapter: PublicBootstrapClient,
    node: Any,
    *,
    deadline: float,
    poll_interval_seconds: float,
) -> bool:
    """Poll this member's own control path until the seed starts refusing it.

    The peer cannot reach the seed's owner-private administration plane - no
    admin route is publicly exposed - so it cannot cause the revocation. It
    waits for one, bounded by ``deadline``. Returning False means the window
    closed with control still accepted: the gate did not happen, and that is
    a failure rather than something to retry into a pass.
    """

    while _time_module.time() < deadline:
        try:
            _renew_membership(adapter, node)
        except EnrollmentError:
            return True
        _time_module.sleep(
            min(poll_interval_seconds, max(deadline - _time_module.time(), 0.0))
        )
    return False


def _refusal_is_durable(
    adapter: PublicBootstrapClient, node: Any, *, attempts: int = 3
) -> bool:
    """A revoked member stays refused. One transient error is not revocation."""

    for _ in range(attempts):
        try:
            _renew_membership(adapter, node)
        except EnrollmentError:
            continue
        return False
    return True


def _validated_revocation_connection_before(
    report: Any,
    *,
    member_id: str,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    validated = _validated_case_probe_common(
        report,
        protocol="mycelium.a8_revocation_connection_before.v1",
        case_id="revoked_active_member",
        member_id=member_id,
        fields={
            "spec_digest",
            "source_digest",
            "probe_session_id",
            "transport",
            "authenticated_connection",
            "activation_attempts",
            "activation_admissions",
            "path_class",
            "connection_generation",
        },
    )
    if (
        validated.get("spec_digest") != spec_digest
        or validated.get("source_digest") != source_digest
        or not isinstance(validated.get("probe_session_id"), str)
        or not validated["probe_session_id"]
        or validated.get("transport") != "iroh"
        or validated.get("authenticated_connection") is not True
        or type(validated.get("activation_attempts")) is not int
        or validated["activation_attempts"] < 1
        or type(validated.get("activation_admissions")) is not int
        or validated["activation_admissions"] < 1
        or validated.get("path_class") not in {"direct", "relay"}
        or type(validated.get("connection_generation")) is not int
        or validated["connection_generation"] < 1
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return validated


def _validated_revocation_connection_after(
    report: Any,
    *,
    member_id: str,
    spec_digest: str,
    source_digest: str,
    before_report: Mapping[str, Any],
) -> dict[str, Any]:
    validated = _validated_case_probe_common(
        report,
        protocol="mycelium.a8_revocation_connection_after.v1",
        case_id="revoked_active_member",
        member_id=member_id,
        fields={
            "spec_digest",
            "source_digest",
            "probe_session_id",
            "before_evidence_digest",
            "transport",
            "activation_attempts",
            "activation_admissions",
            "connection_state",
            "incident_detail",
        },
    )
    if (
        validated.get("spec_digest") != spec_digest
        or validated.get("source_digest") != source_digest
        or validated.get("probe_session_id") != before_report["probe_session_id"]
        or validated.get("before_evidence_digest") != _evidence_digest(before_report)
        or validated.get("transport") != "iroh"
        or type(validated.get("activation_attempts")) is not int
        or validated["activation_attempts"] < 1
        or validated.get("activation_admissions") != 0
        or validated.get("connection_state") not in {"closed", "quarantined"}
        or validated.get("incident_detail") != "bounded"
        or validated["observed_at_unix_ms"] < before_report["observed_at_unix_ms"]
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")
    return validated

def _run_revoked_member_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    revoke: Callable[[], None] | None,
    await_revocation_seconds: float | None,
    poll_interval_seconds: float,
    case_probe_before: Callable[[], Any],
    case_probe_after: Callable[[dict[str, Any]], Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Physical negative: revocation removes control and activation
    admission for an already-active external member.

    Two shapes. ``revoke`` drives it in-process, for a caller that holds the
    administration plane. ``await_revocation_seconds`` waits for an
    out-of-band revocation instead - the only shape available to a real
    external peer, which has no route to that plane.
    """

    _require_live_boundary(adapter)
    outcomes: list[str] = []
    _enroll_peer(adapter, node, join_envelope)
    if _renew_membership(adapter, node) is not None:
        outcomes.append("member_enrolled_before_revocation")
    before_probe = _validated_revocation_connection_before(
        _resolve(case_probe_before),
        member_id=str(node.node_id),
        spec_digest=spec_digest,
        source_digest=source_digest,
    )
    if revoke is not None:
        revoke()
        refused = True
        try:
            _renew_membership(adapter, node)
            refused = False
        except EnrollmentError:
            pass
    else:
        assert await_revocation_seconds is not None
        refused = _await_control_refusal(
            adapter,
            node,
            deadline=_time_module.time() + float(await_revocation_seconds),
            poll_interval_seconds=poll_interval_seconds,
        )
    if not refused:
        outcomes.append(
            "control_accepted_after_revocation"
            if revoke is not None
            else "revocation_not_observed"
        )
    else:
        outcomes.append("control_refused_after_revocation")
        # The bounded code the seed returns here is generation fencing, which
        # is not unique to revocation. Durability across retries is what
        # separates a fenced member from one transient control failure.
        if _refusal_is_durable(adapter, node):
            outcomes.append("refusal_durable_across_retries")
    if not refused:
        return _executed_envelope(
            case_id="revoked_active_member",
            result="failed",
            outcomes=outcomes,
            spec_digest=spec_digest,
            source_digest=source_digest,
            evidence_digests=[],
        )
    after_probe = _validated_revocation_connection_after(
        case_probe_after(before_probe),
        member_id=str(node.node_id),
        spec_digest=spec_digest,
        source_digest=source_digest,
        before_report=before_probe,
    )
    outcomes.extend(
        [
            "revocation_incident_bounded",
            "revoked_connection_removed_from_activation_admission",
            "revoked_control_rejected",
        ]
    )
    required = {
        "member_enrolled_before_revocation",
        "control_refused_after_revocation",
        "revocation_incident_bounded",
        "revoked_connection_removed_from_activation_admission",
        "revoked_control_rejected",
    }
    if revoke is None:
        required.add("refusal_durable_across_retries")
    result = "passed" if required <= set(outcomes) else "failed"
    return _executed_envelope(
        case_id="revoked_active_member",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[
            _evidence_digest(before_probe),
            _evidence_digest(after_probe),
        ],
    )


def _run_endpoint_mismatch_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    mismatched_node: Any,
    case_probe: Callable[[], Any],
    transport_authority: Any,
    sidecar_binary_digest: str,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Physical negative: a member presenting control under an endpoint
    identity other than its accepted one is refused, record unchanged."""

    _require_live_boundary(adapter)
    outcomes: list[str] = []
    acceptance = _enroll_peer(adapter, node, join_envelope)
    accepted_generation = (acceptance.get("message") or {}).get(
        "membership_generation"
    )
    # Resume is the honest vehicle: it claims the accepted membership record
    # while carrying the signer's own sender_endpoint_id, so the refusal comes
    # from the seed rejecting the identity - not from local session state.
    accepted_incarnation = (acceptance.get("message") or {}).get(
        "accepted_incarnation"
    )
    if accepted_generation is None or accepted_incarnation is None:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    resume_envelope = mismatched_node.resume_request(
        previous_generation=int(accepted_generation),
        previous_incarnation=str(accepted_incarnation),
        endpoint_addrs=[f"https://{mismatched_node.node_id}.invalid/control"],
    )
    try:
        adapter.resume(resume_envelope, now=_time_module.time())
        outcomes.append("mismatched_identity_accepted")
    except EnrollmentError as exc:
        # Only an identity refusal counts. Any other bounded failure would
        # satisfy a bare except while proving nothing about the gate.
        outcomes.append(
            "mismatched_identity_refused"
            if exc.code in _IDENTITY_MISMATCH_CODES
            else "refused_for_unrelated_reason"
        )
    renewal = _renew_membership(adapter, node)
    still_accepted = (
        renewal is not None
        and (renewal.get("message") or {}).get("membership_generation")
        == accepted_generation
    )
    if still_accepted:
        outcomes.append("member_record_unchanged")
    required_control = {"mismatched_identity_refused", "member_record_unchanged"}
    if required_control <= set(outcomes):
        probe = _validated_endpoint_activation_probe(
            _resolve(case_probe),
            member_id=str(node.node_id),
            expected_endpoint_id=str(node.signer.endpoint_id),
            dialed_endpoint_id=str(mismatched_node.signer.endpoint_id),
            spec_digest=spec_digest,
            source_digest=source_digest,
            sidecar_binary_digest=sidecar_binary_digest,
            transport_authority=transport_authority,
        )
        outcomes.extend(
            [
                "endpoint_mismatch_rejected",
                "no_activation_frame_accepted",
                "path_class_remains_unknown",
            ]
        )
        evidence_digests = [_evidence_digest(probe)]
    else:
        evidence_digests = []
    required = required_control | {
        "endpoint_mismatch_rejected",
        "no_activation_frame_accepted",
        "path_class_remains_unknown",
    }
    result = "passed" if required <= set(outcomes) else "failed"
    return _executed_envelope(
        case_id="endpoint_identity_mismatch",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=evidence_digests,
    )


_SIGNED_NODE_FIELDS = {
    "protocol",
    "event",
    "monotonic_ns",
    "run_id",
    "deployment_id",
    "node_id",
    "host_id",
    "process_id",
    "endpoint_id",
    "peer_generation",
    "state",
    "route_ready",
    "details",
}
_SWARM_REPORT_FIELDS = {
    "protocol",
    "captured_at_unix_ms",
    "deployment_id",
    "model_id",
    "resolved_commit",
    "placement",
    "topology",
    "signed_snapshots",
    "route_ready",
}
_BROWSER_REPORT_FIELDS = {
    "protocol",
    "origin",
    "challenge_id",
    "case_id",
    "deployment_id",
    "spec_digest",
    "source_digest",
    "observed_at_unix_ms",
    "passed",
    "browser_failures",
    "completed_requests",
    "request_ids",
    "terminal_states",
    "transport_report_digests",
    "workspaces",
    "public_projection",
}
_BROWSER_ENVELOPE_FIELDS = {"protocol", "observation", "signature"}
_BROWSER_AUTHORITY_FIELDS = {
    "protocol",
    "signer_id",
    "verification_keys",
    "challenge_id",
    "case_id",
    "origin",
    "deployment_id",
    "spec_digest",
    "source_digest",
    "request_count",
    "issued_at_unix_ms",
    "expires_at_unix_ms",
}
_A8_WORKSPACES = {
    "lab",
    "network",
    "plans",
    "incidents",
    "readiness",
    "nodes",
    "settings",
    "inference",
}
_TRANSPORT_REPORT_MAX_AGE_MS = 5 * 60 * 1_000
_TRANSPORT_REPORT_FUTURE_SKEW_MS = 30 * 1_000


def _evidence_digest(document: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json_bytes(dict(document))).hexdigest()


def _validated_transport_authority(
    authority: Any,
) -> tuple[str, dict[str, str], str]:
    if (
        not isinstance(authority, Mapping)
        or set(authority) != {"protocol", "deployment_id", "endpoints"}
        or authority.get("protocol") != "mycelium.a8_transport_authority.v1"
        or not isinstance(authority.get("deployment_id"), str)
        or re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", authority["deployment_id"]
        )
        is None
        or not isinstance(authority.get("endpoints"), list)
        or not 1 <= len(authority["endpoints"]) <= 64
    ):
        raise PhysicalGateError("transport_observation_signature_invalid")
    trusted: dict[str, str] = {}
    key_digests: set[str] = set()
    for endpoint in authority["endpoints"]:
        if (
            not isinstance(endpoint, Mapping)
            or set(endpoint) != {"endpoint_id", "verification_key_digest"}
            or not isinstance(endpoint.get("endpoint_id"), str)
            or not 1 <= len(endpoint["endpoint_id"]) <= 256
            or not isinstance(endpoint.get("verification_key_digest"), str)
            or re.fullmatch(
                r"sha256:[0-9a-f]{64}", endpoint["verification_key_digest"]
            )
            is None
            or endpoint["verification_key_digest"] == "sha256:" + "0" * 64
            or endpoint["endpoint_id"] in trusted
            or endpoint["verification_key_digest"] in key_digests
        ):
            raise PhysicalGateError("transport_observation_signature_invalid")
        trusted[endpoint["endpoint_id"]] = endpoint["verification_key_digest"]
        key_digests.add(endpoint["verification_key_digest"])
    return str(authority["deployment_id"]), trusted, _evidence_digest(authority)


def _verified_transport_reports(
    reports: Any,
    authority: Any,
    *,
    now_unix_ms: int | None = None,
) -> tuple[list[list[dict[str, Any]]], list[str], str]:
    if not isinstance(reports, list) or not reports:
        raise PeerRequired()
    gate_now = _now_unix_ms() if now_unix_ms is None else now_unix_ms
    if type(gate_now) is not int:
        raise PhysicalGateError("transport_observation_invalid")
    authority_deployment_id, trusted_keys, authority_digest = (
        _validated_transport_authority(authority)
    )
    all_observations: list[list[dict[str, Any]]] = []
    digests: list[str] = [authority_digest]
    deployment_id: str | None = None
    for report in reports:
        if (
            not isinstance(report, Mapping)
            or set(report) != _SWARM_REPORT_FIELDS
            or report.get("protocol")
            != "mycelium.live_swarm_resource_observations.v1"
            or report.get("route_ready") is not False
            or type(report.get("captured_at_unix_ms")) is not int
            or not isinstance(report.get("deployment_id"), str)
            or not isinstance(report.get("signed_snapshots"), list)
            or not report["signed_snapshots"]
        ):
            raise PhysicalGateError("transport_observation_invalid")
        if deployment_id is None:
            deployment_id = str(report["deployment_id"])
        elif report["deployment_id"] != deployment_id:
            raise PhysicalGateError("transport_observation_invalid")
        if report["deployment_id"] != authority_deployment_id:
            raise PhysicalGateError("transport_observation_signature_invalid")
        captured = int(report["captured_at_unix_ms"])
        if not (
            gate_now - _TRANSPORT_REPORT_MAX_AGE_MS
            <= captured
            <= gate_now + _TRANSPORT_REPORT_FUTURE_SKEW_MS
        ):
            raise PhysicalGateError("transport_observation_invalid")
        report_observations: list[dict[str, Any]] = []
        for signed in report["signed_snapshots"]:
            if not isinstance(signed, Mapping) or set(signed) != {
                "observation",
                "signature",
                "verification_key",
            }:
                raise PhysicalGateError("transport_observation_invalid")
            observation = signed.get("observation")
            signature = signed.get("signature")
            verification_key = signed.get("verification_key")
            if (
                not isinstance(observation, Mapping)
                or set(observation) != _SIGNED_NODE_FIELDS
                or observation.get("protocol")
                != "mycelium.physical_node_observation.v1"
                or observation.get("event") != "snapshot"
                or observation.get("route_ready") is not False
                or observation.get("deployment_id") != deployment_id
                or not isinstance(observation.get("endpoint_id"), str)
                or not isinstance(signature, Mapping)
                or not isinstance(verification_key, Mapping)
                or signature.get("signer_endpoint_id")
                != observation.get("endpoint_id")
            ):
                raise PhysicalGateError("transport_observation_invalid")
            if trusted_keys.get(str(observation["endpoint_id"])) != verification_key.get(
                "verification_key_digest"
            ):
                raise PhysicalGateError("transport_observation_signature_invalid")
            try:
                verifier = build_ed25519_verifier([verification_key])
                valid = verifier(
                    canonical_json_bytes(dict(observation)), dict(signature)
                )
            except (EvidenceSigningError, TypeError, ValueError) as exc:
                raise PhysicalGateError(
                    "transport_observation_signature_invalid"
                ) from exc
            if valid is not True:
                raise PhysicalGateError("transport_observation_signature_invalid")
            details = observation.get("details")
            transport = details.get("transport") if isinstance(details, Mapping) else None
            paths = (
                transport.get("transport_path_observations")
                if isinstance(transport, Mapping)
                else None
            )
            if paths is None:
                continue
            if not isinstance(paths, list):
                raise PhysicalGateError("transport_observation_invalid")
            for value in paths:
                try:
                    path = validate_transport_path_observation(
                        value,
                        now_unix_ms=captured,
                        require_resolved=True,
                    )
                except (TypeError, ValueError) as exc:
                    raise PhysicalGateError("transport_observation_invalid") from exc
                if (
                    not (
                        gate_now - _TRANSPORT_REPORT_MAX_AGE_MS
                        <= path["measured_at_unix_ms"]
                        <= gate_now + _TRANSPORT_REPORT_FUTURE_SKEW_MS
                    )
                    or path["fresh_until_unix_ms"] <= gate_now
                    or
                    path["local_node_id"] != observation.get("node_id")
                    or path["local_endpoint_id"] != observation.get("endpoint_id")
                    or path["remote_endpoint_id"] not in trusted_keys
                ):
                    raise PhysicalGateError("transport_observation_invalid")
                report_observations.append(path)
        if not report_observations:
            raise PhysicalGateError("transport_observation_invalid")
        all_observations.append(report_observations)
        digests.append(_evidence_digest(report))
    assert deployment_id is not None
    return all_observations, digests, deployment_id


def _projected_metric(value: Any) -> int:
    return max(0, int(round(float(value))))


def _activation_matches_path(
    activation: Mapping[str, Any], path: Mapping[str, Any]
) -> bool:
    endpoint_pseudonym = "sha256:" + hashlib.sha256(
        str(path["remote_endpoint_id"]).encode("utf-8")
    ).hexdigest()
    metrics = activation["metrics"]
    return bool(
        activation["endpoint_pseudonym"] == endpoint_pseudonym
        and activation["connection_generation"] == path["connection_generation"]
        and activation["connection_reuse"]
        == path["frames_sent"] - path["connections_opened"]
        and activation["path_class"] == path["path_class"]
        and activation["observed_at_unix_ms"] == path["measured_at_unix_ms"]
        and metrics["rtt_ms"] == _projected_metric(path["cold_rtt_ms"])
        and metrics["warm_rtt_ms"] == _projected_metric(path["warm_rtt_ms"])
        and metrics["jitter_ms"] == _projected_metric(path["jitter_ms"])
        and metrics["goodput_bytes_per_second"]
        == _projected_metric(path["observed_goodput_Bps"])
        and metrics["loss_ratio"] == path["loss_ratio"]
        and metrics["sample_count"] == path["sample_count"]
        and metrics["measured_zero"] == (path["loss_ratio"] == 0.0)
    )


def _browser_signature_value(value: Any) -> list[Any]:
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["boolean", value]
    if isinstance(value, str):
        return ["string", value]
    if type(value) is int:
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("browser integer outside JavaScript safe range")
        return ["integer", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("browser number is not finite")
        if value.is_integer():
            integer = int(value)
            if abs(integer) > 9_007_199_254_740_991:
                raise ValueError("browser integer outside JavaScript safe range")
            return ["integer", str(integer)]
        return ["float64", struct.pack(">d", value).hex()]
    if isinstance(value, list):
        return ["array", [_browser_signature_value(item) for item in value]]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("browser object key is not a string")
        return [
            "object",
            [
                [key, _browser_signature_value(value[key])]
                for key in sorted(value)
            ],
        ]
    raise ValueError("browser observation is not canonical")


def _browser_signature_statement(observation: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "protocol": "mycelium.a8_browser_signature_statement.v1",
        "value": _browser_signature_value(dict(observation)),
    }


def _browser_statement_bytes(observation: Mapping[str, Any]) -> bytes:
    return canonical_json_bytes(_browser_signature_statement(observation))


def _verified_browser_observation(
    envelope: Any,
    authority: Any,
    *,
    expected_case_id: str,
    expected_origin: str,
    expected_deployment_id: str,
    expected_spec_digest: str,
    expected_source_digest: str,
    expected_transport_report_digests: list[str],
    now_unix_ms: int | None = None,
) -> dict[str, Any]:
    gate_now = _now_unix_ms() if now_unix_ms is None else now_unix_ms
    if type(gate_now) is not int:
        raise PhysicalGateError("browser_observation_invalid")
    if (
        not isinstance(envelope, Mapping)
        or set(envelope) != _BROWSER_ENVELOPE_FIELDS
        or envelope.get("protocol")
        != "mycelium.a8_product_browser_observation_envelope.v2"
        or not isinstance(authority, Mapping)
        or set(authority) != _BROWSER_AUTHORITY_FIELDS
        or authority.get("protocol")
        != "mycelium.a8_browser_observation_authority.v2"
        or not isinstance(authority.get("signer_id"), str)
        or not authority["signer_id"]
        or not isinstance(authority.get("verification_keys"), list)
        or len(authority["verification_keys"]) != 1
    ):
        raise PhysicalGateError("browser_observation_signature_invalid")
    challenge_id = authority.get("challenge_id")
    request_count = authority.get("request_count")
    issued_at = authority.get("issued_at_unix_ms")
    expires_at = authority.get("expires_at_unix_ms")
    if (
        not isinstance(challenge_id, str)
        or re.fullmatch(r"sha256:[0-9a-f]{64}", challenge_id) is None
        or challenge_id == "sha256:" + "0" * 64
        or authority.get("case_id") != expected_case_id
        or authority.get("origin") != expected_origin
        or authority.get("deployment_id") != expected_deployment_id
        or authority.get("spec_digest") != expected_spec_digest
        or authority.get("source_digest") != expected_source_digest
        or type(request_count) is not int
        or not 1 <= request_count <= 8
        or type(issued_at) is not int
        or type(expires_at) is not int
        or issued_at > gate_now + _TRANSPORT_REPORT_FUTURE_SKEW_MS
        or expires_at < gate_now
        or expires_at <= issued_at
        or expires_at - issued_at > _TRANSPORT_REPORT_MAX_AGE_MS
    ):
        raise PhysicalGateError("browser_observation_invalid")
    observation = envelope.get("observation")
    signature = envelope.get("signature")
    if not isinstance(observation, Mapping) or not isinstance(signature, dict):
        raise PhysicalGateError("browser_observation_signature_invalid")
    if signature.get("signer_endpoint_id") != authority["signer_id"]:
        raise PhysicalGateError("browser_observation_signature_invalid")
    try:
        verifier = build_ed25519_verifier(authority["verification_keys"])
        valid = verifier(_browser_statement_bytes(observation), signature)
    except (EvidenceSigningError, EvidenceValidationError, TypeError, ValueError) as exc:
        raise PhysicalGateError("browser_observation_signature_invalid") from exc
    if valid is not True:
        raise PhysicalGateError("browser_observation_signature_invalid")
    observed_at = observation.get("observed_at_unix_ms")
    request_ids = observation.get("request_ids")
    transport_digests = observation.get("transport_report_digests")
    if (
        type(observed_at) is not int
        or not gate_now - _TRANSPORT_REPORT_MAX_AGE_MS
        <= observed_at
        <= gate_now + _TRANSPORT_REPORT_FUTURE_SKEW_MS
        or not issued_at <= observed_at <= expires_at
        or observation.get("protocol")
        != "mycelium.a8_product_browser_observation.v2"
        or observation.get("challenge_id") != challenge_id
        or observation.get("case_id") != expected_case_id
        or observation.get("origin") != expected_origin
        or observation.get("deployment_id") != expected_deployment_id
        or observation.get("spec_digest") != expected_spec_digest
        or observation.get("source_digest") != expected_source_digest
        or observation.get("completed_requests") != request_count
        or not isinstance(request_ids, list)
        or len(request_ids) != request_count
        or len(set(request_ids)) != len(request_ids)
        or any(
            not isinstance(request_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", request_id) is None
            for request_id in request_ids
        )
        or transport_digests != expected_transport_report_digests
    ):
        raise PhysicalGateError("browser_observation_invalid")
    return dict(observation)


def _validated_browser_report(
    report: Mapping[str, Any],
    *,
    observations: list[dict[str, Any]],
    relay_projection_key: Any,
) -> tuple[dict[str, Any], str | None]:
    if (
        not isinstance(report, Mapping)
        or set(report) != _BROWSER_REPORT_FIELDS
        or report.get("protocol")
        != "mycelium.a8_product_browser_observation.v2"
        or report.get("passed") is not True
        or type(report.get("browser_failures")) is not int
        or report.get("browser_failures") != 0
        or type(report.get("completed_requests")) is not int
        or report["completed_requests"] < 1
        or not isinstance(report.get("terminal_states"), list)
        or len(report["terminal_states"]) != report["completed_requests"]
        or any(value != "completed" for value in report["terminal_states"])
        or not isinstance(report.get("workspaces"), list)
        or set(report["workspaces"]) != _A8_WORKSPACES
        or len(report["workspaces"]) != len(_A8_WORKSPACES)
    ):
        raise PhysicalGateError("browser_observation_invalid")
    public = report.get("public_projection")
    if not isinstance(public, Mapping) or set(public) != {
        "activation_observation",
        "activation_history",
        "relay_projection",
    }:
        raise PhysicalGateError("browser_observation_invalid")
    activation = public.get("activation_observation")
    history = public.get("activation_history")
    relay = public.get("relay_projection")
    try:
        if not isinstance(activation, Mapping):
            raise ValueError
        activation_document = validate_activation_observation(activation)
        if not isinstance(history, list) or not 1 <= len(history) <= 64:
            raise ValueError
        history_documents = [validate_activation_observation(item) for item in history]
        relay_document = (
            None
            if relay is None
            else validate_relay_projection(relay)
        )
        needles = {
            str(value)
            for path in observations
            for key in (
                "local_node_id",
                "local_endpoint_id",
                "remote_node_id",
                "remote_endpoint_id",
                "relay_identity",
            )
            if (value := path.get(key)) is not None
        }
        ensure_privacy_clean(public, forbidden_needles=needles)
    except (TypeError, ValueError) as exc:
        raise PhysicalGateError("browser_observation_invalid") from exc
    matching_paths = [
        path for path in observations if _activation_matches_path(activation_document, path)
    ]
    if not matching_paths or any(
        not any(_activation_matches_path(item, path) for path in observations)
        for item in history_documents
    ):
        raise PhysicalGateError("browser_observation_invalid")
    relay_reference = None
    if activation_document["path_class"] == "relay":
        if (
            not isinstance(relay_projection_key, bytes)
            or len(relay_projection_key) < 32
            or relay_document is None
        ):
            raise PhysicalGateError("relay_projection_key_invalid")
        matching_relay = next(
            (
                path
                for path in matching_paths
                if isinstance(path.get("relay_identity"), str)
            ),
            None,
        )
        if matching_relay is None:
            raise PhysicalGateError("browser_observation_invalid")
        expected_reference = RelayProjector(
            projection_key=relay_projection_key
        ).reference(str(matching_relay["relay_identity"]))
        if relay_document["relay_reference"] != expected_reference:
            raise PhysicalGateError("browser_observation_invalid")
        relay_reference = expected_reference
    elif relay_document is not None:
        raise PhysicalGateError("browser_observation_invalid")
    return dict(report), relay_reference


def _run_browser_transport_case(
    case_id: str,
    *,
    inputs: Mapping[str, Any],
    origin: str,
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    browser_envelope = inputs.get("browser_report")
    transport_reports = inputs.get("transport_reports")
    if browser_envelope is None or transport_reports is None:
        raise PeerRequired()
    reports, transport_digests, deployment_id = _verified_transport_reports(
        transport_reports, inputs.get("transport_authority")
    )
    observations = [path for report in reports for path in report]
    browser_observation = _verified_browser_observation(
        browser_envelope,
        inputs.get("browser_authority"),
        expected_case_id=case_id,
        expected_origin=origin,
        expected_deployment_id=deployment_id,
        expected_spec_digest=spec_digest,
        expected_source_digest=source_digest,
        # `_verified_transport_reports` prefixes the owner transport-authority
        # digest; browser requests bind only the chronological signed reports.
        expected_transport_report_digests=transport_digests[1:],
    )
    browser, relay_reference = _validated_browser_report(
        browser_observation,
        observations=observations,
        relay_projection_key=inputs.get("relay_projection_key"),
    )
    browser_digest = _evidence_digest(browser_envelope)
    browser_authority_digest = _evidence_digest(inputs["browser_authority"])
    last = reports[-1]
    activation = browser["public_projection"]["activation_observation"]
    current_matches_last = any(
        _activation_matches_path(activation, path) for path in last
    )
    outcomes: list[str] = []

    if case_id == "direct_path_qualified_browser_inference":
        outcomes.append(
            "direct_path_observed"
            if current_matches_last
            and activation["path_class"] == "direct"
            and all(path["path_class"] == "direct" for path in last)
            else "direct_path_not_observed"
        )
        outcomes.extend(
            ["browser_inference_completes", "positive_physical_counters"]
        )
        required = {
            "direct_path_observed",
            "browser_inference_completes",
            "positive_physical_counters",
        }
    elif case_id == "forced_relay_privacy_reduced_browser_inference":
        relay_observed = bool(
            current_matches_last
            and activation["path_class"] == "relay"
            and all(path["path_class"] == "relay" for path in last)
        )
        outcomes.append(
            "relay_path_observed" if relay_observed else "relay_path_not_observed"
        )
        outcomes.append("browser_inference_completes")
        outcomes.append(
            "relay_identity_privacy_reduced"
            if relay_reference is not None
            else "relay_identity_not_projected"
        )
        if relay_observed and relay_reference is not None:
            outcomes.append("privacy_safe_relay_reference_only")
        outcomes.append(
            "no_http_inference_fallback"
            if relay_observed
            else "physical_transport_not_observed"
        )
        required = {
            "relay_path_observed",
            "browser_inference_completes",
            "relay_identity_privacy_reduced",
            "no_http_inference_fallback",
        }
    else:
        first_by_edge = {
            (path["local_endpoint_id"], path["remote_endpoint_id"]): path
            for path in reports[0]
        }
        last_by_edge = {
            (path["local_endpoint_id"], path["remote_endpoint_id"]): path
            for path in last
        }
        transitions = [
            (before, after)
            for edge, before in first_by_edge.items()
            if (after := last_by_edge.get(edge)) is not None
            and before["path_class"] != after["path_class"]
            and before["connection_generation"] < after["connection_generation"]
        ]
        history = browser["public_projection"]["activation_history"]
        retained = any(
            any(_activation_matches_path(item, before) for item in history)
            and any(_activation_matches_path(item, after) for item in history)
            for before, after in transitions
        )
        reconnect = any(
            (
                after["reconnect_count"] > before["reconnect_count"]
                and after["selected_path_changes"] > before["selected_path_changes"]
            )
            or (
                before["connections_opened"] > 0
                and after["connections_opened"] > 0
                and before["frames_sent"] > before["connections_opened"]
                and after["frames_sent"] > after["connections_opened"]
            )
            for before, after in transitions
        )
        current_transition = any(
            _activation_matches_path(activation, after)
            for _, after in transitions
        )
        outcomes.extend(
            [
                "transition_generations_retained"
                if retained
                else "transition_generations_missing",
                "connection_reuse_observed"
                if reconnect
                else "connection_reuse_not_observed",
                "stale_connection_not_reused"
                if current_transition
                else "stale_connection_reused",
                "browser_inference_completes_after_transition"
                if browser["completed_requests"] >= 2 and current_matches_last
                else "browser_inference_missing_after_transition",
            ]
        )
        if browser["completed_requests"] >= 2 and current_matches_last:
            outcomes.append("subsequent_request_completes")
        required = {
            "transition_generations_retained",
            "connection_reuse_observed",
            "stale_connection_not_reused",
            "browser_inference_completes_after_transition",
        }
    return _executed_envelope(
        case_id=case_id,
        result="passed" if required <= set(outcomes) else "failed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[browser_digest, browser_authority_digest, *transport_digests],
        relay_reference=relay_reference,
        evidence_fresh_until_unix_ms=min(
            path["fresh_until_unix_ms"] for path in observations
        ),
        qualification_nonce=browser["challenge_id"],
    )


def _run_unqualified_member_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    authority_probe: Callable[[], Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Physical negative: an enrolled member without serve authority stays
    visible but receives no artifact, placement, activation, selection, or
    prompt traffic.

    The injected probe is an execution seam, not an assertion seam: the live
    CLI supplies a program that exercises the owner-side product authorities
    after this function enrolls the external member. Its exact closed report
    is reduced to privacy-safe outcomes here.
    """

    _require_live_boundary(adapter)
    _enroll_peer(adapter, node, join_envelope)
    if _renew_membership(adapter, node) is None:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    report = _resolve(authority_probe)
    expected_fields = {
        "protocol",
        "member_id",
        "member_visible",
        "activation_eligible",
        "authority_attempts",
        "forbidden_side_effects",
        "prompt_deliveries",
    }
    if not isinstance(report, Mapping) or set(report) != expected_fields:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    attempts = report.get("authority_attempts")
    side_effects = report.get("forbidden_side_effects")
    attempt_names = {"artifact", "placement", "activation", "selection", "inference"}
    side_effect_names = {
        "artifact_disclosed",
        "placement_created",
        "deployment_selected",
    }
    if (
        report.get("protocol")
        != "mycelium.unqualified_member_authority_probe.v1"
        or report.get("member_id") != getattr(node, "node_id", None)
        or not isinstance(report.get("member_visible"), bool)
        or not isinstance(report.get("activation_eligible"), bool)
        or not isinstance(attempts, Mapping)
        or set(attempts) != attempt_names
        or any(value not in {"accepted", "rejected"} for value in attempts.values())
        or not isinstance(side_effects, Mapping)
        or set(side_effects) != side_effect_names
        or any(not isinstance(value, bool) for value in side_effects.values())
        or isinstance(report.get("prompt_deliveries"), bool)
        or not isinstance(report.get("prompt_deliveries"), int)
        or int(report["prompt_deliveries"]) < 0
    ):
        raise PhysicalGateError("physical_infrastructure_unavailable")

    outcomes: list[str] = []
    if report["member_visible"] and not report["activation_eligible"]:
        outcomes.append("member_visible_but_ineligible")
    else:
        outcomes.append("member_not_visible_or_eligible")
    if all(value == "rejected" for value in attempts.values()) and not any(
        side_effects.values()
    ):
        outcomes.append("all_serve_authorities_rejected")
    else:
        outcomes.append(
            "forbidden_side_effect_observed"
            if any(side_effects.values())
            else "serve_authority_accepted"
        )
    if report["prompt_deliveries"] == 0:
        outcomes.append("no_prompt_delivery")
    else:
        outcomes.append("prompt_delivery_observed")
    required = {
        "member_visible_but_ineligible",
        "all_serve_authorities_rejected",
        "no_prompt_delivery",
    }
    report_bytes = json.dumps(
        report,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    report_digest = "sha256:" + hashlib.sha256(report_bytes).hexdigest()
    return _executed_envelope(
        case_id="unqualified_external_member",
        result="passed" if required <= set(outcomes) else "failed",
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[report_digest],
    )


def _run_independence_case(
    case_id: str,
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    inputs: Mapping[str, Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    """Physical negatives ``tailscale_unavailable`` and ``ssh_unavailable``:
    bootstrap, join, and renewal complete over the public origin with no
    tailnet path, and with no SSH invocation, anywhere in the window."""

    _require_live_boundary(adapter)
    outcomes: list[str] = []
    if case_id == "tailscale_unavailable":
        outcomes.append(
            _peer_network_outcome(_resolve(inputs.get("peer_network_before")))
        )
    _enroll_peer(adapter, node, join_envelope)
    if _renew_membership(adapter, node) is not None:
        outcomes.append("bootstrap_completed_over_public_origin")
    if case_id == "tailscale_unavailable":
        # Resolved HERE, after the window, so the observation reflects the
        # end of the window rather than a value captured before it opened.
        outcomes.append(
            _peer_network_outcome(_resolve(inputs.get("peer_network_after")))
        )
        required = {
            "no_tailnet_path_observed",
            "bootstrap_completed_over_public_origin",
        }
        result = (
            "failed"
            if "tailnet_path_present" in outcomes
            else ("passed" if required <= set(outcomes) else "failed")
        )
        if result == "passed":
            probe = _validated_supported_path_probe(
                _resolve(inputs.get("case_probe")),
                case_id=case_id,
                member_id=str(node.node_id),
            )
            outcomes.extend(
                [
                    "no_tailnet_address_or_evidence",
                    "ordinary_internet_path_observed",
                    "supported_path_works_without_tailscale",
                ]
            )
            evidence_digests = [_evidence_digest(probe)]
        else:
            evidence_digests = []
    else:
        audit = _resolve(inputs.get("peer_process_audit"))
        if not isinstance(audit, Mapping):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        invocations = audit.get("ssh_invocations")
        if isinstance(invocations, bool) or not isinstance(invocations, int):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        outcomes.append(
            "no_ssh_invocation_in_window"
            if invocations == 0
            else "ssh_invoked_in_window"
        )
        # Recorded truthfully rather than hidden: an ssh binary may exist on
        # the host as an owner-staging tool. The gate proves the supported
        # path does not require it, not that the host lacks it.
        if invocations == 0 and (
            audit.get("ssh_client_present") or audit.get("ssh_server_present")
        ):
            outcomes.append("ssh_present_but_unused")
        required = {
            "no_ssh_invocation_in_window",
            "bootstrap_completed_over_public_origin",
        }
        result = "passed" if required <= set(outcomes) else "failed"
        if result == "passed":
            probe = _validated_supported_path_probe(
                _resolve(inputs.get("case_probe")),
                case_id=case_id,
                member_id=str(node.node_id),
            )
            outcomes.extend(
                [
                    "no_remote_shell_attempted",
                    "signed_artifact_path_only",
                    "supported_path_works_without_ssh",
                ]
            )
            evidence_digests = [_evidence_digest(probe)]
        else:
            evidence_digests = []
    return _executed_envelope(
        case_id=case_id,
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=evidence_digests,
    )


def _run_peer_case(
    case_id: str,
    adapter: PublicBootstrapClient,
    inputs: Mapping[str, Any],
    spec_digest: str,
    source_digest: str,
) -> dict[str, Any]:
    node = inputs.get("node")
    join_envelope = inputs.get("join_envelope")
    if node is None or not isinstance(join_envelope, Mapping):
        raise PeerRequired()
    case_probe = inputs.get("case_probe")
    if case_id == "revoked_active_member":
        case_probe_before = inputs.get("case_probe_before")
        case_probe_after = inputs.get("case_probe_after")
        if not callable(case_probe_before) or not callable(case_probe_after):
            raise PeerRequired()
    elif case_id in {
        "unrelated_https_invite_without_tailscale",
        "endpoint_identity_mismatch",
        "tailscale_unavailable",
        "ssh_unavailable",
    } and not callable(case_probe):
        raise PeerRequired()
    if case_id == "unrelated_https_invite_without_tailscale":
        if "peer_network" not in inputs:
            raise PeerRequired()
        return _run_unrelated_invite_case(
            adapter,
            node=node,
            join_envelope=join_envelope,
            peer_network=inputs.get("peer_network"),
            case_probe=case_probe,
            spec_digest=spec_digest,
            source_digest=source_digest,
        )
    if case_id == "revoked_active_member":
        revoke = inputs.get("revoke")
        await_seconds = inputs.get("await_revocation_seconds")
        if not callable(revoke):
            revoke = None
            if (
                isinstance(await_seconds, bool)
                or not isinstance(await_seconds, (int, float))
                or not 0.0 < float(await_seconds) <= 3600.0
            ):
                raise PeerRequired()
        poll = inputs.get("poll_interval_seconds", 2.0)
        if (
            isinstance(poll, bool)
            or not isinstance(poll, (int, float))
            or not 0.0 < float(poll) <= 60.0
        ):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        return _run_revoked_member_case(
            adapter,
            node=node,
            join_envelope=join_envelope,
            revoke=revoke,
            await_revocation_seconds=(
                None if revoke is not None else float(await_seconds)
            ),
            poll_interval_seconds=float(poll),
            case_probe_before=case_probe_before,
            case_probe_after=case_probe_after,
            spec_digest=spec_digest,
            source_digest=source_digest,
        )
    if case_id == "endpoint_identity_mismatch":
        mismatched_node = inputs.get("mismatched_node")
        if mismatched_node is None:
            raise PeerRequired()
        return _run_endpoint_mismatch_case(
            adapter,
            node=node,
            join_envelope=join_envelope,
            mismatched_node=mismatched_node,
            case_probe=case_probe,
            transport_authority=inputs.get("transport_authority"),
            sidecar_binary_digest=inputs.get("sidecar_binary_digest"),
            spec_digest=spec_digest,
            source_digest=source_digest,
        )
    if case_id == "unqualified_external_member":
        authority_probe = inputs.get("authority_probe")
        if not callable(authority_probe):
            raise PeerRequired()
        return _run_unqualified_member_case(
            adapter,
            node=node,
            join_envelope=join_envelope,
            authority_probe=authority_probe,
            spec_digest=spec_digest,
            source_digest=source_digest,
        )
    if case_id == "tailscale_unavailable":
        if (
            "peer_network_before" not in inputs
            or "peer_network_after" not in inputs
        ):
            raise PeerRequired()
    elif "peer_process_audit" not in inputs:
        raise PeerRequired()
    return _run_independence_case(
        case_id,
        adapter,
        node=node,
        join_envelope=join_envelope,
        inputs=inputs,
        spec_digest=spec_digest,
        source_digest=source_digest,
    )


def execute_case(
    case_id: str,
    *,
    origin: str,
    evidence_root: Path | None,
    spec_digest: str,
    source_digest: str,
    adapter: PublicBootstrapClient | None = None,
    case_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Execute one physical case if, and only if, its inputs exist.

    ``case_inputs`` carries per-case procedure inputs:
    ``probe`` for the cleartext case (network observation, injectable for
    deterministic tests), and ``first_join_envelope``/
    ``second_join_envelope``/``second_adapter`` for the replay case.
    """

    spec_digest = _require_source_binding(spec_digest)
    source_digest = _verify_default_source_binding(source_digest)
    if case_id not in A8_PHYSICAL_CASES:
        raise PhysicalGateError("case_unknown")
    try:
        canonical_https_origin(origin)
    except ValueError as exc:
        raise PhysicalGateError("origin_invalid") from exc
    if case_id in PEER_REQUIRED_CASES:
        if case_id in _UI_DEPENDENT_CASES:
            if case_inputs is None:
                raise PeerRequired()
            return _run_browser_transport_case(
                case_id,
                inputs=case_inputs,
                origin=origin,
                spec_digest=spec_digest,
                source_digest=source_digest,
            )
        if adapter is None:
            raise PeerRequired()
        return _run_peer_case(
            case_id,
            adapter,
            dict(case_inputs or {}),
            spec_digest,
            source_digest,
        )
    if case_id in _PROJECTION_ONLY_CASES:
        return _run_projection_cases(case_id, spec_digest, source_digest)
    if adapter is None:
        raise PhysicalGateError("physical_infrastructure_unavailable")
    inputs = dict(case_inputs or {})
    if case_id == "cleartext_or_redirect_bootstrap":
        probe: Callable[[str], str] = inputs.get(  # type: ignore[assignment]
            "probe", probe_bootstrap_over_cleartext
        )
        if not callable(probe):
            raise PhysicalGateError("case_unknown")
        return _run_cleartext_case(
            adapter, origin, spec_digest, source_digest, probe=probe
        )
    if case_id == "certificate_without_seed_authority":
        return _run_certificate_case(adapter, spec_digest, source_digest)
    if case_id == "invalid_or_replayed_invitation":
        first = inputs.get("first_join_envelope")
        second = inputs.get("second_join_envelope")
        second_adapter = inputs.get("second_adapter")
        case_probe = inputs.get("case_probe")
        if (
            not isinstance(first, Mapping)
            or not isinstance(second, Mapping)
            or not isinstance(second_adapter, PublicBootstrapClient)
            or not callable(case_probe)
        ):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        return _run_replay_case(
            adapter,
            first_join_envelope=first,
            second_join_envelope=second,
            second_adapter=second_adapter,
            case_probe=case_probe,
            spec_digest=spec_digest,
            source_digest=source_digest,
        )
    raise PhysicalGateError("physical_infrastructure_unavailable")


def seal_qualification(
    document: Mapping[str, Any],
    *,
    evidence_root: Path,
) -> Path:
    """Validate and lock one executed qualification record into the
    owner-private evidence root. Sealing never fabricates a result."""

    data = dict(document)
    validate_internet_native_qualification(data)
    if data.get("executed") is not True or data.get("result") != "passed":
        raise PhysicalGateError("qualification_not_passed")
    root = Path(evidence_root)
    _require_source_binding(data.get("spec_digest"))
    _verify_default_source_binding(data.get("source_digest"))
    qualification_id = data["qualification_id"]
    if not _CODE_RE.fullmatch(qualification_id) and not re.fullmatch(
        r"[A-Za-z0-9._-]{1,128}", qualification_id
    ):
        raise PhysicalGateError("evidence_root_unsafe")
    record_name = f"qualification-{qualification_id}.json"
    record = root / record_name
    raw = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    root_descriptor = -1
    record_descriptor = -1
    created = False
    try:
        root_descriptor = os.open(
            root,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        root_metadata = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or stat.S_IMODE(root_metadata.st_mode) & 0o022
        ):
            raise PhysicalGateError("evidence_root_unsafe")
        record_descriptor = os.open(
            record_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=root_descriptor,
        )
        created = True
        record_metadata = os.fstat(record_descriptor)
        if (
            not stat.S_ISREG(record_metadata.st_mode)
            or record_metadata.st_uid != os.geteuid()
            or record_metadata.st_nlink != 1
        ):
            raise PhysicalGateError("evidence_root_unsafe")
        written = 0
        while written < len(raw):
            count = os.write(record_descriptor, raw[written:])
            if count <= 0:
                raise OSError("short qualification write")
            written += count
        os.fchmod(record_descriptor, 0o400)
        os.fsync(record_descriptor)
        os.fsync(root_descriptor)
        current_root = root.lstat()
        if (
            stat.S_ISLNK(current_root.st_mode)
            or (current_root.st_dev, current_root.st_ino)
            != (root_metadata.st_dev, root_metadata.st_ino)
        ):
            raise PhysicalGateError("evidence_root_unsafe")
        final_descriptor = os.fstat(record_descriptor)
        final_name = os.stat(
            record_name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(final_name.st_mode)
            or (final_name.st_dev, final_name.st_ino)
            != (final_descriptor.st_dev, final_descriptor.st_ino)
            or final_name.st_nlink != 1
            or final_name.st_size != len(raw)
            or stat.S_IMODE(final_name.st_mode) != 0o400
        ):
            raise PhysicalGateError("evidence_root_unsafe")
        _verify_default_source_binding(data.get("source_digest"))
    except FileExistsError as exc:
        raise PhysicalGateError("record_exists") from exc
    except PhysicalGateError:
        if created and root_descriptor >= 0:
            try:
                os.unlink(record_name, dir_fd=root_descriptor)
            except OSError:
                pass
        raise
    except OSError as exc:
        if created and root_descriptor >= 0:
            try:
                os.unlink(record_name, dir_fd=root_descriptor)
            except OSError:
                pass
        raise PhysicalGateError("evidence_root_unsafe") from exc
    finally:
        if record_descriptor >= 0:
            os.close(record_descriptor)
        if root_descriptor >= 0:
            os.close(root_descriptor)
    return record


__all__ = [
    "A8_PHYSICAL_CASES",
    "PEER_REQUIRED_CASES",
    "PeerRequired",
    "PhysicalGateError",
    "build_adapter_from_bundle",
    "execute_case",
    "preflight_document",
    "probe_bootstrap_over_cleartext",
    "seal_qualification",
]
