# SPDX-License-Identifier: AGPL-3.0-only
"""The closed set of signed swarm documents: builders (Python signers) and validators.

Every validator checks the exact key set, field bounds and the signature, and
returns the document unchanged.  Freshness (``at`` vs. now) and state checks
(seq, ownership) belong to the caller, which owns the clock and storage.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
from typing import Any, Mapping

from .wire import (WireError, b64url, canonical, exact_keys, ident, integer, key_bytes, signing_bytes, text,
                   unb64url, verify_document, Verifier)

RECORD = 'mycelium.swarm.record.v1'
DELETE = 'mycelium.swarm.delete.v1'
HEARTBEAT = 'mycelium.swarm.heartbeat.v1'
ACCESS_REQUEST = 'mycelium.swarm.access_request.v1'
DENY = 'mycelium.swarm.deny.v1'
GRANT = 'mycelium.swarm.grant.v1'
OWNER_QUERY = 'mycelium.swarm.owner_query.v1'
GRANTEE_QUERY = 'mycelium.swarm.grantee_query.v1'
ENTRY_OFFER = 'mycelium.swarm.entry_offer.v1'
ENTRY_SEND = 'mycelium.swarm.entry_send.v1'
OPEN_GRANT_REQUEST = 'mycelium.swarm.open_grant_request.v1'
KILL_SWITCH = 'mycelium.swarm.kill_switch.v1'
ENTRY_CANCEL = 'mycelium.swarm.entry_cancel.v1'
LINK = 'mycelium.swarm.link.v1'
LINK_SCHEME = 'mycelium-swarm:'

ACCESS_MODES = ('open', 'request', 'invite')
STATUSES = ('serving', 'warming', 'offline')
SWARM_ID = re.compile(r'sw-[a-z0-9]{20}\Z')
GRANT_ID = re.compile(r'gr-[A-Za-z0-9_-]{22}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9_-]{16,64}\Z')  # the chat service's request ids
NONCE = re.compile(r'[A-Za-z0-9_-]{16,43}\Z')
MODEL = re.compile(r'[A-Za-z0-9._/:+-]{1,96}\Z')
MAX_RECORD_BYTES = 4096
MAX_GRANT_SECONDS = 30 * 86400
MAX_GRANT_REQUESTS = 10_000
MAX_GRANT_TOKENS = 1_000_000
MAX_MEMBERS = 64
MAX_PROMPT_CHARS = 2000
MAX_NEW_TOKENS = 256
TIME_MAX = 2 ** 40
_ORIGIN = re.compile(r'https://(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\Z')
_LOOPBACK_ORIGIN = re.compile(r'http://127\.0\.0\.1:[1-9][0-9]{1,4}\Z')
_PRIVATE_ORIGIN = re.compile(r'https?://([0-9.]{7,15})(?::([1-9][0-9]{1,4}))?\Z')
_RFC1918 = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16'))

_KEYS = {
    RECORD: frozenset({'protocol', 'swarm_id', 'owner_key', 'seq', 'name', 'model', 'description',
                       'access_mode', 'open_quota', 'entry', 'sig'}),
    DELETE: frozenset({'protocol', 'swarm_id', 'owner_key', 'seq', 'sig'}),
    HEARTBEAT: frozenset({'protocol', 'swarm_id', 'signer_key', 'status', 'members', 'served_model', 'at', 'sig'}),
    ACCESS_REQUEST: frozenset({'protocol', 'swarm_id', 'requester_key', 'note', 'at', 'sig'}),
    DENY: frozenset({'protocol', 'swarm_id', 'owner_key', 'requester_key', 'at', 'sig'}),
    GRANT: frozenset({'protocol', 'grant_id', 'swarm_id', 'grantee_key', 'issuer_key', 'issued_at', 'expires_at',
                      'max_requests', 'max_tokens', 'sig'}),
    OWNER_QUERY: frozenset({'protocol', 'swarm_id', 'owner_key', 'at', 'nonce', 'sig'}),
    GRANTEE_QUERY: frozenset({'protocol', 'grantee_key', 'at', 'nonce', 'sig'}),
    ENTRY_OFFER: frozenset({'protocol', 'swarm_id', 'entry_key', 'origin', 'at', 'sig'}),
    ENTRY_SEND: frozenset({'protocol', 'swarm_id', 'key', 'grant', 'prompt', 'max_new_tokens', 'at', 'nonce', 'sig'}),
    OPEN_GRANT_REQUEST: frozenset({'protocol', 'swarm_id', 'key', 'at', 'nonce', 'sig'}),
    KILL_SWITCH: frozenset({'protocol', 'swarm_id', 'owner_key', 'engaged', 'seq', 'at', 'sig'}),
    ENTRY_CANCEL: frozenset({'protocol', 'swarm_id', 'key', 'request_id', 'at', 'nonce', 'sig'}),
}
_QUOTA_KEYS = frozenset({'max_requests', 'max_tokens', 'ttl_seconds'})
DEFAULT_OPEN_QUOTA = {'max_requests': 20, 'max_tokens': 4000, 'ttl_seconds': 86400}


# -- building (Python signers: the A13 app and tests) -------------------------

def sign(signer: Any, document: Mapping[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in document.items() if k != 'sig'}
    return {**body, 'sig': b64url(signer.sign(signing_bytes(body)))}


def new_swarm_id() -> str:
    return 'sw-' + ''.join(secrets.choice('abcdefghijklmnopqrstuvwxyz0123456789') for _ in range(20))


def new_nonce() -> str:
    return b64url(secrets.token_bytes(18))


def swarm_record(owner, *, swarm_id, seq, name, model, description='', access_mode='request', entry=None,
                 open_quota=None) -> dict[str, Any]:
    if open_quota is None and access_mode == 'open':
        open_quota = dict(DEFAULT_OPEN_QUOTA)
    return sign(owner, {'protocol': RECORD, 'swarm_id': swarm_id, 'owner_key': owner.public_key, 'seq': seq,
                        'name': name, 'model': model, 'description': description, 'access_mode': access_mode,
                        'open_quota': open_quota, 'entry': entry})


def delete(owner, swarm_id, seq) -> dict[str, Any]:
    return sign(owner, {'protocol': DELETE, 'swarm_id': swarm_id, 'owner_key': owner.public_key, 'seq': seq})


def heartbeat(signer, swarm_id, *, status, members, served_model, at) -> dict[str, Any]:
    return sign(signer, {'protocol': HEARTBEAT, 'swarm_id': swarm_id, 'signer_key': signer.public_key,
                         'status': status, 'members': members, 'served_model': served_model, 'at': at})


def access_request(requester, swarm_id, *, note, at) -> dict[str, Any]:
    return sign(requester, {'protocol': ACCESS_REQUEST, 'swarm_id': swarm_id,
                            'requester_key': requester.public_key, 'note': note, 'at': at})


def deny(owner, swarm_id, requester_key, *, at) -> dict[str, Any]:
    return sign(owner, {'protocol': DENY, 'swarm_id': swarm_id, 'owner_key': owner.public_key,
                        'requester_key': requester_key, 'at': at})


def grant(issuer, *, swarm_id, grantee_key, issued_at, expires_at, max_requests, max_tokens,
          grant_id=None) -> dict[str, Any]:
    return sign(issuer, {'protocol': GRANT, 'grant_id': grant_id or 'gr-' + b64url(secrets.token_bytes(16))[:22],
                         'swarm_id': swarm_id, 'grantee_key': grantee_key, 'issuer_key': issuer.public_key,
                         'issued_at': issued_at, 'expires_at': expires_at, 'max_requests': max_requests,
                         'max_tokens': max_tokens})


def owner_query(owner, swarm_id, *, at, nonce=None) -> dict[str, Any]:
    return sign(owner, {'protocol': OWNER_QUERY, 'swarm_id': swarm_id, 'owner_key': owner.public_key, 'at': at,
                        'nonce': nonce or new_nonce()})


def grantee_query(grantee, *, at, nonce=None) -> dict[str, Any]:
    return sign(grantee, {'protocol': GRANTEE_QUERY, 'grantee_key': grantee.public_key, 'at': at,
                          'nonce': nonce or new_nonce()})


def entry_offer(entry, swarm_id, origin, *, at) -> dict[str, Any]:
    return sign(entry, {'protocol': ENTRY_OFFER, 'swarm_id': swarm_id, 'entry_key': entry.public_key,
                        'origin': origin, 'at': at})


def entry_send(requester, swarm_id, grant_document, prompt, max_new_tokens, *, at, nonce=None) -> dict[str, Any]:
    return sign(requester, {'protocol': ENTRY_SEND, 'swarm_id': swarm_id, 'key': requester.public_key,
                            'grant': grant_document, 'prompt': prompt, 'max_new_tokens': max_new_tokens, 'at': at,
                            'nonce': nonce or new_nonce()})


def entry_cancel(requester, swarm_id, request_id, *, at, nonce=None) -> dict[str, Any]:
    """The requester's own stop for one running prompt; the id is inside the signature."""
    return sign(requester, {'protocol': ENTRY_CANCEL, 'swarm_id': swarm_id, 'key': requester.public_key,
                            'request_id': request_id, 'at': at, 'nonce': nonce or new_nonce()})


