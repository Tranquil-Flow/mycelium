# SPDX-License-Identifier: AGPL-3.0-or-later
"""Deterministic gates for the public HTTPS bootstrap boundary (spec §3)."""

from __future__ import annotations

import pytest

from mycelium_internet.bootstrap import (
    PUBLIC_ROUTE_ALLOWLIST,
    BoundaryError,
    InviteAttemptTracker,
    PublicBootstrapPolicy,
    RateLimiter,
    canonical_https_origin,
    downgrade_verdict,
    redirect_verdict,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _policy(**overrides: object) -> PublicBootstrapPolicy:
    return PublicBootstrapPolicy(
        **{
            "canonical_origin": "https://seed.example.com",
            **overrides,
        }
    )


# ---------------------------------------------------------------------------
# Canonical origin
# ---------------------------------------------------------------------------

def test_canonical_https_origin_accepted_with_and_without_port() -> None:
    assert canonical_https_origin("https://seed.example.com") == "https://seed.example.com"
    assert (
        canonical_https_origin("https://seed.example.com:8443")
        == "https://seed.example.com:8443"
    )


def test_canonical_origin_rejects_userinfo_path_query_and_fragment() -> None:
    for value in (
        "https://user@seed.example.com",
        "https://seed.example.com/path",
        "https://seed.example.com?x=1",
        "https://seed.example.com#frag",
        "https://seed.example.com/",
        "https://seed.example.com:443/",
    ):
        with pytest.raises(ValueError):
            canonical_https_origin(value)


def test_canonical_origin_requires_exact_form() -> None:
    for value in (
        "https://SEED.example.com",
        "https://seed.example.com:0",
        "https://seed.example.com:",
        "https://",
        "http://seed.example.com",
        "https://seed.example.com:8443 ",
        " https://seed.example.com",
        "https://seed.exa_mple.com",
        "https://seed.-bad.com",
    ):
        with pytest.raises(ValueError):
            canonical_https_origin(value)


# ---------------------------------------------------------------------------
# Downgrade and redirect refusal
# ---------------------------------------------------------------------------

def test_cleartext_http_origin_is_a_downgrade_refusal() -> None:
    assert downgrade_verdict("http://seed.example.com") == "downgrade_refused"
    assert downgrade_verdict("https://seed.example.com") is None
    assert downgrade_verdict("ftp://seed.example.com") == "downgrade_refused"


def test_every_redirect_is_refused() -> None:
    assert (
        redirect_verdict("https://seed.example.com", "https://other.example.com")
        == "redirect_refused"
    )
    # Downgrade-shaped redirects get the sharper verdict.
    assert (
        redirect_verdict("https://seed.example.com", "http://seed.example.com")
        == "downgrade_refused"
    )


# ---------------------------------------------------------------------------
# Method/path allowlist
# ---------------------------------------------------------------------------

def test_allowlist_is_exactly_the_five_closed_routes() -> None:
    assert PUBLIC_ROUTE_ALLOWLIST == {
        "GET": frozenset({"/seed/identity", "/seed/rotation"}),
        "POST": frozenset({"/seed/join", "/seed/resume", "/seed/message"}),
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/seed/identity"),
        ("GET", "/seed/rotation"),
        ("POST", "/seed/join"),
        ("POST", "/seed/resume"),
        ("POST", "/seed/message"),
    ],
)
def test_allowlisted_routes_pass(method: str, path: str) -> None:
    _policy().validate_request(
        method=method,
        target=path,
        content_type="application/json" if method == "POST" else None,
        body_length=2 if method == "POST" else 0,
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/seed/join"),          # wrong method on a known route
        ("POST", "/seed/identity"),     # wrong method on a known route
        ("GET", "/"),                   # root
        ("GET", "/seed/admin"),         # operator administration
        ("POST", "/seed/invite"),       # invite minting
        ("GET", "/seed/members"),       # private inventory
        ("DELETE", "/seed/identity"),   # unsupported method
        ("PUT", "/seed/join"),
        ("HEAD", "/seed/identity"),
        ("OPTIONS", "/seed/join"),
        ("POST", "/seed/join?x=1"),     # query string
        ("GET", "/seed/identity#frag"),  # fragment
        ("POST", "/seed/message/extra"),
        ("GET", "/seed/identity/"),
    ],
)
def test_non_allowlisted_routes_rejected(method: str, path: str) -> None:
    with pytest.raises(BoundaryError) as exc_info:
        _policy().validate_request(
            method=method,
            target=path,
            content_type="application/json" if method == "POST" else None,
            body_length=2 if method == "POST" else 0,
        )
    assert exc_info.value.code in {
        "route_not_allowed",
        "target_invalid",
        "method_not_allowed",
    }


