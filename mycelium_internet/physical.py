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
import time as _time_module
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping

from .activation import ActivationObservations
from .bootstrap import (
    BoundaryError,
    PublicBootstrapPolicy,
    canonical_https_origin,
)
from .contracts import (
    INTERNET_NATIVE_QUALIFICATION_PROTOCOL,
    validate_internet_native_qualification,
)
from .enrollment import EnrollmentError, PublicBootstrapClient
from .privacy import ensure_privacy_clean
from mycelium_invite import verify_invite_bundle
from mycelium_seed.coordinator import SEED_SIGNED_ENVELOPE_PROTOCOL
from mycelium_seed.http import SeedHTTPClient

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
# Peer-required cases that ALSO need the A8 product surface mounted in the
# shared spine (browser inference, serving refusal). Blocked on the A4 lane
# owning that spine; they stay fail-closed until it lands.
_UI_DEPENDENT_CASES = frozenset(
    {
        "direct_path_qualified_browser_inference",
        "forced_relay_privacy_reduced_browser_inference",
        "observed_path_transition_and_reconnect",
        "unqualified_external_member",
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


def build_adapter_from_bundle(
    *,
    origin: str,
    bundle: Mapping[str, Any],
    invite_token: str,
    clock: Callable[[], float] = _time_module.time,
) -> PublicBootstrapClient:
    """Construct the pin-first adapter for one canonical public origin from
    an owner-delivered invite bundle (the same wiring the rehearsal peer
    uses, shared for the physical-era CLI)."""

    canonical_origin = canonical_https_origin(origin)
    now = clock()
    verified = verify_invite_bundle(dict(bundle), now=now)
    client = SeedHTTPClient(
        seed_url=canonical_origin,
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
    return _executed_envelope(
        case_id="cleartext_or_redirect_bootstrap",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
    )


def _run_replay_case(
    adapter: PublicBootstrapClient,
    *,
    first_join_envelope: Mapping[str, Any],
    second_join_envelope: Mapping[str, Any],
    second_adapter: PublicBootstrapClient,
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
    return _executed_envelope(
        case_id="invalid_or_replayed_invitation",
        result=result,
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


def _run_unrelated_invite_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    peer_network: Any,
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
    if _activation_is_unknown_not_zero():
        outcomes.append("activation_unknown_not_zero")
    outcomes.append(_peer_network_outcome(_resolve(peer_network)))
    required = {
        "seed_pin_verified",
        "invite_redeemed",
        "membership_renewed",
        "activation_unknown_not_zero",
        "no_tailnet_path_observed",
    }
    result = "passed" if required <= set(outcomes) else "failed"
    return _executed_envelope(
        case_id="unrelated_https_invite_without_tailscale",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
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


def _run_revoked_member_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    revoke: Callable[[], None] | None,
    await_revocation_seconds: float | None,
    poll_interval_seconds: float,
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
    if _activation_is_unknown_not_zero():
        outcomes.append("no_serving_after_revocation")
    required = {
        "member_enrolled_before_revocation",
        "control_refused_after_revocation",
        "no_serving_after_revocation",
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
        evidence_digests=[],
    )


def _run_endpoint_mismatch_case(
    adapter: PublicBootstrapClient,
    *,
    node: Any,
    join_envelope: Mapping[str, Any],
    mismatched_node: Any,
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
    required = {"mismatched_identity_refused", "member_record_unchanged"}
    result = "passed" if required <= set(outcomes) else "failed"
    return _executed_envelope(
        case_id="endpoint_identity_mismatch",
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
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
    return _executed_envelope(
        case_id=case_id,
        result=result,
        outcomes=outcomes,
        spec_digest=spec_digest,
        source_digest=source_digest,
        evidence_digests=[],
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
    if case_id == "unrelated_https_invite_without_tailscale":
        if "peer_network" not in inputs:
            raise PeerRequired()
        return _run_unrelated_invite_case(
            adapter,
            node=node,
            join_envelope=join_envelope,
            peer_network=inputs.get("peer_network"),
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
    adapter: PublicBootstrapClient | None = None,
    case_inputs: Mapping[str, Any] | None = None,
    spec_digest: str = "sha256:" + "0" * 64,
    source_digest: str = "sha256:" + "0" * 64,
) -> dict[str, Any]:
    """Execute one physical case if, and only if, its inputs exist.

    ``case_inputs`` carries per-case procedure inputs:
    ``probe`` for the cleartext case (network observation, injectable for
    deterministic tests), and ``first_join_envelope``/
    ``second_join_envelope``/``second_adapter`` for the replay case.
    """

    if case_id not in A8_PHYSICAL_CASES:
        raise PhysicalGateError("case_unknown")
    try:
        canonical_https_origin(origin)
    except ValueError as exc:
        raise PhysicalGateError("origin_invalid") from exc
    if case_id in PEER_REQUIRED_CASES:
        if case_id in _UI_DEPENDENT_CASES or adapter is None:
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
        if (
            not isinstance(first, Mapping)
            or not isinstance(second, Mapping)
            or not isinstance(second_adapter, PublicBootstrapClient)
        ):
            raise PhysicalGateError("physical_infrastructure_unavailable")
        return _run_replay_case(
            adapter,
            first_join_envelope=first,
            second_join_envelope=second,
            second_adapter=second_adapter,
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
    "build_adapter_from_bundle",
    "execute_case",
    "preflight_document",
    "probe_bootstrap_over_cleartext",
    "seal_qualification",
]
