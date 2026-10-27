# SPDX-License-Identifier: AGPL-3.0-only
"""'Publish this run as a swarm': the owner app's link to the public swarm directory.

Two modes, one Ed25519 key per installation (``<root>/swarm-key``, 0600):

* ``app``  -- this app creates the swarm: its key is the swarm owner *and* the
  entry.  It signs the record (entry origin = this app's public relay), sends
  heartbeats, lists access requests and signs grants (approve/deny in the app).
* ``link`` -- the swarm was created in a browser (owner key in WebCrypto).  The
  owner pastes its ``mycelium-swarm:`` link here; this app offers itself as the
  entry and starts heartbeating/serving only after the owner signs a record
  naming this app's key (the fingerprint shown on both screens).

Heartbeats say ``serving`` only when the owner run is ``serving`` with
``route_ready`` (the existing serving == route-qualified rule), ``warming``
while the run is running, else ``offline``.  Prompts never pass the directory:
browsers reach ``/swarm-entry/v1/*`` on this app's relay, where
``swarm_entry`` enforces the owner-signed grants.
"""
from __future__ import annotations

import functools
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from mycelium_platform_neutral.network_mode import NetworkMode
from mycelium_swarm_directory import docs
from mycelium_http_identity import with_user_agent
from mycelium_swarm_directory.crypto import Ed25519Signer, verify
from mycelium_swarm_directory.entry import EntryGateway, EntryGuard, EntryPolicy, UsageLedger
from mycelium_swarm_directory.wire import WireError, fingerprint

ROUTES = frozenset({'/api/swarm/publish', '/api/swarm/unpublish', '/api/swarm/inbox', '/api/swarm/approve',
                    '/api/swarm/deny', '/api/swarm/revoke', '/api/swarm/kill'})
HEARTBEAT_SECONDS = 30
OFFER_SECONDS = 300
_CODE = re.compile(r'[a-z][a-z0-9_]{0,79}\Z')


class SwarmPublishError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if isinstance(code, str) and _CODE.fullmatch(code) else 'swarm_publish_failed'
        super().__init__(self.code)


class DirectoryClient:
    def __init__(self, origin: str, *, timeout: float = 8.0, allow_private: bool = False) -> None:
        try:
            docs.origin(origin, allow_loopback=True, allow_private=allow_private,
                        code='swarm_directory_origin_invalid')
        except WireError as error:
            raise SwarmPublishError(error.code) from None
        self.origin = origin
        self.timeout = timeout

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _call(self, path: str, document: Any = None) -> dict[str, Any]:
        data = None if document is None else json.dumps(document, separators=(',', ':')).encode()
        headers = {'Accept': 'application/json'}
        if data is not None:
            headers.update({'Content-Type': 'application/json', 'X-Mycelium-Swarms': '1'})
        request = urllib.request.Request(self.origin + '/swarms/api/v1' + path, data=data, headers=with_user_agent(headers),
                                         method='GET' if data is None else 'POST')
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return json.loads(response.read(1 << 20))
        except urllib.error.HTTPError as error:
            try:
                code = json.loads(error.read(4096)).get('error')
            except Exception:
                code = None
            raise SwarmPublishError('swarm_directory_' + code if isinstance(code, str) and _CODE.fullmatch(code)
                                    else 'swarm_directory_http_%d' % error.code) from None
        except (OSError, ValueError):
            raise SwarmPublishError('swarm_directory_unreachable') from None

    def get(self, path: str) -> dict[str, Any]:
        return self._call(path)

    def post(self, path: str, document: Any) -> dict[str, Any]:
        return self._call(path, document)


def run_state(app: Any) -> tuple[str, int, str | None]:
    """(status, members, served_model) from the owner run; serving == route-qualified."""
    run = getattr(app, 'owner_run', None)
    try:
        status = None if run is None else run.status()
    except Exception:
        status = None
    if not status:
        return 'offline', 0, None
    members = len(status.get('participants') or [])
    if status.get('state') == 'serving' and status.get('route_ready') is True and status.get('registry_origin'):
        try:
            model = app.run_model_contract().model_id
        except Exception:
            model = None
        return 'serving', members, model if isinstance(model, str) and docs.MODEL.fullmatch(model) else None
    if status.get('state') == 'running':
        return 'warming', members, None
    return 'offline', 0, None


