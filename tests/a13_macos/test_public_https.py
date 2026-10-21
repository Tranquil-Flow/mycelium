# SPDX-License-Identifier: AGPL-3.0-only
"""Resolver fallback never changes HTTP identity or TLS verification."""
import json
import socket
import ssl
import time
from io import BytesIO
from unittest.mock import Mock
import pytest
from mycelium_a13_macos.public_https import (
    relay_addresses, RelayHTTPSConnection, invalidate_relay_address, _clear_address_cache,
    _RELAY_ADDRESS_TTL_SECONDS,
)

HOST='example-test.trycloudflare.com'


@pytest.fixture(autouse=True)
def _isolated_address_cache():
    _clear_address_cache()
    yield
    _clear_address_cache()

def test_system_dns_first(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**kw:[(2,1,6,'',('104.16.230.132',443))])
    fetch=Mock(side_effect=AssertionError('fallback not needed'))
    assert relay_addresses(HOST,timeout=2,fetch=fetch)==['104.16.230.132']
    fetch.assert_not_called()

@pytest.mark.parametrize('address',['127.0.0.1','10.0.0.1','100.64.0.1','169.254.169.254','::1'])
def test_private_fallback_answer_rejected(monkeypatch,address):
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    fetch=Mock(return_value={'Status':0,'Answer':[{'type':1,'data':address}]})
    with pytest.raises(OSError):relay_addresses(HOST,timeout=2,fetch=fetch)

def test_fallback_only_for_product_relay(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    fetch=Mock()
    with pytest.raises(OSError):relay_addresses('example.org',timeout=2,fetch=fetch)
    fetch.assert_not_called()

def test_original_hostname_is_tls_server_name(monkeypatch):
    monkeypatch.setattr('mycelium_a13_macos.public_https.relay_addresses',lambda *a,**k:['104.16.230.132'])
    raw=Mock();connect=Mock(return_value=raw);monkeypatch.setattr(socket,'create_connection',connect)
    context=ssl.create_default_context();wrapped=Mock();wrap=Mock(return_value=wrapped)
    monkeypatch.setattr(context,'wrap_socket',wrap)
    connection=RelayHTTPSConnection(HOST,context=context,timeout=2);connection.connect()
    assert connect.call_args.args[0]==('104.16.230.132',443)
    wrap.assert_called_once_with(raw,server_hostname=HOST)
    assert connection.host==HOST
    assert context.check_hostname and context.verify_mode==ssl.CERT_REQUIRED

@pytest.mark.parametrize('document',[{'Status':3},{'Status':0,'Answer':[]},{'Status':0,'Answer':[{'type':1,'data':'bad'}]}])
def test_bad_dns_never_dials(monkeypatch,document):
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    with pytest.raises(OSError):relay_addresses(HOST,timeout=2,fetch=lambda *a,**k:document)


def test_transient_nxdomain_is_retried_within_a_bounded_window(monkeypatch):
    """A quick-tunnel name can be briefly absent from Cloudflare's own answers.

    Measured 2026-09-15 on real hardware: system DNS returns `gaierror` for every
    `*.trycloudflare.com` name, so this fallback is on the critical path of every
    request; the authoritative DoH answer for a live, healthy origin came back
    NXDOMAIN for one request and `Status: 0` minutes later.  A single negative
    answer used to abort an idempotent staging read with
    `managed_control_unreachable`, which cost a whole two-Mac attempt.
    """
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    attempts=[]
    def fetch(host,*,timeout):
        attempts.append(host)
        if len(attempts)<3:return {'Status':3}
        return {'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    assert relay_addresses(HOST,timeout=6,fetch=fetch)==['104.16.230.132']
    assert len(attempts)==3


def test_persistent_nxdomain_still_fails_closed_within_the_budget(monkeypatch):
    """Retries are bounded by the caller's budget; the terminal code is unchanged."""
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    attempts=[]
    def fetch(host,*,timeout):
        attempts.append(host);return {'Status':3}
    started=time.monotonic()
    with pytest.raises(OSError,match='relay_dns_unavailable'):
        relay_addresses(HOST,timeout=2,fetch=fetch)
    assert len(attempts)>=2
    assert time.monotonic()-started<3.0


def test_transient_fetch_failure_is_retried_then_still_fails_closed(monkeypatch):
    """A transport failure of the DoH call itself is retried, never masked."""
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    calls={'n':0}
    def fetch(host,*,timeout):
        calls['n']+=1
        if calls['n']==1:raise OSError('doh transport down')
        return {'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    assert relay_addresses(HOST,timeout=6,fetch=fetch)==['104.16.230.132']
    assert calls['n']==2

    # A cached success must not mask a genuinely failing lookup: clear it first.
    _clear_address_cache()
    def always_fail(host,*,timeout):raise OSError('doh transport down')
    with pytest.raises(OSError,match='relay_dns_unavailable'):
        relay_addresses(HOST,timeout=2,fetch=always_fail)


def test_resolved_address_is_reused_without_another_lookup(monkeypatch):
    """One flaky answer must not fail every request for the tunnel's whole life.

    Measured 2026-09-15: this laptop cannot resolve `*.trycloudflare.com` through
    system DNS at all, so every child request depended on a fresh DoH lookup; a
    single NXDOMAIN then failed an in-flight inference request
    (`managed_control_unreachable`, route state `request_cleanup_unproven`).  A
    resolved origin is now reused, so the lookup happens once per TTL rather than
    once per request.
    """
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    calls=[]
    def fetch(host,*,timeout):
        calls.append(host)
        return {'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    for _ in range(3):
        assert relay_addresses(HOST,timeout=4,fetch=fetch)==['104.16.230.132']
    assert len(calls)==1


def test_cached_address_is_dropped_after_its_ttl(monkeypatch):
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    clock={'at':1000.0}
    monkeypatch.setattr('mycelium_a13_macos.public_https.time.monotonic',lambda:clock['at'])
    calls=[]
    def fetch(host,*,timeout):
        calls.append(host);return {'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    relay_addresses(HOST,timeout=4,fetch=fetch)
    assert len(calls)==1
    clock['at']+=_RELAY_ADDRESS_TTL_SECONDS+1.0
    relay_addresses(HOST,timeout=4,fetch=fetch)
    assert len(calls)==2


def test_invalidated_address_is_resolved_again(monkeypatch):
    """A failed dial must not pin a stale edge address."""
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    calls=[]
    def fetch(host,*,timeout):
        calls.append(host);return {'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    relay_addresses(HOST,timeout=4,fetch=fetch)
    relay_addresses(HOST,timeout=4,fetch=fetch)
    assert len(calls)==1
    invalidate_relay_address(HOST)
    relay_addresses(HOST,timeout=4,fetch=fetch)
    assert len(calls)==2


def test_address_cache_is_bounded(monkeypatch):
    from mycelium_a13_macos import public_https as module
    monkeypatch.setattr(socket,'getaddrinfo',Mock(side_effect=socket.gaierror()))
    fetch=lambda host,*,timeout:{'Status':0,'Answer':[{'type':1,'data':'104.16.230.132'}]}
    for index in range(module._RELAY_ADDRESS_CACHE_LIMIT+4):
        relay_addresses(f'host-{index}.trycloudflare.com',timeout=4,fetch=fetch)
    assert len(module._address_cache)<=module._RELAY_ADDRESS_CACHE_LIMIT
