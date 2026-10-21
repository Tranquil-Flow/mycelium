# SPDX-License-Identifier: AGPL-3.0-only
"""The swarm directory: a phonebook of signed swarm records (pure core).

It lists every swarm (visible to all, for the demo), keeps owner-signed
records, entry/owner heartbeats, access requests, owner-signed grants for the
grantee to fetch, and entry offers (a Mac asking a web-created swarm's owner to
attach it as the entry).  It grants nothing itself: every right is a signature
by the swarm owner's key, enforced by the swarm's entry (``entry.EntryGuard``).

``Directory.handle(method, path, body, client)`` is transport-free: ``client``
is an opaque per-client rate key (``ip:<addr>``) supplied by the HTTP layer.
No request content, IP or key is ever logged; nothing is kept beyond the
records themselves (no analytics).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable

from . import docs
from .ratelimit import WindowLimiter
from .wire import Verifier, WireError, fingerprint, parse_json_object

PRIVACY_NOTE = ('Your prompt is seen by this swarm\'s nodes (the Macs that run it). It is not end-to-end '
                'encrypted from them. The directory never sees prompts and keeps no logs or analytics. '
                'Cloudflare terminates TLS for this site and for swarm entries on trycloudflare.com and can see '
                'your IP address, timing and plaintext prompts.')
#: Signed read-only queries: own budgets, not the write budgets.
QUERY_ROUTES = frozenset({'/owner-inbox', '/my-grants'})
API_ROUTES = frozenset({'/records', '/delete', '/heartbeat', '/access-requests', '/owner-inbox', '/grants', '/deny',
                        '/my-grants', '/entry-offers'})
_STATUS_ORDER = {'serving': 0, 'warming': 1, 'offline': 2}


@dataclass(frozen=True)
class Limits:
    max_body: int = 8192
    max_swarms: int = 500
    max_swarms_per_owner: int = 5
    max_requests_per_swarm: int = 64
    max_offers_per_swarm: int = 4
    max_grants_per_grantee: int = 64
    max_live_nonces: int = 20000
    heartbeat_ttl: int = 90
    skew: int = 300
    offer_ttl: int = 3600
    writes_per_ip_per_minute: int = 30
    writes_per_key_per_minute: int = 20
    reads_per_ip_per_minute: int = 240
    #: signed read-only queries (/my-grants, /owner-inbox): the page repeats them, so they do not spend the write budgets
    queries_per_ip_per_minute: int = 240
    queries_per_key_per_minute: int = 90
    #: directory-wide budget of *signature-verified* writes (records, deletes, requests, grants, denials, offers)
    writes_per_minute: int = 600
    #: signature-verified heartbeats have their own budget: a write flood cannot turn live swarms offline
    heartbeats_per_minute: int = 1200


class Directory:
    def __init__(self, store: Any, verifier: Verifier, *, clock: Callable[[], float] = time.time,
                 limits: Limits = Limits(), allow_loopback_entries: bool = False,
                 allow_private_entries: bool = False) -> None:
        self.store = store
        self.verifier = verifier
        self.clock = clock
        self.limits = limits
        self.allow_loopback = allow_loopback_entries
        self.allow_private = allow_private_entries
        self._ip_writes = WindowLimiter(limits.writes_per_ip_per_minute)
        self._key_writes = WindowLimiter(limits.writes_per_key_per_minute)
        self._ip_reads = WindowLimiter(limits.reads_per_ip_per_minute)
        self._ip_queries = WindowLimiter(limits.queries_per_ip_per_minute)
        self._key_queries = WindowLimiter(limits.queries_per_key_per_minute)
        self._writes = WindowLimiter(limits.writes_per_minute, max_keys=1)
        self._beats = WindowLimiter(limits.heartbeats_per_minute, max_keys=1)
        self._nonce_inserts = 0

    def now(self) -> int:
        return int(self.clock())

    # -- transport-free entry point -------------------------------------------
    def handle(self, method: str, path: str, body: bytes, client: str) -> tuple[int, dict[str, Any]]:
        try:
            return 200, self._route(method, path, body, client)
        except WireError as error:
            return error.status, {'error': error.code}

    def _route(self, method, path, body, client):
        now = self.now()
        if method == 'GET':
            if path == '/swarms' or (path.startswith('/swarms/') and docs.SWARM_ID.fullmatch(path[8:])):
                if not self._ip_reads.allow(client, now):
                    raise WireError('rate_limited_ip', 429)
                if path == '/swarms':
                    return {'swarms': self.list_swarms(), 'privacy': PRIVACY_NOTE, 'now': now}
                row = self.store.get('swarm', path[8:])
                if row is None:
                    raise WireError('swarm_unknown', 404)
                return {'swarm': self._public(row, now)}
            if path in API_ROUTES:
                raise WireError('method_not_allowed', 405)
            raise WireError('route_unknown', 404)
        if method != 'POST':
            if path == '/swarms' or path in API_ROUTES:
                raise WireError('method_not_allowed', 405)
            raise WireError('route_unknown', 404)
        if path not in API_ROUTES:
            raise WireError('route_unknown', 404)
        if len(body) > self.limits.max_body:
            raise WireError('body_too_large', 413)
        if not (self._ip_queries if path in QUERY_ROUTES else self._ip_writes).allow(client, now):
            raise WireError('rate_limited_ip', 429)
        document = parse_json_object(body, limit=self.limits.max_body)
        handler = {'/records': self._put_record, '/delete': self._delete, '/heartbeat': self._heartbeat,
                   '/access-requests': self._access_request, '/owner-inbox': self._owner_inbox,
                   '/grants': self._grant, '/deny': self._deny, '/my-grants': self._my_grants,
                   '/entry-offers': self._entry_offer}[path]
        return handler(document, now)

    def _charge_write(self, now: int) -> None:
        """Spend the directory-wide write budget; called only once the document's signature has been verified."""
        if not self._writes.allow('*', now):
            raise WireError('directory_busy', 503)

    def _key_rate(self, key: str, now: int) -> None:
        if not self._key_writes.allow(key, now):
            raise WireError('rate_limited_key', 429)

    def _query_rate(self, key: str, now: int) -> None:
        if not self._key_queries.allow(key, now):
            raise WireError('rate_limited_key', 429)

    def _owned(self, swarm_id: str, owner_key: str) -> dict[str, Any]:
        row = self.store.get('swarm', swarm_id)
        if row is None:
            raise WireError('swarm_unknown', 404)
        if row['record']['owner_key'] != owner_key:
            raise WireError('swarm_owner_mismatch', 403)
        return row

    def _consume_nonce(self, nonce: str, now: int) -> None:
        if self.store.get('nonce', nonce) is not None:
            raise WireError('nonce_replayed', 409)
        self._nonce_inserts += 1
        if self._nonce_inserts % 256 == 0:
            live = 0
            for key, value in self.store.scan('nonce'):
                if value['expires'] <= now:
                    self.store.delete('nonce', key)
                else:
                    live += 1
            if live >= self.limits.max_live_nonces:
                raise WireError('directory_busy', 503)
        self.store.put('nonce', nonce, {'expires': now + 2 * self.limits.skew + 1})

    # -- writes ----------------------------------------------------------------
    def _put_record(self, document, now):
        record = docs.check_record(document, self.verifier, allow_loopback=self.allow_loopback,
                                    allow_private=self.allow_private)
        self._key_rate(record['owner_key'], now)
        self._charge_write(now)
        swarm_id = record['swarm_id']
        if self.store.get('tomb', swarm_id) is not None:
            raise WireError('swarm_id_retired', 409)
        row = self.store.get('swarm', swarm_id)
        if row is None:
            rows = [value for _key, value in self.store.scan('swarm')]
            if len(rows) >= self.limits.max_swarms:
                raise WireError('directory_full', 429)
            if sum(1 for value in rows if value['record']['owner_key'] == record['owner_key']) \
                    >= self.limits.max_swarms_per_owner:
                raise WireError('owner_swarm_cap', 429)
            row = {'record': record, 'heartbeat': None, 'heartbeat_received': None, 'created': now, 'updated': now}
        else:
            if row['record']['owner_key'] != record['owner_key']:
                raise WireError('swarm_owner_mismatch', 403)
            if record['seq'] <= row['record']['seq']:
                raise WireError('record_seq_stale', 409)
            old_entry = row['record']['entry']
            if old_entry != record['entry'] and row.get('heartbeat') is not None \
                    and row['heartbeat']['signer_key'] != record['owner_key']:
                row['heartbeat'] = None  # the detached entry no longer speaks for the swarm
                row['heartbeat_received'] = None
            row = dict(row, record=record, updated=now)
            if record['entry'] is not None:
                self.store.delete('offer', swarm_id + '/' + record['entry']['key'])
        self.store.put('swarm', swarm_id, row)
        return {'swarm': self._public(row, now)}

    def _delete(self, document, now):
        document = docs.check(document, docs.DELETE, self.verifier)
        self._key_rate(document['owner_key'], now)
        self._charge_write(now)
        row = self._owned(document['swarm_id'], document['owner_key'])
        if document['seq'] <= row['record']['seq']:
            raise WireError('record_seq_stale', 409)
        swarm_id = document['swarm_id']
        self.store.put('tomb', swarm_id, {'owner_key': document['owner_key'], 'seq': document['seq'], 'at': now})
        self.store.delete('swarm', swarm_id)
        for table in ('request', 'offer'):
            for key, _value in self.store.scan(table, swarm_id + '/'):
                self.store.delete(table, key)
        for key, value in self.store.scan('grant'):
            if value.get('swarm_id') == swarm_id:
                self.store.delete('grant', key)
        return {'deleted': swarm_id}

    def _heartbeat(self, document, now):
        document = docs.check(document, docs.HEARTBEAT, self.verifier)
        docs.fresh(document['at'], now, self.limits.skew)
        row = self.store.get('swarm', document['swarm_id'])
        if row is None:
            raise WireError('swarm_unknown', 404)
        record = row['record']
        allowed = {record['owner_key']} | ({record['entry']['key']} if record['entry'] else set())
        if document['signer_key'] not in allowed:
            raise WireError('heartbeat_signer_not_authorized', 403)
        self._key_rate(document['signer_key'], now)
        if not self._beats.allow('*', now):  # verified and authorised heartbeats only, on their own budget
            raise WireError('directory_busy', 503)
        previous = row.get('heartbeat')
        if previous is not None and document['at'] <= previous['at']:
            raise WireError('heartbeat_stale', 409)
        row = dict(row, heartbeat=document, heartbeat_received=now)
        self.store.put('swarm', document['swarm_id'], row)
        return {'status': self._status(row, now)}

    def _access_request(self, document, now):
        document = docs.check(document, docs.ACCESS_REQUEST, self.verifier)
        docs.fresh(document['at'], now, self.limits.skew)
        self._key_rate(document['requester_key'], now)
        self._charge_write(now)
        row = self.store.get('swarm', document['swarm_id'])
        if row is None:
            raise WireError('swarm_unknown', 404)
        mode = row['record']['access_mode']
        if mode == 'invite':
            raise WireError('swarm_invite_only', 403)
        if mode == 'open':
            raise WireError('swarm_is_open', 409)
        swarm_id, requester = document['swarm_id'], document['requester_key']
        key = swarm_id + '/' + requester
        if self.store.get('request', key) is None and \
                len(self.store.scan('request', swarm_id + '/')) >= self.limits.max_requests_per_swarm:
            raise WireError('swarm_request_queue_full', 429)
        self.store.put('request', key, {'request': document, 'state': 'pending', 'received': now})
        state = self.store.get('grant', requester + '/' + swarm_id)
        if state is None or state.get('state') != 'granted':
            self._grantee_state(requester, swarm_id, {'state': 'pending', 'swarm_id': swarm_id})
        return {'state': 'pending'}

    def _grantee_state(self, grantee, swarm_id, value):
        key = grantee + '/' + swarm_id
        if self.store.get('grant', key) is None and \
                len(self.store.scan('grant', grantee + '/')) >= self.limits.max_grants_per_grantee:
            raise WireError('grantee_grant_cap', 429)
        self.store.put('grant', key, value)

    def _grant(self, document, now):
        document = docs.check(document, docs.GRANT, self.verifier)
        row = self.store.get('swarm', document['swarm_id'])
        if row is None:
            raise WireError('swarm_unknown', 404)
        if document['issuer_key'] != row['record']['owner_key']:
            raise WireError('grant_issuer_not_owner', 403)
        self._key_rate(document['issuer_key'], now)
        self._charge_write(now)
        if document['expires_at'] <= now:
            raise WireError('grant_expired', 403)
        swarm_id, grantee = document['swarm_id'], document['grantee_key']
        self._grantee_state(grantee, swarm_id, {'state': 'granted', 'swarm_id': swarm_id, 'grant': document})
        request = self.store.get('request', swarm_id + '/' + grantee)
        if request is not None:
            self.store.put('request', swarm_id + '/' + grantee, dict(request, state='granted'))
        return {'state': 'granted', 'grant_id': document['grant_id']}

    def _deny(self, document, now):
        document = docs.check(document, docs.DENY, self.verifier)
        docs.fresh(document['at'], now, self.limits.skew)
        self._owned(document['swarm_id'], document['owner_key'])
        self._key_rate(document['owner_key'], now)
        self._charge_write(now)
        swarm_id, requester = document['swarm_id'], document['requester_key']
        request = self.store.get('request', swarm_id + '/' + requester)
        if request is None:
            raise WireError('request_unknown', 404)
        self.store.put('request', swarm_id + '/' + requester, dict(request, state='denied'))
        self._grantee_state(requester, swarm_id, {'state': 'denied', 'swarm_id': swarm_id})
        return {'state': 'denied'}

    def _entry_offer(self, document, now):
        document = docs.check(document, docs.ENTRY_OFFER, self.verifier, allow_loopback=self.allow_loopback,
                              allow_private=self.allow_private)
        docs.fresh(document['at'], now, self.limits.skew)
        self._key_rate(document['entry_key'], now)
        self._charge_write(now)
        swarm_id = document['swarm_id']
        if self.store.get('swarm', swarm_id) is None:
            raise WireError('swarm_unknown', 404)
        offers = self.store.scan('offer', swarm_id + '/')
        for key, value in offers:
            if now - value['received'] > self.limits.offer_ttl:
                self.store.delete('offer', key)
        key = swarm_id + '/' + document['entry_key']
        if self.store.get('offer', key) is None and \
                len(self.store.scan('offer', swarm_id + '/')) >= self.limits.max_offers_per_swarm:
            raise WireError('entry_offers_full', 429)
        self.store.put('offer', key, {'offer': document, 'received': now})
        return {'state': 'offered', 'entry_fingerprint': fingerprint(document['entry_key'])}

    # -- signed reads ------------------------------------------------------------
    def _owner_inbox(self, document, now):
        document = docs.check(document, docs.OWNER_QUERY, self.verifier)
        docs.fresh(document['at'], now, self.limits.skew)
        self._owned(document['swarm_id'], document['owner_key'])
        self._query_rate(document['owner_key'], now)
        self._consume_nonce(document['nonce'], now)
        swarm_id = document['swarm_id']
        requests = [{'requester_key': v['request']['requester_key'],
                     'requester_fingerprint': fingerprint(v['request']['requester_key']),
                     'note': v['request']['note'], 'at': v['request']['at'], 'state': v['state']}
                    for _k, v in self.store.scan('request', swarm_id + '/')]
        offers = [{'entry_key': v['offer']['entry_key'], 'entry_fingerprint': fingerprint(v['offer']['entry_key']),
                   'origin': v['offer']['origin'], 'at': v['offer']['at']}
                  for _k, v in self.store.scan('offer', swarm_id + '/')
                  if now - v['received'] <= self.limits.offer_ttl]
        requests.sort(key=lambda r: (r['state'] != 'pending', -r['at']))
        return {'requests': requests, 'entry_offers': offers}

    def _my_grants(self, document, now):
        document = docs.check(document, docs.GRANTEE_QUERY, self.verifier)
        docs.fresh(document['at'], now, self.limits.skew)
        self._query_rate(document['grantee_key'], now)
        self._consume_nonce(document['nonce'], now)
        grants, denied, pending = [], [], []
        for _key, value in self.store.scan('grant', document['grantee_key'] + '/'):
            if value['state'] == 'granted' and value['grant']['expires_at'] > now:
                grants.append(value['grant'])
            elif value['state'] == 'denied':
                denied.append(value['swarm_id'])
            elif value['state'] == 'pending':
                pending.append(value['swarm_id'])
        return {'grants': grants, 'denied': denied, 'pending': pending}

    # -- public view -------------------------------------------------------------
    def _status(self, row, now):
        heartbeat = row.get('heartbeat')
        if heartbeat is None or row.get('heartbeat_received') is None or \
                now - row['heartbeat_received'] > self.limits.heartbeat_ttl:
            return 'offline'
        return heartbeat['status']

    def _public(self, row, now):
        record = row['record']
        status = self._status(row, now)
        heartbeat = row.get('heartbeat') if status != 'offline' else None
        entry = record['entry']
        return {'swarm_id': record['swarm_id'], 'name': record['name'], 'model': record['model'],
                'description': record['description'], 'access_mode': record['access_mode'],
                'open_quota': record['open_quota'], 'owner_key': record['owner_key'],
                'owner_fingerprint': fingerprint(record['owner_key']), 'status': status,
                'members': 0 if heartbeat is None else heartbeat['members'],
                'served_model': None if heartbeat is None else heartbeat['served_model'],
                'entry_origin': None if entry is None else entry['origin'],
                'entry_fingerprint': None if entry is None else fingerprint(entry['key']),
                'last_seen_seconds': None if row.get('heartbeat_received') is None
                else max(0, now - row['heartbeat_received']),
                'privacy': PRIVACY_NOTE, 'record': record}

    def list_swarms(self) -> list[dict[str, Any]]:
        now = self.now()
        rows = [self._public(value, now) for _key, value in self.store.scan('swarm')]
        rows.sort(key=lambda r: (_STATUS_ORDER[r['status']], r['name'].lower(), r['swarm_id']))
        return rows