def _relay_origin(app: Any) -> str | None:
    relay = getattr(app, 'public_relay', None)
    origin = getattr(relay, 'public_origin', None) if relay is not None else None
    return origin if isinstance(origin, str) and origin.startswith('https://') else None


class SwarmPublisher:
    def __init__(self, app: Any, *, origin: Callable[[], str | None] | None = None,
                 client_factory: Callable[[str], Any] = DirectoryClient, clock: Callable[[], float] = time.time,
                 chat: Callable[[], Any] | None = None, live: Callable[[], Any] | None = None,
                 network_mode: NetworkMode = NetworkMode.INTERNET) -> None:
        self.network_mode = network_mode
        self._allow_private = network_mode.allows_private_origins
        if self._allow_private and client_factory is DirectoryClient:
            client_factory = functools.partial(DirectoryClient, allow_private=True)
        self.app = app
        self.root = Path(app.root)
        self._origin = origin or (lambda: _relay_origin(app))
        self._client_factory = client_factory
        self.clock = clock
        from .swarm_stats import AppStats
        self._stats = AppStats(clock=clock)
        self.signer = Ed25519Signer.load_or_create(self.root / 'swarm-key')
        self.ledger = UsageLedger(self.root / 'swarm-entry.sqlite3')
        self._op = threading.RLock()
        self._snap = threading.Lock()
        self._policy: EntryPolicy | None = None
        self._last_offer = 0.0
        self._last_heartbeat = None
        self._last_at = 0
        self._error: str | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = self._load()
        if self.state.get('enabled') and isinstance(self.state.get('record'), dict):
            self._set_policy(self.state['record'])  # re-verified, never trusted from disk
        if chat is None:
            from .chat import service as _chat_service
            chat = lambda: _chat_service(app)  # noqa: E731
        if live is None:
            from .swarm_live import live_state
            live = lambda: live_state(app)  # noqa: E731
        self.gateway = EntryGateway(EntryGuard(self.policy, self.signer, verify, self.ledger, clock=clock), chat, live)

    # -- persistence -----------------------------------------------------------
    def _path(self) -> Path:
        return self.root / 'swarm-publish.json'

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self._path().read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        temporary = self._path().with_suffix('.tmp')
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump(self.state, stream, sort_keys=True)
        os.replace(temporary, self._path())

    def _client(self) -> Any:
        return self._client_factory(self.state['directory'])

    def now(self) -> int:
        return int(self.clock())

    # -- policy used by the entry guard -----------------------------------------
    def policy(self) -> EntryPolicy | None:
        with self._snap:
            return self._policy if self.state.get('enabled') else None

    def _set_policy(self, record: dict[str, Any] | None) -> None:
        policy = None
        if record is not None:
            try:
                policy = EntryPolicy.from_record(record, verify, entry_key=self.signer.public_key,
                                              allow_private=self._allow_private)
            except WireError:
                policy = None
        with self._snap:
            self._policy = policy

    # -- owner actions ----------------------------------------------------------
    def publish(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise SwarmPublishError('invalid_request')
        with self._op:
            if 'link' in body:
                return self._publish_link(body)
            return self._publish_app(body)

    def _publish_app(self, body):
        allowed = {'directory', 'name', 'access_mode', 'description', 'open_quota'}
        if not set(body) <= allowed or not {'directory', 'name', 'access_mode'} <= set(body):
            raise SwarmPublishError('invalid_request')
        origin = self._origin()
        if origin is None:
            raise SwarmPublishError('swarm_relay_required')
        try:
            model = self.app.run_model_contract().model_id
        except Exception:
            raise SwarmPublishError('swarm_model_unknown') from None
        previous = self.state if self.state.get('mode') == 'app' else {}
        same = previous.get('directory') == body['directory'] and previous.get('swarm_id')
        swarm_id = previous['swarm_id'] if same else docs.new_swarm_id()
        seq = previous.get('record', {}).get('seq', 0) + 1 if same else 1
        try:
            record = docs.swarm_record(self.signer, swarm_id=swarm_id, seq=seq, name=body['name'], model=model,
                                       description=body.get('description', ''), access_mode=body['access_mode'],
                                       open_quota=body.get('open_quota'),
                                       entry={'key': self.signer.public_key, 'origin': origin})
            docs.check_record(record, verify, allow_loopback=True, allow_private=self._allow_private)
        except WireError as error:
            raise SwarmPublishError('swarm_' + error.code) from None
        self._client_factory(body['directory']).post('/records', record)  # refused -> state unchanged
        self.state = {'enabled': True, 'mode': 'app', 'directory': body['directory'], 'swarm_id': swarm_id,
                      'owner_key': self.signer.public_key, 'record': record}
        self._save()
        self._set_policy(record)
        self._error = None
        self._heartbeat(force=True)
        return self.status()

    def _publish_link(self, body):
        if set(body) != {'link'}:
            raise SwarmPublishError('invalid_request')
        try:
            link = docs.parse_swarm_link(body['link'], allow_loopback=True, allow_private=self._allow_private)
        except WireError as error:
            raise SwarmPublishError(error.code) from None
        if self._origin() is None:
            raise SwarmPublishError('swarm_relay_required')
        self.state = {'enabled': True, 'mode': 'link', 'directory': link['directory'], 'swarm_id': link['swarm_id'],
                      'owner_key': link['owner_key'], 'record': None}
        self._save()
        self._error = None
        self._refresh_link(force_offer=True)
        return self.status()

    def unpublish(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or not set(body) <= {'delete'}:
            raise SwarmPublishError('invalid_request')
        with self._op:
            if not self.state.get('enabled'):
                return self.status()
            try:
                self._heartbeat(force=True, status_override='offline')
            except SwarmPublishError:
                pass
            if body.get('delete') is True and self.state.get('mode') == 'app':
                record = self.state['record']
                self._client().post('/delete', docs.delete(self.signer, record['swarm_id'], record['seq'] + 1))
                self.state = {}
            else:
                self.state['enabled'] = False
            self._save()
            self._set_policy(None)
        return self.status()

    def _owner_only(self):
        if not self.state.get('enabled') or self.state.get('mode') != 'app':
            raise SwarmPublishError('swarm_owner_in_browser' if self.state.get('mode') == 'link'
                                    else 'swarm_not_published')

    def inbox(self, body: Any) -> dict[str, Any]:
        if body:
            raise SwarmPublishError('invalid_request')
        with self._op:
            self._owner_only()
            return self._client().post('/owner-inbox', docs.owner_query(self.signer, self.state['swarm_id'],
                                                                        at=self.now()))

    def approve(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) != {'requester_key', 'hours', 'max_requests', 'max_tokens'}:
            raise SwarmPublishError('invalid_request')
        with self._op:
            self._owner_only()
            hours = body['hours']
            if type(hours) is not int or not 1 <= hours <= 24 * 30:
                raise SwarmPublishError('swarm_grant_hours_invalid')
            now = self.now()
            try:
                grant = docs.grant(self.signer, swarm_id=self.state['swarm_id'], grantee_key=body['requester_key'],
                                   issued_at=now, expires_at=now + hours * 3600,
                                   max_requests=body['max_requests'], max_tokens=body['max_tokens'])
                docs.check(grant, docs.GRANT, verify)
            except WireError as error:
                raise SwarmPublishError('swarm_' + error.code) from None
            self._client().post('/grants', grant)
            return {'grant_id': grant['grant_id'], 'grantee_fingerprint': fingerprint(grant['grantee_key']),
                    'expires_at': grant['expires_at']}

    def deny(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) != {'requester_key'}:
            raise SwarmPublishError('invalid_request')
        with self._op:
            self._owner_only()
            try:
                document = docs.deny(self.signer, self.state['swarm_id'], body['requester_key'], at=self.now())
            except WireError as error:
                raise SwarmPublishError('swarm_' + error.code) from None
            return self._client().post('/deny', document)

    def revoke(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict) or set(body) != {'grant_id'} or not isinstance(body['grant_id'], str) \
                or not docs.GRANT_ID.fullmatch(body['grant_id']):
            raise SwarmPublishError('invalid_request')
        self.ledger.revoke(body['grant_id'], self.now())
        return {'revoked': body['grant_id']}

    def kill(self, body: Any) -> dict[str, Any]:
        """Engage or release the owner kill switch (new admissions only; the document is owner-signed)."""
        if not isinstance(body, dict) or set(body) != {'engaged'} or type(body['engaged']) is not bool:
            raise SwarmPublishError('invalid_request')
        with self._op:
            self._owner_only()
            swarm_id = self.state['swarm_id']
            document = docs.kill_switch(self.signer, swarm_id, engaged=body['engaged'],
                                        seq=self.ledger.kill_state(swarm_id)[1] + 1, at=self.now())
            try:
                self.gateway.guard.apply_kill(json.dumps(document).encode(), 'owner-app')
            except WireError as error:
                raise SwarmPublishError('swarm_' + error.code) from None
            return self.status()

    # -- periodic work -------------------------------------------------------------
    def tick(self) -> dict[str, Any]:
        with self._op:
            if not self.state.get('enabled'):
                return self.status()
            try:
                if self.state['mode'] == 'app':
                    self._refresh_app_origin()
                else:
                    self._refresh_link()
                self._heartbeat()
                self._error = None
            except SwarmPublishError as error:
                self._error = error.code
        return self.status()

    def _refresh_app_origin(self):
        origin = self._origin()
        record = self.state['record']
        if origin is None or record['entry']['origin'] == origin:
            self._set_policy(record)
            return
        fields = {k: record[k] for k in ('swarm_id', 'name', 'model', 'description', 'access_mode', 'open_quota')}
        record = docs.swarm_record(self.signer, seq=record['seq'] + 1,
                                   entry={'key': self.signer.public_key, 'origin': origin}, **fields)
        self._client().post('/records', record)
        self.state['record'] = record
        self._save()
        self._set_policy(record)

    def _refresh_link(self, *, force_offer: bool = False):
        swarm_id = self.state['swarm_id']
        value = self._client().get('/swarms/' + swarm_id)
        record = value.get('swarm', {}).get('record') if isinstance(value, dict) else None
        try:
            record = docs.check_record(record, verify, allow_loopback=True, allow_private=self._allow_private)
        except WireError:
            raise SwarmPublishError('swarm_record_invalid') from None
        if record['owner_key'] != self.state['owner_key'] or record['swarm_id'] != swarm_id:
            raise SwarmPublishError('swarm_record_owner_mismatch')  # the directory cannot swap owners
        self.state['record'] = record
        self._save()
        self._set_policy(record)
        origin = self._origin()
        attached = record['entry'] is not None and record['entry']['key'] == self.signer.public_key
        if (not attached or record['entry']['origin'] != origin) and origin is not None and \
                (force_offer or self.clock() - self._last_offer >= OFFER_SECONDS):
            self._client().post('/entry-offers', docs.entry_offer(self.signer, swarm_id, origin, at=self.now()))
            self._last_offer = self.clock()

    def _heartbeat(self, *, force: bool = False, status_override: str | None = None):
        if self.state.get('mode') == 'link' and self.policy() is None:
            return  # not attached yet: the directory would refuse this key
        if not force and self._last_heartbeat is not None and \
                self.clock() - self._last_heartbeat[0] < HEARTBEAT_SECONDS - 1:
            return
        status, members, model = run_state(self.app)
        if status_override is not None:
            status, members, model = status_override, 0, None
        for attempt in (0, 1):
            # strictly increasing ``at`` (the directory refuses a replayed/older beat);
            # after a restart the directory may hold a beat from this same second.
            at = max(self.now(), self._last_at + 1) + attempt
            try:
                self._client().post('/heartbeat', docs.heartbeat(self.signer, self.state['swarm_id'], status=status,
                                                                  members=members, served_model=model, at=at))
                break
            except SwarmPublishError as error:
                if attempt or error.code != 'swarm_directory_heartbeat_stale':
                    raise
        self._last_at = at
        self._last_heartbeat = (self.clock(), at, status)
        # A separate optional document: an old directory's 404 MUST NOT break liveness.
        # Numeric collection/validation failures likewise cannot turn a serving swarm offline.
        try:
            from mycelium_swarm_directory import stats_docs
            document = stats_docs.stats(self.signer, self.state['swarm_id'], at=at,
                                        values=self._stats.snapshot(self.app, status))
            stats_docs.check(document, verify)
            self._client().post('/stats', document)
        except Exception:
            pass

    def heartbeat_now(self) -> dict[str, Any]:
        """Send a heartbeat immediately (e.g. right after the run changes state)."""
        with self._op:
            if self.state.get('enabled'):
                try:
                    self._heartbeat(force=True)
                    self._error = None
                except SwarmPublishError as error:
                    self._error = error.code
        return self.status()

    def start(self, interval: float = HEARTBEAT_SECONDS) -> None:
        if self._thread is not None:
            return

        def loop():
            while not self._stop.wait(interval):
                try:
                    self.tick()
                except Exception:
                    self._error = 'swarm_publish_tick_failed'
        self._thread = threading.Thread(target=loop, name='a13-swarm-publish', daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
        self.ledger.close()

    def status(self) -> dict[str, Any]:
        state = self.state
        record = state.get('record') or {}
        entry = record.get('entry') or {}
        policy = self.policy()
        link = None
        if state.get('swarm_id') and state.get('owner_key') and state.get('directory'):
            link = docs.swarm_link(state['directory'], state['swarm_id'], state['owner_key'])
        return {'enabled': bool(state.get('enabled')), 'mode': state.get('mode'),
                'directory': state.get('directory'), 'swarm_id': state.get('swarm_id'),
                'name': record.get('name'), 'access_mode': record.get('access_mode'),
                'owner_fingerprint': fingerprint(state['owner_key']) if state.get('owner_key') else None,
                'entry_fingerprint': fingerprint(self.signer.public_key),
                'attached': policy is not None, 'entry_origin': entry.get('origin'),
                'heartbeat': None if self._last_heartbeat is None else self._last_heartbeat[2],
                'error_code': self._error, 'link': link,
                'killed': bool(state.get('swarm_id')) and self.ledger.kill_state(state['swarm_id'])[0]}


# -- app hooks ---------------------------------------------------------------------

def existing(app: Any) -> SwarmPublisher | None:
    return getattr(app, 'swarm_publisher', None)


_SERVICE_LOCK = threading.Lock()


def service(app: Any) -> SwarmPublisher:
    with _SERVICE_LOCK:
        publisher = existing(app)
        if publisher is None:
            publisher = SwarmPublisher(app)
            app.swarm_publisher = publisher
            publisher.start()
        return publisher


def resume(app: Any) -> None:
    """After an app restart: resume heartbeats for a swarm that was published."""
    if getattr(app, 'installation_role', None) == 'owner' and existing(app) is None and \
            (Path(app.root) / 'swarm-publish.json').is_file():
        # off the caller's thread: the first tick talks to the directory
        threading.Thread(target=lambda: service(app).tick(), name='a13-swarm-resume', daemon=True).start()


def action(app: Any, path: str, body: Any) -> dict[str, Any]:
    if getattr(app, 'installation_role', None) != 'owner':
        raise SwarmPublishError('owner_role_required')
    publisher = service(app)
    handler = {'/api/swarm/publish': publisher.publish, '/api/swarm/unpublish': publisher.unpublish,
               '/api/swarm/inbox': publisher.inbox, '/api/swarm/approve': publisher.approve,
               '/api/swarm/deny': publisher.deny, '/api/swarm/revoke': publisher.revoke,
               '/api/swarm/kill': publisher.kill}.get(path)
    if handler is None:
        raise SwarmPublishError('route_unknown')
    try:
        return handler(body)
    except SwarmPublishError as error:
        if error.code != 'swarm_relay_required':
            raise
        from .app import DemoActionError  # the page gets the code plus what to do
        raise DemoActionError(
            'swarm_relay_required',
            message='Publishing needs the public relay. Start it first, then publish again.',
            fix={'action': 'start_relay'}) from None


def status(app: Any) -> dict[str, Any] | None:
    if getattr(app, 'installation_role', None) != 'owner':
        return None
    publisher = existing(app)
    if publisher is None:
        return {'enabled': False, 'mode': None}
    return publisher.status()


def close(app: Any) -> None:
    publisher = existing(app)
    if publisher is not None:
        publisher.close()