def open_grant_request(requester, swarm_id, *, at, nonce=None) -> dict[str, Any]:
    return sign(requester, {'protocol': OPEN_GRANT_REQUEST, 'swarm_id': swarm_id, 'key': requester.public_key,
                            'at': at, 'nonce': nonce or new_nonce()})


def kill_switch(owner, swarm_id, *, engaged, seq, at) -> dict[str, Any]:
    """Owner-signed switch for new admissions; ``seq`` must grow so an old document cannot replay."""
    return sign(owner, {'protocol': KILL_SWITCH, 'swarm_id': swarm_id, 'owner_key': owner.public_key,
                        'engaged': engaged, 'seq': seq, 'at': at})


def swarm_link(directory_origin: str, swarm_id: str, owner_key: str) -> str:
    """The code/QR a Mac pastes into the A13 app to become this swarm's entry."""
    return LINK_SCHEME + b64url(canonical({'protocol': LINK, 'directory': directory_origin, 'swarm_id': swarm_id,
                                           'owner_key': owner_key}))


def parse_swarm_link(value: Any, *, allow_loopback: bool = False, allow_private: bool = False) -> dict[str, str]:
    import json
    if not isinstance(value, str) or not value.startswith(LINK_SCHEME) or len(value) > 1024:
        raise WireError('swarm_link_invalid')
    try:
        document = json.loads(unb64url(value[len(LINK_SCHEME):], code='swarm_link_invalid'))
    except ValueError:
        raise WireError('swarm_link_invalid') from None
    if not isinstance(document, dict) or set(document) != {'protocol', 'directory', 'swarm_id', 'owner_key'} \
            or document['protocol'] != LINK:
        raise WireError('swarm_link_invalid')
    ident(document['swarm_id'], SWARM_ID, 'swarm_link_invalid')
    key_bytes(document['owner_key'])
    origin(document['directory'], allow_loopback=allow_loopback, allow_private=allow_private,
           code='swarm_link_invalid')
    return document


