# SPDX-License-Identifier: AGPL-3.0-only
"""Product-relay HTTPS with bounded public DNS fallback; no OS DNS edits.

Only trycloudflare.com tunnel names may use Cloudflare's HTTPS DNS service
when system resolution fails. The original hostname remains the HTTP Host,
TLS SNI and certificate identity. Both connections use normal Web-PKI; no
custom trust root, redirect, proxy, secret, or address supplied by a caller.
Relay/network consent includes this resolver disclosure. Other hosts retain
normal system resolution. Private DNS answers fail closed.
"""
from __future__ import annotations
from mycelium_http_identity import with_user_agent
import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import time
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener

_RELAY=re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\.trycloudflare\.com\Z')

# A quick-tunnel hostname is published by Cloudflare and can be briefly absent
# from its authoritative answers even while the tunnel is healthy.  Measured
# 2026-09-15 on the real laptop: system resolution returns `gaierror` for EVERY
# `*.trycloudflare.com` name, so this fallback is on the critical path of every
# request; the DoH answer for a live origin came back NXDOMAIN for one request and
# `Status: 0` later, which failed an in-flight inference with
# `managed_control_unreachable` (route state `request_cleanup_unproven`).
#
# Two defences, neither of which weakens TLS identity:
#   1. resolve once per TTL and reuse the address set, instead of re-resolving on
#      every child request -- this is what removes the repeated dependency;
#   2. inside the caller's budget, retry a negative answer rather than failing the
#      first time.
# A cached address can never be used to impersonate the origin: the original
# hostname stays the SNI/certificate name and verification is unchanged, so a
# stale address simply fails closed, and a failed dial invalidates the entry.
_RELAY_ADDRESS_TTL_SECONDS=120.0
_RELAY_ADDRESS_CACHE_LIMIT=8
_address_cache={}
_address_cache_lock=threading.Lock()

_DNS_RETRY_FRACTION=0.5
_DNS_RETRY_CEILING_SECONDS=5.0
_DNS_RETRY_INTERVAL_SECONDS=0.5


def _cached_addresses(host):
    with _address_cache_lock:
        entry=_address_cache.get(host)
        if entry is None:return None
        stored,addresses=entry
        if time.monotonic()-stored>=_RELAY_ADDRESS_TTL_SECONDS:
            _address_cache.pop(host,None)
            return None
        return list(addresses)


def _store_addresses(host,addresses):
    with _address_cache_lock:
        _address_cache.pop(host,None)
        _address_cache[host]=(time.monotonic(),tuple(addresses))
        while len(_address_cache)>_RELAY_ADDRESS_CACHE_LIMIT:
            _address_cache.pop(next(iter(_address_cache)))


def invalidate_relay_address(host):
    """Drop one cached origin address set; the next connection re-resolves it."""
    with _address_cache_lock:_address_cache.pop(host,None)


def _clear_address_cache():
    with _address_cache_lock:_address_cache.clear()

class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):return None


def _dns_document(host,*,timeout):
    request=Request('https://cloudflare-dns.com/dns-query?'+urlencode({'name':host,'type':'A'}),headers=with_user_agent({'Accept':'application/dns-json'}))
    opener=build_opener(ProxyHandler({}),_NoRedirect())
    with opener.open(request,timeout=timeout) as response:
        raw=response.read(16385)
        if len(raw)>16384:raise OSError('relay_dns_invalid')
        document=json.loads(raw)
    return document


def _doh_addresses(document):
    """Addresses from one DoH answer; ``None`` means "no answer yet, retryable".

    A non-zero ``Status`` is Cloudflare's own negative answer (NXDOMAIN/SERVFAIL)
    for a name it may publish moments later, so it is retryable inside the bounded
    window.  A malformed answer is not: it fails closed as ``relay_dns_invalid``.
    """
    if not isinstance(document,dict):return None
    if document.get('Status')!=0:return None
    answers=document.get('Answer',[])
    if not isinstance(answers,list) or len(answers)>32:raise OSError('relay_dns_invalid')
    return [row.get('data') for row in answers if isinstance(row,dict) and row.get('type')==1]


def relay_addresses(host,*,timeout,fetch=_dns_document):
    if not _RELAY.fullmatch(host):raise OSError('relay_dns_host_invalid')
    cached=_cached_addresses(host)
    if cached is not None:return cached
    try:
        rows=socket.getaddrinfo(host,443,type=socket.SOCK_STREAM)
    except socket.gaierror:
        # URLError and every socket/TLS failure are OSError subclasses, so one
        # clause covers a transport failure of the DoH call itself.
        deadline=time.monotonic()+min(max(1.0,float(timeout)*_DNS_RETRY_FRACTION),_DNS_RETRY_CEILING_SECONDS)
        addresses=None
        while addresses is None:
            try: document=fetch(host,timeout=timeout)
            except OSError: document=None
            addresses=_doh_addresses(document)
            if addresses is None and time.monotonic()>=deadline:
                raise OSError('relay_dns_unavailable') from None
            if addresses is None:time.sleep(_DNS_RETRY_INTERVAL_SECONDS)
    else:
        addresses=[row[4][0] for row in rows]
    try:
        if not addresses or len(addresses)>32:raise ValueError()
        parsed=[ipaddress.ip_address(a) for a in addresses]
        if any(not a.is_global for a in parsed):raise ValueError()
    except (TypeError,ValueError):raise OSError('relay_dns_nonpublic') from None
    resolved=list(dict.fromkeys(str(a) for a in parsed))
    _store_addresses(host,resolved)
    return resolved


class RelayHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        if not _RELAY.fullmatch(self.host):return super().connect()
        if self.port!=443 or self._tunnel_host:raise OSError('relay_origin_invalid')
        if not self._context.check_hostname or self._context.verify_mode!=ssl.CERT_REQUIRED:
            raise OSError('relay_tls_verification_required')
        timeout=float(self.timeout);deadline=time.monotonic()+timeout
        addresses=relay_addresses(self.host,timeout=timeout)
        for address in addresses:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('relay_connect_timeout')
            raw=None
            try:
                raw=socket.create_connection((address,self.port),remaining,self.source_address)
                self.sock=self._context.wrap_socket(raw,server_hostname=self.host)
                return
            except ssl.SSLCertVerificationError:
                if raw is not None:raw.close()
                raise
            except OSError:
                if raw is not None:raw.close()
        # Every cached address failed to dial, so the entry is stale.  Drop it so
        # the next attempt re-resolves instead of retrying the same dead set.
        invalidate_relay_address(self.host)
        raise OSError('relay_connect_unavailable')


class RelayHTTPSHandler(HTTPSHandler):
    def https_open(self,request):
        return self.do_open(RelayHTTPSConnection,request,context=self._context)


def public_opener(*,context=None):
    return build_opener(ProxyHandler({}),_NoRedirect(),RelayHTTPSHandler(context=context or ssl.create_default_context()))


def configure_seed_client(client):
    from urllib.parse import urlsplit
    if _RELAY.fullmatch(urlsplit(client.seed_url).hostname or ''):
        client._opener=public_opener()
    return client
