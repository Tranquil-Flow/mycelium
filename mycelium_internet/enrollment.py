# SPDX-License-Identifier: AGPL-3.0-or-later
"""Internet-native enrollment and control adapter (spec §4-§5, §10.1).

Wraps a pinned seed transport behind the public HTTPS bootstrap boundary:

- the seed-key pin is verified against a signed identity envelope BEFORE the
  single-use invite secret is ever transmitted;
- rotation advances trust only through the dual-signed transition with a
  bounded overlap window;
- join retries are exact-idempotent and a changed request under the same
  nonce fails closed without transmission;
- resume, heartbeat, and member messages flow through the same bounded
  adapter, which refuses control for revoked members, expired leases, and
  stale generations;
- reconnect is bounded and always uses the same canonical HTTPS origin -
  no tailnet, no alternate origin, no silent fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
import hashlib
import re
import time
from typing import Any

from mycelium_invite import InviteError, verify_invite_bundle
from mycelium_membership import HEARTBEAT_PROTOCOL
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier
from mycelium_seed.coordinator import (
    SEED_IDENTITY_PROTOCOL,
    SEED_SIGNED_ENVELOPE_PROTOCOL,
)
from mycelium_seed.http import (
    SEED_JOIN_HTTP_PROTOCOL,
    SEED_MEMBER_HTTP_PROTOCOL,
    SEED_RESUME_HTTP_PROTOCOL,
    SeedHTTPError,
)
from mycelium_seed.operator import (
    SeedOperatorError,
    verify_seed_key_transition,
)

from .bootstrap import PublicBootstrapPolicy, canonical_https_origin
from .contracts import validate_bootstrap_status

_LOCAL_CODES = frozenset(
    {
        "pin_not_verified",
        "pin_mismatch",
        "tls_not_publicly_trusted",
        "invitation_authority_missing",
        "seed_origin_mismatch",
        "seed_swarm_mismatch",
        "seed_signature_invalid",
        "seed_time_invalid",
        "changed_retry_rejected",
        "revoked",
        "lease_expired",
        "generation_stale",
        "incarnation_changed",
        "reconnect_exhausted",
        "transport_failed",
        "bootstrap_unavailable",
    }
)
_REMOTE_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_BASE_STATEMENT_FIELDS = frozenset(
    {
        "protocol",
        "swarm_id",
        "seed_node_id",
        "seed_endpoint_id",
        "seed_url",
        "issued_at",
        "expires_at",
    }
)
_REVOKED_CODES = frozenset(
    {
        "seed_resume_member_revoked",
        "seed_member_lifecycle_ineligible",
    }
)
_EVIDENCE_FRESHNESS_SECONDS = 90.0


class EnrollmentError(RuntimeError):
    """A bounded enrollment or control failure.

    Local codes come from a closed vocabulary; remote seed codes pass through
    verbatim when they match the bounded code alphabet.
    """

    def __init__(self, code: str) -> None:
        if code not in _LOCAL_CODES and _REMOTE_CODE_RE.fullmatch(code) is None:
            raise ValueError("enrollment error code is invalid")
        self.code = code
        super().__init__(code)


class PublicBootstrapClient:
    """Pin-first enrollment and control over one canonical HTTPS origin."""

    def __init__(
        self,
        *,
        policy: PublicBootstrapPolicy,
        transport: Callable[
            [str, str, Mapping[str, Any] | None], Mapping[str, Any]
        ],
        tls_state: str,
        bundle: Mapping[str, Any] | None,
        invite_token: str | None,
        clock: Callable[[], float],
        backoff_seconds: float = 1.0,
        evidence_freshness_seconds: float = _EVIDENCE_FRESHNESS_SECONDS,
    ) -> None:
        if not isinstance(policy, PublicBootstrapPolicy):
            raise ValueError("policy is invalid")
        if not callable(transport):
            raise ValueError("transport is invalid")
        if tls_state not in {"publicly_trusted", "unverified"}:
            raise ValueError("tls_state is invalid")
        if not callable(clock):
            raise ValueError("clock is invalid")
        if (
            isinstance(backoff_seconds, bool)
            or not isinstance(backoff_seconds, (int, float))
            or not 0.0 <= float(backoff_seconds) <= 60.0
        ):
            raise ValueError("backoff_seconds is invalid")
        if (
            isinstance(evidence_freshness_seconds, bool)
            or not isinstance(evidence_freshness_seconds, (int, float))
            or not 0.0 < float(evidence_freshness_seconds) <= 86400.0
        ):
            raise ValueError("evidence_freshness_seconds is invalid")
        self._policy = policy
        self._transport = transport
        self._tls_state = tls_state
        self._clock = clock
        self._backoff_seconds = float(backoff_seconds)
        self._evidence_freshness_seconds = float(evidence_freshness_seconds)
        self._bundle: Mapping[str, Any] | None = bundle
        self._invite_token = invite_token
        self._bundle_payload: dict[str, Any] | None = None
        self._bundle_pin: str | None = None
        self._bundle_records: tuple[Mapping[str, Any], ...] = ()
        if bundle is not None:
            try:
                verified = verify_invite_bundle(bundle, now=float(clock()))
            except InviteError as exc:
                raise EnrollmentError(exc.code) from exc
            payload = dict(verified["payload"])
            self._bundle_payload = payload
            self._bundle_pin = verified["seed_key_digest"]
            self._bundle_records = tuple(
                dict(record) for record in verified["seed_key_records"]
            )
            seed_url = payload.get("seed_url")
            if not isinstance(seed_url, str):
                raise EnrollmentError("seed_origin_mismatch")
            if canonical_https_origin(seed_url) != policy.canonical_origin:
                raise EnrollmentError("seed_origin_mismatch")
        self._pin_verified_at: float | None = None
        self._identity: dict[str, Any] | None = None
        self._rotation_transition: dict[str, Any] | None = None
        self._rotation_records: dict[str, dict[str, Any]] = {}
        self._joined: dict[str, Any] | None = None
        self._generation: int | None = None
        self._incarnation: str | None = None
        self._lease_expires_at: float | None = None
        self._revoked = False
        self._counters = {
            "requests": 0,
            "joins_accepted": 0,
            "joins_rejected": 0,
        }
        self._last_success_at: float | None = None
        self._last_join_nonce: str | None = None
        self._last_join_digest: str | None = None
        self._join_acceptance: dict[str, Any] | None = None
        self._attempted_nonces: set[str] = set()
        self._join_transmissions = 0
        self.total_backoff_seconds = 0.0

    # -- construction helpers -------------------------------------------------

    @classmethod
    def from_seed_client(
        cls,
        client: Any,
        *,
        policy: PublicBootstrapPolicy,
        tls_state: str,
        bundle: Mapping[str, Any] | None,
        invite_token: str | None,
        clock: Callable[[], float],
        backoff_seconds: float = 1.0,
    ) -> "PublicBootstrapClient":
        def transport(
            method: str,
            path: str,
            body: Mapping[str, Any] | None,
        ) -> Mapping[str, Any]:
            if method == "GET" and path == "/seed/identity":
                return client.request("GET", "/seed/identity", None)
            if method == "GET" and path == "/seed/rotation":
                return client.request("GET", "/seed/rotation", None)
            if method == "POST" and path == "/seed/join":
                assert body is not None
                return client.request("POST", "/seed/join", body)
            if method == "POST" and path == "/seed/resume":
                assert body is not None
                return client.request("POST", "/seed/resume", body)
            if method == "POST" and path == "/seed/message":
                assert body is not None
                return client.request("POST", "/seed/message", body)
            raise SeedHTTPError("seed_http_route_unknown", status=404)

        return cls(
            policy=policy,
            transport=transport,
            tls_state=tls_state,
            bundle=bundle,
            invite_token=invite_token,
            clock=clock,
            backoff_seconds=backoff_seconds,
        )

    # -- state ---------------------------------------------------------------

    @property
    def canonical_origin(self) -> str:
        return self._policy.canonical_origin

    @property
    def policy(self) -> PublicBootstrapPolicy:
        return self._policy

    @property
    def pin_verified(self) -> bool:
        return self._pin_verified_at is not None

    @property
    def freshness(self) -> str:
        now = float(self._clock())
        if self._last_success_at is None:
            return "unknown"
        if now - self._last_success_at <= self._evidence_freshness_seconds:
            return "current"
        return "stale"

    # -- guards --------------------------------------------------------------

    def _require_tls(self) -> None:
        if self._tls_state != "publicly_trusted":
            raise EnrollmentError("tls_not_publicly_trusted")

    def _require_pin(self) -> None:
        if self._pin_verified_at is None:
            raise EnrollmentError("pin_not_verified")

    def _require_not_revoked(self) -> None:
        if self._revoked:
            raise EnrollmentError("revoked")

    def _require_lease(self, now: float) -> None:
        if self._lease_expires_at is not None and now >= self._lease_expires_at:
            raise EnrollmentError("lease_expired")

    def _translate(self, exc: SeedHTTPError) -> EnrollmentError:
        mapping = {
            "seed_http_unreachable": "bootstrap_unavailable",
            "seed_http_remote_error": "transport_failed",
            "seed_http_seed_pin_mismatch": "pin_mismatch",
            "seed_http_seed_signature_invalid": "seed_signature_invalid",
            "seed_http_seed_time_invalid": "seed_time_invalid",
            "seed_http_seed_key_invalid": "seed_signature_invalid",
        }
        code = exc.code
        if code in _REVOKED_CODES:
            self._revoked = True
            return EnrollmentError("revoked")
        if code in mapping:
            return EnrollmentError(mapping[code])
        return EnrollmentError(code)

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
    ) -> Mapping[str, Any]:
        self._counters["requests"] += 1
        content_type = "application/json" if method == "POST" else None
        body_length = 0 if body is None else len(canonical_json_bytes(dict(body)))
        self._policy.validate_request(
            method=method,
            target=path,
            content_type=content_type,
            body_length=body_length,
        )
        try:
            return self._transport(method, path, body)
        except SeedHTTPError as exc:
            raise self._translate(exc) from exc

    # -- seed identity and rotation ------------------------------------------

    def _seed_digest_accepted(
        self,
        digest: object,
        *,
        now: float,
    ) -> bool:
        if not isinstance(digest, str) or not digest:
            return False
        transition = self._rotation_transition
        if transition is None:
            return digest == self._bundle_pin
        if now < float(transition["effective_at"]):
            return digest == transition["old_seed_key_digest"]
        if now <= float(transition["overlap_expires_at"]):
            return digest in {
                transition["old_seed_key_digest"],
                transition["new_seed_key_digest"],
            }
        return digest == transition["new_seed_key_digest"]

    def accepted_seed_key_digest(self, digest: str, *, now: float) -> str:
        if not self._seed_digest_accepted(digest, now=now):
            raise EnrollmentError("pin_mismatch")
        return digest

    def preflight(self, *, now: float) -> dict[str, Any]:
        """Fetch and verify the signed seed identity under the pinned key."""

        self._require_tls()
        envelope = self._request("GET", "/seed/identity", None)
        statement = self._verify_seed_envelope(
            envelope,
            expected_protocol=SEED_IDENTITY_PROTOCOL,
            now=now,
        )
        self._identity = statement
        self._pin_verified_at = now
        self._last_success_at = now
        return statement

    def _verify_seed_envelope(
        self,
        envelope: Mapping[str, Any],
        *,
        expected_protocol: str,
        now: float,
    ) -> dict[str, Any]:
        if (
            not isinstance(envelope, Mapping)
            or set(envelope) != {"protocol", "statement", "signature", "verification_key"}
            or envelope.get("protocol") != SEED_SIGNED_ENVELOPE_PROTOCOL
        ):
            raise EnrollmentError("seed_signature_invalid")
        statement = envelope.get("statement")
        signature = envelope.get("signature")
        record = envelope.get("verification_key")
        if (
            not isinstance(statement, Mapping)
            or not isinstance(signature, Mapping)
            or not isinstance(record, Mapping)
        ):
            raise EnrollmentError("seed_signature_invalid")
        if (
            set(statement) != _BASE_STATEMENT_FIELDS
            or statement.get("protocol") != expected_protocol
        ):
            raise EnrollmentError("seed_signature_invalid")
        if statement.get("seed_url") != self.canonical_origin:
            raise EnrollmentError("seed_origin_mismatch")
        if self._bundle_payload is None:
            raise EnrollmentError("invitation_authority_missing")
        if statement.get("swarm_id") != self._bundle_payload.get("swarm_id"):
            raise EnrollmentError("seed_swarm_mismatch")
        digest = record.get("verification_key_digest")
        if not self._seed_digest_accepted(digest, now=now):
            raise EnrollmentError("pin_mismatch")
        try:
            verify = build_ed25519_verifier([dict(record)])
        except Exception as exc:
            raise EnrollmentError("seed_signature_invalid") from exc
        if not verify(canonical_json_bytes(dict(statement)), dict(signature)):
            raise EnrollmentError("seed_signature_invalid")
        issued = statement.get("issued_at")
        expires = statement.get("expires_at")
        if (
            isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not isinstance(issued, (int, float))
            or isinstance(issued, bool)
            or not isinstance(expires, (int, float))
            or isinstance(expires, bool)
            or now < float(issued)
            or now > float(expires)
        ):
            raise EnrollmentError("seed_time_invalid")
        return dict(statement)

    def rotation(
        self,
        *,
        now: float,
        envelope: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Fetch (or accept) a dual-signed rotation under the current pin."""

        self._require_tls()
        if envelope is None:
            try:
                fetched = self._request("GET", "/seed/rotation", None)
            except EnrollmentError as exc:
                if exc.code == "seed_rotation_absent":
                    return None
                raise
        else:
            fetched = envelope
        if fetched is None:
            return None
        try:
            transition = verify_seed_key_transition(dict(fetched), now=now)
        except SeedOperatorError as exc:
            raise EnrollmentError(exc.code) from exc
        if now > float(transition["overlap_expires_at"]):
            raise EnrollmentError("seed_rotation_expired")
        if self._bundle_pin not in {
            transition["old_seed_key_digest"],
            transition["new_seed_key_digest"],
        }:
            raise EnrollmentError("pin_mismatch")
        self._rotation_transition = transition
        self._rotation_records = {
            transition["old_seed_key_digest"]: dict(fetched["old_verification_key"]),
            transition["new_seed_key_digest"]: dict(fetched["new_verification_key"]),
        }
        return dict(fetched)

    # -- join ----------------------------------------------------------------

    def join(
        self,
        join_envelope: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        """Transmit the single-use secret only after pin and TLS authority."""

        self._require_tls()
        self._require_pin()
        if self._bundle_payload is None or self._invite_token is None:
            raise EnrollmentError("invitation_authority_missing")
        message = join_envelope.get("message")
        if not isinstance(message, Mapping):
            raise EnrollmentError("changed_retry_rejected")
        nonce = message.get("invite_nonce")
        if not isinstance(nonce, str) or not nonce:
            raise EnrollmentError("changed_retry_rejected")
        digest = hashlib.sha256(
            canonical_json_bytes(dict(join_envelope))
        ).hexdigest()
        if self._last_join_nonce is not None:
            if nonce == self._last_join_nonce and digest == self._last_join_digest:
                assert self._join_acceptance is not None
                return dict(self._join_acceptance)
            if nonce in self._attempted_nonces:
                raise EnrollmentError("changed_retry_rejected")
        body: dict[str, Any] = {
            "protocol": SEED_JOIN_HTTP_PROTOCOL,
            "invite_token": self._invite_token,
            "join_envelope": dict(join_envelope),
        }
        self._policy.validate_request(
            method="POST",
            target="/seed/join",
            content_type="application/json",
            body_length=len(canonical_json_bytes(body)),
            invite_token=self._invite_token,
        )
        try:
            acceptance = dict(self._transport("POST", "/seed/join", body))
        except SeedHTTPError as exc:
            self._counters["joins_rejected"] += 1
            raise self._translate(exc) from exc
        self._join_transmissions += 1
        self._counters["joins_accepted"] += 1
        self._attempted_nonces.add(nonce)
        self._last_join_nonce = nonce
        self._last_join_digest = digest
        self._join_acceptance = dict(acceptance)
        self._joined = dict(acceptance)
        acceptance_message = acceptance.get("message")
        if isinstance(acceptance_message, Mapping):
            self._generation = int(acceptance_message["membership_generation"])
            self._incarnation = str(acceptance_message["accepted_incarnation"])
            self._lease_expires_at = float(acceptance_message["lease_expires_at"])
        return dict(acceptance)

    # -- post-join control ---------------------------------------------------

    def resume(
        self,
        resume_envelope: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        self._require_tls()
        self._require_pin()
        self._require_not_revoked()
        self._require_lease(now)
        body: dict[str, Any] = {
            "protocol": SEED_RESUME_HTTP_PROTOCOL,
            "resume_envelope": dict(resume_envelope),
        }
        acceptance = self._request("POST", "/seed/resume", body)
        self._joined = dict(acceptance)
        acceptance_message = acceptance.get("message")
        if isinstance(acceptance_message, Mapping):
            self._generation = int(acceptance_message["membership_generation"])
            self._incarnation = str(acceptance_message["accepted_incarnation"])
            self._lease_expires_at = float(acceptance_message["lease_expires_at"])
        self._last_success_at = now
        return dict(acceptance)

    def heartbeat(
        self,
        envelope: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        self._require_tls()
        self._require_pin()
        self._require_not_revoked()
        self._require_lease(now)
        message = envelope.get("message")
        if not isinstance(message, Mapping) or message.get(
            "protocol"
        ) != HEARTBEAT_PROTOCOL:
            raise EnrollmentError("changed_retry_rejected")
        body: dict[str, Any] = {
            "protocol": SEED_MEMBER_HTTP_PROTOCOL,
            "envelope": dict(envelope),
        }
        response = self._request("POST", "/seed/message", body)
        renewal = response.get("message")
        if isinstance(renewal, Mapping) and renewal.get(
            "lease_expires_at"
        ) is not None:
            self._lease_expires_at = max(
                self._lease_expires_at or 0.0,
                float(renewal["lease_expires_at"]),
            )
        self._last_success_at = now
        return dict(response)

    def send_control(
        self,
        envelope: Mapping[str, Any],
        *,
        now: float,
    ) -> dict[str, Any]:
        self._require_tls()
        self._require_pin()
        self._require_not_revoked()
        self._require_lease(now)
        body: dict[str, Any] = {
            "protocol": SEED_MEMBER_HTTP_PROTOCOL,
            "envelope": dict(envelope),
        }
        response = self._request("POST", "/seed/message", body)
        self._last_success_at = now
        return dict(response)

    # -- reconnect -----------------------------------------------------------

    def reconnect(
        self,
        *,
        max_attempts: int = 3,
        backoff_seconds: float | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
            or max_attempts < 1
        ):
            raise ValueError("max_attempts is invalid")
        delay = (
            self._backoff_seconds
            if backoff_seconds is None
            else float(backoff_seconds)
        )
        for attempt in range(1, max_attempts + 1):
            now = float(self._clock())
            try:
                return self.preflight(now=now)
            except EnrollmentError as exc:
                if exc.code not in {"bootstrap_unavailable", "transport_failed"}:
                    raise
            if attempt < max_attempts:
                self.total_backoff_seconds += delay
                time.sleep(delay)
        raise EnrollmentError("reconnect_exhausted")

    # -- bootstrap status projection -----------------------------------------

    def bootstrap_status(self, *, now: float) -> dict[str, Any]:
        freshness = self.freshness
        if self._joined is not None:
            invitation_state = "accepted"
        elif self._counters["joins_rejected"] > 0:
            invitation_state = "rejected"
        elif self._bundle_payload is not None:
            invitation_state = "pending"
        else:
            invitation_state = "unknown"
        if self._last_success_at is None:
            route_state = "unknown"
        elif freshness == "current":
            route_state = "available"
        else:
            route_state = "unavailable"
        document: dict[str, Any] = {
            "protocol": "mycelium.internet_bootstrap_status.v1",
            "generation": 1,
            "observed_at_unix_ms": int(float(now) * 1_000),
            "freshness": freshness,
            "tls_state": self._tls_state,
            "canonical_origin_verified": self._pin_verified_at is not None,
            "seed_pin_state": (
                "verified" if self._pin_verified_at is not None else "unknown"
            ),
            "route_state": route_state,
            "invitation_state": invitation_state,
            "counters": dict(self._counters),
        }
        validate_bootstrap_status(document)
        return document


__all__ = [
    "EnrollmentError",
    "PublicBootstrapClient",
]