def test_rejection_discloses_only_bounded_codes() -> None:
    for code in (
        "route_not_allowed",
        "method_not_allowed",
        "target_invalid",
        "downgrade_refused",
        "redirect_refused",
        "content_type_invalid",
        "transfer_encoding_unsupported",
        "upgrade_rejected",
        "cookie_rejected",
        "authorization_rejected",
        "frame_too_large",
        "concurrency_exhausted",
        "rate_exhausted",
        "invite_attempts_exhausted",
        "body_required",
        "body_forbidden",
    ):
        assert BoundaryError(code).code == code


def test_policy_rejects_unknown_error_codes() -> None:
    with pytest.raises(ValueError):
        BoundaryError("not_a_boundary_code")
    with pytest.raises(ValueError):
        BoundaryError("../../etc/passwd")


# ---------------------------------------------------------------------------
# Content type, encodings, and secret-bearing headers
# ---------------------------------------------------------------------------

def test_post_requires_exact_json_content_type() -> None:
    policy = _policy()
    for content_type in (
        None,
        "text/plain",
        "application/x-www-form-urlencoded",
        "application/json; charset=utf-8",
        "multipart/form-data",
    ):
        with pytest.raises(BoundaryError) as exc_info:
            policy.validate_request(
                method="POST",
                target="/seed/join",
                content_type=content_type,
                body_length=2,
            )
        assert exc_info.value.code in {"content_type_invalid", "body_required"}


def test_get_with_body_rejected() -> None:
    with pytest.raises(BoundaryError) as exc_info:
        _policy().validate_request(
            method="GET",
            target="/seed/identity",
            content_type="application/json",
            body_length=3,
        )
    assert exc_info.value.code == "body_forbidden"


def test_transfer_encoding_websocket_cookie_and_authorization_rejected() -> None:
    policy = _policy()
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/join",
            content_type="application/json",
            body_length=2,
            headers={"Transfer-Encoding": "chunked"},
        )
    assert exc_info.value.code == "transfer_encoding_unsupported"
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/message",
            content_type="application/json",
            body_length=2,
            headers={"Upgrade": "websocket"},
        )
    assert exc_info.value.code == "upgrade_rejected"
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/join",
            content_type="application/json",
            body_length=2,
            headers={"Cookie": "session=abc"},
        )
    assert exc_info.value.code == "cookie_rejected"
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/message",
            content_type="application/json",
            body_length=2,
            headers={"Authorization": "Bearer xyz"},
        )
    assert exc_info.value.code == "authorization_rejected"


def test_forwarded_cleartext_scheme_is_downgrade_refused() -> None:
    policy = _policy()
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="GET",
            target="/seed/identity",
            content_type=None,
            body_length=0,
            headers={"X-Forwarded-Proto": "http"},
        )
    assert exc_info.value.code == "downgrade_refused"


def test_forwarded_https_scheme_is_allowed() -> None:
    _policy().validate_request(
        method="GET",
        target="/seed/identity",
        content_type=None,
        body_length=0,
        headers={"X-Forwarded-Proto": "https"},
    )


def test_cf_visitor_cleartext_scheme_is_downgrade_refused() -> None:
    policy = _policy()
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="GET",
            target="/seed/identity",
            content_type=None,
            body_length=0,
            headers={"Cf-Visitor": '{"scheme":"http"}'},
        )
    assert exc_info.value.code == "downgrade_refused"


def test_cf_visitor_https_scheme_is_allowed() -> None:
    _policy().validate_request(
        method="GET",
        target="/seed/identity",
        content_type=None,
        body_length=0,
        headers={"Cf-Visitor": '{"scheme":"https"}'},
    )


def test_absent_scheme_marker_is_allowed() -> None:
    # The loopback listener is trusted; when no forwarder marks the scheme
    # (nginx 444-on-80 topology, or direct local access) the request is
    # served. Only an explicit cleartext marker is refused.
    _policy().validate_request(
        method="GET",
        target="/seed/identity",
        content_type=None,
        body_length=0,
        headers=None,
    )


# ---------------------------------------------------------------------------
# Frame, concurrency, rate, and per-invite attempt bounds
# ---------------------------------------------------------------------------