# -- validation -----------------------------------------------------------------

def _private_origin(value: str) -> bool:
    """``http(s)://<RFC 1918 IPv4>[:port]`` -- the LAN opt-in, never a name or a public literal."""
    match = _PRIVATE_ORIGIN.fullmatch(value)
    if match is None or (match[2] is not None and int(match[2]) > 65535):
        return False
    try:
        address = ipaddress.IPv4Address(match[1])
    except ValueError:
        return False
    return any(address in network for network in _RFC1918)


def origin(value: Any, *, allow_loopback: bool, allow_private: bool = False,
           code: str = 'entry_origin_invalid') -> str:
    if isinstance(value, str) and (_ORIGIN.fullmatch(value) or (allow_loopback and _LOOPBACK_ORIGIN.fullmatch(value))
                                   or (allow_private and _private_origin(value))):
        return value
    raise WireError(code)


def _key(value: Any, code: str) -> str:
    try:
        key_bytes(value)
    except WireError:
        raise WireError(code) from None
    return value


def _time(value: Any) -> int:
    return integer(value, low=0, high=TIME_MAX, code='time_invalid')


def check_record(document: Any, verifier: Verifier, *, allow_loopback: bool = False,
                 allow_private: bool = False) -> dict[str, Any]:
    exact_keys(document, RECORD, _KEYS[RECORD])
    if len(canonical(document)) > MAX_RECORD_BYTES:
        raise WireError('record_too_large', 413)
    ident(document['swarm_id'], SWARM_ID, 'swarm_id_invalid')
    _key(document['owner_key'], 'owner_key_invalid')
    integer(document['seq'], low=1, high=2 ** 40, code='seq_invalid')
    text(document['name'], low=1, high=48, code='name_invalid')
    ident(document['model'], MODEL, 'model_invalid')
    text(document['description'], low=0, high=280, code='description_invalid')
    if document['access_mode'] not in ACCESS_MODES:
        raise WireError('access_mode_invalid')
    quota = document['open_quota']
    if document['access_mode'] == 'open' or quota is not None:
        if not isinstance(quota, dict) or set(quota) != _QUOTA_KEYS:
            raise WireError('open_quota_invalid')
        integer(quota['max_requests'], low=1, high=1000, code='open_quota_invalid')
        integer(quota['max_tokens'], low=1, high=200_000, code='open_quota_invalid')
        integer(quota['ttl_seconds'], low=60, high=MAX_GRANT_SECONDS, code='open_quota_invalid')
    entry = document['entry']
    if entry is not None:
        if not isinstance(entry, dict) or set(entry) != {'key', 'origin'}:
            raise WireError('entry_invalid')
        _key(entry['key'], 'entry_invalid')
        origin(entry['origin'], allow_loopback=allow_loopback, allow_private=allow_private)
    verify_document(document, document['owner_key'], verifier)
    return document


