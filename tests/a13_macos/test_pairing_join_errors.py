# SPDX-License-Identifier: AGPL-3.0-only
"""C2-backend / C3: distinct join-reachability codes and the advisory clock-skew warning."""
from __future__ import annotations

import email.utils
import socket
import ssl
from urllib.error import HTTPError, URLError

import pytest

from mycelium_a13_macos.pairing import JoinWindowClient, PairingError

NOW = 1_800_000_000.0
ORIGIN = "https://quiet-river-apple-stone.trycloudflare.com"
PARENT = "pairing_window_unreachable"


class _Response:
    def __init__(self, body: bytes = b"{}", date: str | None = None) -> None:
        self._body = body
        self.headers = {} if date is None else {"Date": date}

    def read(self, _limit: int = -1) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _Opener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome

    def open(self, _request, timeout=None):
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _client(outcome) -> JoinWindowClient:
    client = JoinWindowClient(ORIGIN, _clock=lambda: NOW)
    client._opener = _Opener(outcome)
    return client


def _http_date(offset_seconds: float) -> str:
    return email.utils.formatdate(NOW + offset_seconds, usegmt=True)


@pytest.mark.parametrize("error,code", [
    (URLError(socket.gaierror(8, "nodename nor servname provided")), "pairing_window_dns_failed"),
    (socket.gaierror(8, "no name"), "pairing_window_dns_failed"),
    (OSError("relay_dns_unavailable"), "pairing_window_dns_failed"),
    (OSError("relay_dns_invalid"), "pairing_window_dns_failed"),
    (OSError("relay_dns_nonpublic"), "pairing_window_dns_failed"),
    (URLError(ssl.SSLCertVerificationError(1, "certificate verify failed")), "pairing_window_tls_failed"),
    (ssl.SSLError(1, "handshake failure"), "pairing_window_tls_failed"),
    (OSError("relay_tls_verification_required"), "pairing_window_tls_failed"),
    (URLError(TimeoutError("timed out")), "pairing_window_timeout"),
    (TimeoutError("timed out"), "pairing_window_timeout"),
    (socket.timeout("timed out"), "pairing_window_timeout"),
    (TimeoutError("relay_connect_timeout"), "pairing_window_timeout"),
    # anything else stays the old catch-all
    (URLError(ConnectionRefusedError(61, "refused")), PARENT),
    (OSError("relay_connect_unavailable"), PARENT),
    (ValueError("bad"), PARENT),
])
def test_unreachable_is_split_by_cause_and_keeps_the_parent(error, code) -> None:
    with pytest.raises(PairingError) as caught:
        _client(error).post("/x", b"{}")
    assert caught.value.code == code
    assert caught.value.parent == (PARENT if code != PARENT else None)


def test_codes_are_closed_vocabulary_and_catalogued() -> None:
    from mycelium_a13_macos import pairing

    assert pairing.UNREACHABLE_CODES == (
        "pairing_window_dns_failed", "pairing_window_tls_failed", "pairing_window_timeout")
    assert all(pairing._ERROR_CODE_RE.fullmatch(code) for code in pairing.UNREACHABLE_CODES)


def test_remote_http_error_codes_are_unchanged() -> None:
    import io
    error = HTTPError(ORIGIN, 409, "conflict", {}, io.BytesIO(b'{"error":"pairing_window_claimed"}'))
    with pytest.raises(PairingError) as caught:
        _client(error).post("/x", b"{}")
    assert caught.value.code == "pairing_window_claimed" and caught.value.parent is None


# -- C3: advisory clock skew -------------------------------------------------

def test_large_skew_is_reported_never_raised() -> None:
    client = _client(_Response(date=_http_date(120)))
    assert client.post("/x", b"{}") == b"{}"           # the request still succeeds
    assert client.warnings == [{"code": "clock_skew", "skew_seconds": 120}]


def test_skew_sign_is_remote_minus_local() -> None:
    client = _client(_Response(date=_http_date(-45)))
    client.post("/x", b"{}")
    assert client.warnings == [{"code": "clock_skew", "skew_seconds": -45}]


@pytest.mark.parametrize("offset", [0, 5, -5, 20, -20])
def test_skew_within_twenty_seconds_is_silent(offset) -> None:
    client = _client(_Response(date=_http_date(offset)))
    client.post("/x", b"{}")
    assert client.warnings == []


@pytest.mark.parametrize("date", [None, "", "not a date", "Fri, 99 Foo 2999 99:99:99 GMT"])
def test_missing_or_invalid_date_is_ignored(date) -> None:
    client = _client(_Response(date=date))
    client.post("/x", b"{}")
    assert client.warnings == []


def test_skew_is_reported_once_per_client_and_cleared_when_clock_recovers() -> None:
    client = _client(_Response(date=_http_date(120)))
    client.post("/x", b"{}")
    client.post("/x", b"{}")
    assert len(client.warnings) == 1
    client._opener = _Opener(_Response(date=_http_date(1)))
    client.post("/x", b"{}")
    assert client.warnings == []


def test_skew_is_read_from_an_error_response_too() -> None:
    import io
    headers = {"Date": _http_date(300)}
    error = HTTPError(ORIGIN, 409, "conflict", headers, io.BytesIO(b'{"error":"pairing_window_claimed"}'))
    client = _client(error)
    with pytest.raises(PairingError):
        client.post("/x", b"{}")
    assert client.warnings == [{"code": "clock_skew", "skew_seconds": 300}]