def test_frame_bound_rejects_oversized_bodies() -> None:
    policy = _policy(max_frame_bytes=1024 * 1024)
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/join",
            content_type="application/json",
            body_length=1024 * 1024 + 1,
        )
    assert exc_info.value.code == "frame_too_large"
    policy.validate_request(
        method="POST",
        target="/seed/join",
        content_type="application/json",
        body_length=1024 * 1024,
    )


def test_concurrency_bound_is_exhaustible_and_releasable() -> None:
    policy = _policy(max_concurrent_requests=2)
    policy.acquire()
    policy.acquire()
    with pytest.raises(BoundaryError) as exc_info:
        policy.acquire()
    assert exc_info.value.code == "concurrency_exhausted"
    policy.release()
    policy.acquire()


def test_concurrency_release_never_goes_negative() -> None:
    policy = _policy(max_concurrent_requests=2)
    with pytest.raises(ValueError):
        policy.release()


def test_rate_limit_allows_burst_then_exhausts_until_refill() -> None:
    clock = FakeClock()
    policy = _policy(
        max_requests_per_second=2,
        rate_bucket_capacity=2,
        clock=clock,
    )
    policy.check_rate()
    policy.check_rate()
    with pytest.raises(BoundaryError) as exc_info:
        policy.check_rate()
    assert exc_info.value.code == "rate_exhausted"
    clock.advance(0.5)
    policy.check_rate()
    with pytest.raises(BoundaryError):
        policy.check_rate()
    clock.advance(10.0)
    policy.check_rate()
    policy.check_rate()


def test_rate_limiter_capacity_is_bounded_by_bucket() -> None:
    clock = FakeClock()
    limiter = RateLimiter(rate_per_second=100.0, capacity=3, clock=clock)
    clock.advance(100.0)
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is True
    assert limiter.allow() is False


def test_invite_attempt_bound_is_per_invite_and_privacy_safe() -> None:
    clock = FakeClock()
    tracker = InviteAttemptTracker(max_attempts=3, clock=clock)
    token = "invite-secret-token-1"
    for _ in range(3):
        assert tracker.allow(token) is True
    assert tracker.allow(token) is False
    # A different invite is tracked independently.
    assert tracker.allow("invite-secret-token-2") is True
    # The tracker never retains the raw secret.
    state = tracker._snapshot()  # noqa: SLF001 - test-only introspection
    assert token not in repr(state)
    assert "invite-secret-token-1" not in str(state)


def test_join_attempt_limit_is_checked_only_on_the_join_route() -> None:
    clock = FakeClock()
    policy = _policy(max_join_attempts_per_invite=1, clock=clock)
    policy.validate_request(
        method="POST",
        target="/seed/join",
        content_type="application/json",
        body_length=2,
        invite_token="token-a",
    )
    with pytest.raises(BoundaryError) as exc_info:
        policy.validate_request(
            method="POST",
            target="/seed/join",
            content_type="application/json",
            body_length=2,
            invite_token="token-a",
        )
    assert exc_info.value.code == "invite_attempts_exhausted"
    # Other routes are not invite-attempt bounded.
    policy.validate_request(
        method="POST",
        target="/seed/message",
        content_type="application/json",
        body_length=2,
    )


# ---------------------------------------------------------------------------
# Timeout and no-store
# ---------------------------------------------------------------------------

def test_read_timeout_is_bounded() -> None:
    policy = _policy(read_timeout_seconds=2.0)
    assert policy.read_timeout_seconds == 2.0
    with pytest.raises(ValueError):
        _policy(read_timeout_seconds=0.0)
    with pytest.raises(ValueError):
        _policy(read_timeout_seconds=31.0)


def test_response_headers_are_json_and_no_store() -> None:
    headers = _policy().response_headers()
    assert headers == {
        "Content-Type": "application/json",
        "Cache-Control": "no-store",
    }


def test_policy_rejects_invalid_construction_values() -> None:
    with pytest.raises(ValueError):
        _policy(canonical_origin="http://seed.example.com")
    with pytest.raises(ValueError):
        _policy(max_frame_bytes=0)
    with pytest.raises(ValueError):
        _policy(max_concurrent_requests=0)
    with pytest.raises(ValueError):
        _policy(max_requests_per_second=0)
    with pytest.raises(ValueError):
        _policy(max_join_attempts_per_invite=0)