def check(document: Any, protocol: str, verifier: Verifier, *, allow_loopback: bool = False,
          allow_private: bool = False) -> dict[str, Any]:
    """Validate any non-record document type and its signature."""
    exact_keys(document, protocol, _KEYS[protocol])
    if 'swarm_id' in document:
        ident(document['swarm_id'], SWARM_ID, 'swarm_id_invalid')
    if 'at' in document:
        _time(document['at'])
    if 'nonce' in document:
        ident(document['nonce'], NONCE, 'nonce_invalid')
    if protocol == DELETE:
        integer(document['seq'], low=1, high=2 ** 40, code='seq_invalid')
        signer = _key(document['owner_key'], 'owner_key_invalid')
    elif protocol == HEARTBEAT:
        if document['status'] not in STATUSES:
            raise WireError('status_invalid')
        integer(document['members'], low=0, high=MAX_MEMBERS, code='members_invalid')
        if document['served_model'] is not None:
            ident(document['served_model'], MODEL, 'model_invalid')
        signer = _key(document['signer_key'], 'signer_key_invalid')
    elif protocol == ACCESS_REQUEST:
        text(document['note'], low=0, high=140, code='note_invalid')
        signer = _key(document['requester_key'], 'requester_key_invalid')
    elif protocol == DENY:
        _key(document['requester_key'], 'requester_key_invalid')
        signer = _key(document['owner_key'], 'owner_key_invalid')
    elif protocol == GRANT:
        ident(document['grant_id'], GRANT_ID, 'grant_invalid')
        _key(document['grantee_key'], 'grant_invalid')
        signer = _key(document['issuer_key'], 'grant_invalid')
        issued, expires = _time(document['issued_at']), _time(document['expires_at'])
        if not issued < expires <= issued + MAX_GRANT_SECONDS:
            raise WireError('grant_lifetime_invalid')
        integer(document['max_requests'], low=1, high=MAX_GRANT_REQUESTS, code='grant_quota_invalid')
        integer(document['max_tokens'], low=1, high=MAX_GRANT_TOKENS, code='grant_quota_invalid')
    elif protocol == OWNER_QUERY:
        signer = _key(document['owner_key'], 'owner_key_invalid')
    elif protocol == GRANTEE_QUERY:
        signer = _key(document['grantee_key'], 'grantee_key_invalid')
    elif protocol == ENTRY_OFFER:
        origin(document['origin'], allow_loopback=allow_loopback, allow_private=allow_private)
        signer = _key(document['entry_key'], 'entry_key_invalid')
    elif protocol == ENTRY_SEND:
        if not isinstance(document['prompt'], str) or not document['prompt'].strip():
            raise WireError('entry_prompt_required')
        if len(document['prompt']) > MAX_PROMPT_CHARS:
            raise WireError('entry_prompt_too_long', 413)
        integer(document['max_new_tokens'], low=1, high=2 ** 31, code='entry_max_new_tokens_invalid')
        if not isinstance(document['grant'], dict):
            raise WireError('grant_invalid', 403)
        signer = _key(document['key'], 'key_invalid')
    elif protocol == OPEN_GRANT_REQUEST:
        signer = _key(document['key'], 'key_invalid')
    elif protocol == ENTRY_CANCEL:
        ident(document['request_id'], REQUEST_ID, 'entry_request_invalid')
        signer = _key(document['key'], 'key_invalid')
    elif protocol == KILL_SWITCH:
        if type(document['engaged']) is not bool:
            raise WireError('kill_engaged_invalid')
        integer(document['seq'], low=1, high=2 ** 40, code='seq_invalid')
        signer = _key(document['owner_key'], 'owner_key_invalid')
    else:  # pragma: no cover - closed set
        raise WireError('protocol_invalid')
    verify_document(document, signer, verifier)
    return document


def fresh(at: int, now: int, skew: int, code: str = 'clock_skew') -> None:
    if abs(at - now) > skew:
        raise WireError(code)
