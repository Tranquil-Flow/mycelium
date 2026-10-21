# SPDX-License-Identifier: AGPL-3.0-only
"""Application-owned adapter for the canonical node-session send/close API.

Owner intent and signer-global sequence are private and durable before HTTP.
An uncertain enqueue replays only its durably saved identical signed request.
This is not a stage producer, execution grant, or remote process-exit proof.
Use ONE owner journal/client for all target sessions of a signing identity.
"""
from __future__ import annotations

try:  # owner-side POSIX lock; guarded so shared workflow imports on Windows
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
from contextlib import contextmanager
import math
import os
import re
from pathlib import Path
import secrets
import stat
import threading
import time
from typing import Any, Callable, Mapping

from physical_inference_qualification import ControllerError
from .managed_classes import is_managed_peer_class
from .authority import _private_directory, _atomic_private_json, _load_private_json
from .managed_control import (
    ManagedControlClient, ManagedControlError, CONTROL_COMMAND_PROTOCOL,
    _binding_from_member, _binding_advanced, _member_live, _document_digest, validate_native_command, request_deadline,
)

from .durable_requests import prepare, replay

_V1_PROTOCOL = 'mycelium.a13.owner_control_journal.v1'
_V2_PROTOCOL = 'mycelium.a13.owner_control_journal.v2'
_PROTOCOL = 'mycelium.a13.owner_control_journal.v3'
_SEQUENCE_BLOCK = 64  # crash-safe reservation: sequences may be skipped, never reused



def _intent_expired(row, now):
    """True only when this unconfirmed intent's signed request is PROVABLY undeliverable.

    durable_requests.prepare builds the statement with `issued_at`/`expires_at`, and replay() verifies the
    envelope against the client clock -- which is why an expired request cannot be replayed. So expiry is
    an ABSOLUTE fact about the intent, not an inference: an expired signed request can never be delivered.

    Every uncertainty returns False -- a missing prepared request, a missing or malformed `expires_at`, a
    non-numeric clock -- because this predicate exists to make a PROVABLY dead intent retireable, NOT to
    weaken the fail-closed guard for a fresh or ambiguous one. Getting that wrong is how a real in-flight
    enqueue would be discarded silently.
    """
    try:
        statement = row['enqueue_request']['envelope']['statement']
        expires_at = statement['expires_at']
        return float(expires_at) <= float(now)
    except Exception:
        return False

def _remaining(deadline, interruption=None):
    if interruption is not None and interruption.is_set():
        raise ControllerError(interruption.code)
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ControllerError('managed_control_response_timeout')
    return remaining


@contextmanager
def _locked_before(lock, deadline, interruption=None):
    while not lock.acquire(timeout=min(.05, _remaining(deadline, interruption))):
        _remaining(deadline, interruption)
    try:
        _remaining(deadline, interruption)
        yield
    finally:
        lock.release()


class _CommandWaiter:
    def __init__(self):
        self.lock = threading.Lock()
        self.event = threading.Event()
        self.gate = threading.Lock()
        self.code = 'managed_control_command_interrupted'
        self.completed = False
        self.callbacks = set()

    def is_set(self):
        return self.event.is_set()

    def register(self, callback):
        with self.gate:
            cancelled = self.event.is_set()
            if not cancelled:
                self.callbacks.add(callback)
        if cancelled:
            try: callback()
            except Exception: pass
        def unregister():
            with self.gate:
                self.callbacks.discard(callback)
        return unregister

    def interrupt(self, code):
        with self.gate:
            if self.completed or self.event.is_set():
                return
            self.code = code
            self.event.set()
            callbacks = tuple(self.callbacks)
        for callback in callbacks:
            try: callback()
            except Exception: pass

    def finish(self):
        with self.gate:
            if self.event.is_set():
                raise ControllerError(self.code)
            self.completed = True


MAX_SESSION_TIMEOUT_SECONDS = 240.0
MAX_OWNER_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_OWNER_COMMANDS = 1024
# Confirmed command rows retained once the ledger is full, so an exact-duplicate
# retry of a recently retired command is still detected.  Everything older is
# dropped.  Unresolved rows are never dropped.
_COMMAND_LEDGER_TAIL = 256


class ManagedControlOwner:
    """One serialized signing channel; concurrent sessions share its sequence."""

    def __init__(self, state_root: Path, *, client: ManagedControlClient,
                 current_member: Callable[[str], Mapping[str, Any]],
                 max_commands: int = MAX_OWNER_COMMANDS, max_requests: int = 4096):
        if (not callable(current_member) or type(max_commands) is not int
                or not 2 <= max_commands <= MAX_OWNER_COMMANDS or type(max_requests) is not int
                or not max_commands <= max_requests <= 10000):
            raise ControllerError('managed_owner_configuration_invalid')
        self.client = client
        self.current_member = current_member
        self.max_commands = max_commands
        self.max_requests = max_requests
        self._lock = threading.RLock()
        # Guards the auto-reconciliation pass in _exchange so a nested reconcile does
        # not start its own pass (mutual recursion between unresolved rows).
        self._reconciling = False
        self._closed = threading.Event()
        self._broken = False
        self._fd = -1
        root = _private_directory(Path(state_root), create=True)
        self.path = root/'owner.json'
        authority = {'swarm_id': client.swarm_id, 'owner_node_id': client.owner_node_id,
                     'key_digest': client.signer.verification_key_digest}
        if authority['key_digest'] != client.trusted_owner_key_record['verification_key_digest']:
            raise ControllerError('managed_owner_signer_invalid')
        self._win32_lock = None
        try:
            import mycelium_host_platform
            if mycelium_host_platform.is_win32():
                try:
                    self._win32_lock = mycelium_host_platform.hold_exclusive_lock(root, 'owner.lock', blocking=False)
                except mycelium_host_platform.HostPlatformError as exc:
                    if exc.code == 'host_lock_busy':
                        raise ControllerError('managed_owner_already_running') from None
                    raise ControllerError('managed_owner_state_invalid') from None
            else:
                self._fd = os.open(root/'owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                info = os.fstat(self._fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600):
                    raise ControllerError('managed_owner_state_invalid')
                try:
                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise ControllerError('managed_owner_already_running') from None
            value = _load_private_json(self.path, max_bytes=MAX_OWNER_JOURNAL_BYTES)
            if value is None:
                value = {'protocol': _PROTOCOL, 'authority': authority, 'sequence': 0,
                    'commands': {}, 'sequence_ceiling': _SEQUENCE_BLOCK, 'write_intents': 0}
                _atomic_private_json(self.path, value, max_bytes=MAX_OWNER_JOURNAL_BYTES)
            legacy = value.get('protocol') == _V1_PROTOCOL
            if legacy and isinstance(value.get('commands'), dict):
                # Old digests cannot establish whether submission happened. Preserve
                # query-only reconciliation; never invent an old signed request.
                for row in value['commands'].values():
                    if not isinstance(row, dict) or set(row) != {'binding', 'digest'}:
                        raise ControllerError('managed_owner_state_invalid')
                    row.update(enqueue_request=None, enqueue_confirmed=True)
                value['protocol'] = _V2_PROTOCOL
            migrated = False
            if value.get('protocol') == _V2_PROTOCOL:
                # v2 burned one durable sequence per result poll; its counter is a
                # conservative upper bound of every write intent ever persisted. Carry
                # the old refusal boundary exactly: previously-refused stays refused,
                # live journals keep the same remaining budget. No reset, no reuse.
                value['write_intents'] = value['sequence']
                value['sequence_ceiling'] = value['sequence'] + _SEQUENCE_BLOCK
                value['protocol'] = _PROTOCOL
                migrated = True
            if (set(value) != {'protocol', 'authority', 'sequence', 'commands',
                    'sequence_ceiling', 'write_intents'}
                    or value['protocol'] != _PROTOCOL or value['authority'] != authority
                    or type(value['sequence']) is not int or value['sequence'] < 0
                    or type(value['sequence_ceiling']) is not int
                    or not value['sequence'] < value['sequence_ceiling']
                    or type(value['write_intents']) is not int or value['write_intents'] < 0
                    or not isinstance(value['commands'], dict) or len(value['commands']) > MAX_OWNER_COMMANDS):
                raise ControllerError('managed_owner_state_invalid')
            for row in value['commands'].values():
                if (not isinstance(row, dict)
                        or set(row) != {'binding', 'digest', 'enqueue_request', 'enqueue_confirmed'}
                        or type(row.get('enqueue_confirmed')) is not bool
                        or (row.get('enqueue_request') is not None and not isinstance(row['enqueue_request'], dict))
                        or not isinstance(row['binding'], dict) or not isinstance(row['digest'], str)):
                    raise ControllerError('managed_owner_state_invalid')
            self._state = value
            # Every issued sequence is strictly below the ceiling in force at issue
            # time (ceilings only grow and are persisted before use); therefore
            # ceiling-1 >= every previously issued sequence: no reuse after restart.
            self._volatile = value['sequence_ceiling'] - 1
            if legacy or migrated:
                self._persist()
        except BaseException:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1
            if self._win32_lock is not None:
                self._win32_lock.release()
                self._win32_lock = None
            raise

    def _save(self):
        _atomic_private_json(self.path, self._state, max_bytes=MAX_OWNER_JOURNAL_BYTES)

    def _persist(self):
        try:
            self._save()
        except Exception:
            self._broken = True
            raise ControllerError('managed_owner_state_unavailable') from None

    def _prune_retired_commands(self, *, keep):
        """Drop long-retired commands so the durable ledger stays bounded.

        ``commands`` only ever grew: every confirmed row stayed forever.  Because
        the ledger lives at a fixed path (``<state_root>/owner/owner.json``), not a
        per-run one, the rows accumulated *across* runs until the cap refused all
        new work.  Observed: ``len(commands) == 1024`` (== ``MAX_OWNER_COMMANDS``),
        all 1024 ``enqueue_confirmed`` and **zero** unresolved, after which every
        new managed command failed with ``managed_owner_command_limit_exceeded``
        on the first observe of the next run.

        Safety: rows that are not yet ``enqueue_confirmed`` are **never** dropped --
        the reconciliation guard above depends on them, and there are none in the
        observed state.  A bounded tail of recently confirmed rows is retained so an
        exact-duplicate retry of a recent command is still detected.  The signer's
        ``sequence``/``sequence_ceiling`` high-water is deliberately untouched, so no
        sequence can ever be reused regardless of what is pruned.
        """

        commands = self._state['commands']
        if len(commands) <= _COMMAND_LEDGER_TAIL:
            return
        unresolved = [key for key, row in commands.items() if not row['enqueue_confirmed']]
        if unresolved:
            # An unknown enqueue means the remote high-water is uncertain; the caller
            # already fails closed on this, so do not prune anything alongside it.
            return
        excess = len(commands) - (_COMMAND_LEDGER_TAIL + 1)  # +1 reserves the new row
        if excess <= 0:
            return
        retired = [key for key in commands if key != keep]
        dropped = 0
        for key in retired[:excess]:
            del commands[key]
            dropped += 1
        if dropped:
            # Retained durable write intents must track retained rows, otherwise the
            # request limit becomes the next accumulation wall (it was already at
            # 1024/4096 after two runs).
            self._state['write_intents'] = max(0, self._state['write_intents'] - dropped)

    def _guard(self):
        if self._closed.is_set() or self._broken:
            raise ControllerError('managed_owner_closed')

    def _next_sequence(self):
        # Caller holds _lock. Monotonic for the journal's whole lifetime: blocks
        # are durably reserved before use, so a crash may skip the unused tail of
        # a block but can never reissue an already-used sequence.
        self._volatile += 1
        if self._volatile >= self._state['sequence_ceiling']:
            self._state['sequence'] = self._volatile
            self._state['sequence_ceiling'] = self._volatile + _SEQUENCE_BLOCK
            self._persist()  # reserve the new block before the sequence is used
        return self._volatile

    def _request(self, method, target, command, *, deadline, interruption=None):
        # Caller holds _lock through sequence allocation AND network issuance.
        # Read-only result observation: consumes no durable write budget, so
        # ordinary result waiting can never exhaust the per-lifetime quota.
        self._guard()
        _remaining(deadline, interruption)
        sequence = self._next_sequence()
        _remaining(deadline, interruption)
        with request_deadline(deadline, interruption=interruption):
            return method(target=target, native_command=command,
                sequence=sequence, nonce=secrets.token_hex(16),
                request_id=secrets.token_hex(16))

    def _target(self, node_id, run_id, deployment_id):
        try:
            member = self.current_member(node_id)
            binding = _binding_from_member(member, run_id, deployment_id)
            lease = member.get('lease_expires_at')
            if (binding['target_node_id'] != node_id or not is_managed_peer_class(member.get('peer_class'))
                    or isinstance(lease, bool) or not isinstance(lease, (int, float))
                    or not math.isfinite(lease) or time.time() >= lease):
                raise ValueError()
            return member, binding
        except Exception:
            raise ControllerError('managed_control_member_stale') from None

    def session(self, *, node_id: str, run_id: str, deployment_id: str,
                timeout_seconds: float = 30.0):
        with self._lock:
            self._guard()
            return ManagedNodeSession(self, node_id=node_id, run_id=run_id,
                deployment_id=deployment_id, timeout_seconds=timeout_seconds)

    def reconcile(self, command_id, *, timeout_seconds=30.0):
        """Resume one persisted intent; never invent another semantic command ID.

        A previously confirmed enqueue only queries native output. An unresolved
        enqueue can replay its identical still-fresh signed request, or resolve an
        expired one through one fresh read-only result lookup. Legacy v1 digests
        lack the command body and require caller-owned reconciliation.
        """
        with self._lock:
            self._guard()
            if not isinstance(command_id, str):
                raise ControllerError('managed_control_command_unknown')
            row = self._state['commands'].get(command_id)
            if row is None:
                raise ControllerError('managed_control_command_unknown')
            prepared = row['enqueue_request']
            if prepared is None:
                raise ControllerError('managed_control_legacy_reconciliation_required')
            command = prepared['envelope']['statement']['native_command']
            restored = self.session(node_id=row['binding']['target_node_id'],
                run_id=command['run_id'], deployment_id=command['deployment_id'],
                timeout_seconds=timeout_seconds)
        return restored.send(command_id=command_id, command=command['command'], payload=command['payload'])

    def session_factory(self, *, argv, node_id, run_id, deployment_id, timeout_seconds):
        """Canonical controller factory; managed peers carry no launch argv."""
        if argv != ():
            raise ControllerError('managed_session_launch_argv_forbidden')
        return self.session(node_id=node_id, run_id=run_id, deployment_id=deployment_id,
            timeout_seconds=timeout_seconds)

    def _exchange(self, session, command, *, deadline, interruption=None):
        with _locked_before(self._lock, deadline, interruption):
            self._guard()
            target, binding = self._target(session.node_id, session.run_id, session.deployment_id)
            if binding != session.binding:
                # Relay rotation re-admits the SAME member with a newer incarnation, and that
                # is not staleness.  The signed intent below is built from the FRESHLY resolved
                # binding, never from session.binding, so adopting it moves no signed byte --
                # while refusing it failed the command outright (round 64r1).  A REVOCATION
                # also advances the generation, so it is excluded by requiring the member to be
                # live: revocation is an advance to STOPPED.
                if not (_binding_advanced(session.binding, binding) and _member_live(target)):
                    raise ControllerError('managed_control_member_stale')
                session.binding = binding
            _remaining(deadline, interruption)
            intent = {'binding': binding, 'digest': _document_digest(command)}
            previous = self._state['commands'].get(command['command_id'])
            if previous is not None and any(previous[field] != intent[field] for field in intent):
                raise ControllerError('managed_control_conflicting_retry')
            unresolved = [key for key, row in self._state['commands'].items()
                if not row['enqueue_confirmed'] and key != command['command_id']]
            if unresolved and not self._reconciling:
                # Resume each persisted intent instead of refusing forever.  Without
                # this the owner wedged permanently: ONE enqueue that was prepared but
                # never confirmed -- e.g. the app was killed mid-enqueue -- made every
                # later command raise managed_control_reconciliation_required, and
                # reconcile(), the function written for exactly this case, had NO
                # callers.  reconcile() replays the IDENTICAL still-fresh signed
                # request, or resolves an expired one with one read-only result
                # lookup, so the signer's sequence never advances past an unknown
                # enqueue and no new semantic command id is invented.
                # The flag stops mutual recursion between reconciling rows: a nested
                # _exchange must not start its own reconciliation pass.
                self._reconciling = True
                try:
                    for key in list(unresolved):
                        try:
                            self.reconcile(key, timeout_seconds=min(30.0, _remaining(deadline, interruption)))
                        except ControllerError:
                            pass                      # re-checked below; never mask the guard
                finally:
                    self._reconciling = False
                unresolved = [key for key, row in self._state['commands'].items()
                    if not row['enqueue_confirmed'] and key != command['command_id']]
            if unresolved:
                # An unconfirmed intent belonging to a DIFFERENT (finished) run can
                # never be delivered usefully, so it must not block forever.  Measured
                # trigger: ONE unconfirmed `health` intent from a finished run -- the
                # app was killed mid-enqueue -- made every later command raise this
                # guard, permanently wedging the owner.
                #
                # Retiring it cannot reuse a sequence: the ceiling lives outside this
                # map, is already ahead of `sequence`, and no new command is assigned
                # the retired intent's sequence.  The invariant the guard protects --
                # never advance the high-water past an unknown enqueue -- is untouched.
                #
                # An unconfirmed intent for the CURRENT run still blocks: that is the
                # fail-closed case this guard exists for.
                # AND a third case, which the two original ones could not cover: an intent that is
                # PROVABLY DEAD. The prepared statement carries an ABSOLUTE `expires_at`
                # (durable_requests.prepare builds the statement with issued_at/expires_at, and replay()
                # verifies the envelope against the client clock -- which is exactly why an expired
                # request cannot be replayed). An expired signed request CAN NEVER BE DELIVERED: the TTL
                # is the TTL. Failing closed on it therefore protects nothing, while wedging the owner
                # forever -- the measured consequence when a SIGKILL (packaged_app_daemon.py:21 kills the
                # app after a 15 s grace, exit_code -9) interrupts an enqueue and the stale intent
                # matches the CURRENT run/deployment because the pilot reuses node and deployment
                # identity across rounds. Same argument as the different-run case below: retiring cannot
                # reuse a sequence, because the ceiling lives outside this map and is already ahead.
                # The FRESH current-run case still blocks -- that is the fail-closed case this guard
                # exists for, and _intent_expired returns False whenever it cannot prove expiry.
                retired = [key for key in unresolved
                    if self._state['commands'][key]['binding'].get('run_id') != session.run_id
                    or self._state['commands'][key]['binding'].get('deployment_id') != session.deployment_id
                    or _intent_expired(self._state['commands'][key], self.client._clock())]
                if retired:
                    for key in retired:
                        self._state['commands'].pop(key, None)
                    try:
                        _atomic_private_json(self.path.parent / 'retired-intents.private.json', {
                            'protocol': 'mycelium.a13.retired_intents.v1',
                            'current_run_id': session.run_id,
                            'current_deployment_id': session.deployment_id,
                            'retired': [key[:32] for key in retired],
                            'retired_count': len(retired),
                            'sequence': self._state.get('sequence'),
                            'sequence_ceiling': self._state.get('sequence_ceiling'),
                        })
                    except BaseException:
                        pass
                    self._persist()
                    unresolved = [key for key in unresolved if key not in retired]
            if unresolved:
                # Do not advance the signer's remote high-water past an unknown enqueue.
                raise ControllerError('managed_control_reconciliation_required')
            if previous is None:
                self._prune_retired_commands(keep=command['command_id'])
                maximum = self.max_commands if command['command'] == 'stop' else self.max_commands - 1
                if len(self._state['commands']) >= maximum:
                    raise ControllerError('managed_owner_command_limit_exceeded')
                if self._state['write_intents'] >= self.max_requests:
                    # max_requests bounds durably persisted write intents only; it can
                    # no longer be exhausted by ordinary read-only result waiting.
                    raise ControllerError('managed_owner_request_limit_exceeded')
                sequence = self._next_sequence()
                prepared = prepare(self.client, 'enqueue', target=target, native_command=command,
                    sequence=sequence, nonce=secrets.token_hex(16),
                    request_id=secrets.token_hex(16), ttl_seconds=min(60.0, _remaining(deadline)))
                previous = {**intent, 'enqueue_request': prepared, 'enqueue_confirmed': False}
                self._state['commands'][command['command_id']] = previous
                self._state['write_intents'] += 1
                self._persist()  # Exact signed bytes and sequence are durable BEFORE HTTP.
            if not previous['enqueue_confirmed']:
                _remaining(deadline, interruption)
                try:
                    with request_deadline(deadline, interruption=interruption):
                        replay(self.client, previous['enqueue_request'], target=target)
                except ManagedControlError as error:
                    if error.code != 'managed_control_message_expired' or interruption is not None and interruption.is_set():
                        raise ControllerError('managed_control_reconciliation_required') from None
                    # A fresh authenticated result can establish that this exact
                    # command was recorded. Absence is NOT proof of non-execution:
                    # a restored/recreated mailbox may have lost its history.
                    try:
                        fresh = self._request(self.client.result, target, command,
                            deadline=deadline, interruption=interruption)
                    except ControllerError:
                        raise
                    except Exception:
                        raise ControllerError('managed_control_reconciliation_required') from None
                    if not isinstance(fresh,dict) or fresh.get('state') not in {
                        'pending','claimed','completed','failed','expired','reconciliation_required'}:
                        raise ControllerError('managed_control_reconciliation_required')
                except Exception:
                    raise ControllerError('managed_control_reconciliation_required') from None
                previous['enqueue_confirmed'] = True
                self._persist()
        while not self._closed.is_set():
            _remaining(deadline, interruption)
            if time.monotonic() >= deadline:
                raise ControllerError('managed_control_response_timeout')
            with _locked_before(self._lock, deadline, interruption):
                self._guard()
                target, binding = self._target(session.node_id, session.run_id, session.deployment_id)
                if binding != session.binding:
                    # A re-admission of the same live member is adopted, never refused: the
                    # result lookup would otherwise be abandoned mid-wait on every rotation.
                    # A stopped member is a revocation and still fails closed.
                    if not (_binding_advanced(session.binding, binding) and _member_live(target)):
                        raise ControllerError('managed_control_member_stale')
                    session.binding = binding
                try:
                    result = self._request(self.client.result, target, command, deadline=deadline, interruption=interruption)
                except ManagedControlError as error:
                    raise ControllerError(error.code) from None
                except ControllerError:
                    raise
                except Exception:
                    raise ControllerError('managed_control_reconciliation_required') from None
            if time.monotonic() >= deadline:
                raise ControllerError('managed_control_response_timeout')
            if result['state'] in {'completed', 'failed'}:
                # The HTTP receipt can race a local authority revocation.
                with _locked_before(self._lock, deadline, interruption):
                    self._guard()
                    member, current = self._target(session.node_id, session.run_id, session.deployment_id)
                    if current != session.binding:
                        # Still fail closed on a revocation or a different member -- this check
                        # exists because the HTTP receipt can race a local revocation -- but a
                        # live re-admission is adopted so the receipt is not discarded on
                        # rotation.  Both are advances, so liveness is what separates them.
                        if not (_binding_advanced(session.binding, current) and _member_live(member)):
                            raise ControllerError('managed_control_member_stale')
                        session.binding = current
                    _remaining(deadline, interruption)
                return result['native_response']
            if result['state'] in {'expired', 'reconciliation_required'}:
                raise ControllerError('managed_control_' + result['state'])
            self._closed.wait(min(.05, max(0.0, deadline-time.monotonic())))
        raise ControllerError('managed_owner_closed')

    def close(self):
        # Cancels local waits, not the remote process. Session.close sends stop.
        self._closed.set()
        with self._lock:
            if self._fd >= 0:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
                self._fd = -1
            if getattr(self, '_win32_lock', None) is not None:
                self._win32_lock.release()
                self._win32_lock = None


class ManagedNodeSession:
    #: Lifetime of an idle liveness probe to a peer reached through the mailbox.  The route's own probe budget (0.5 s)
    #: is shorter than one claim + execute + result round trip over a relay, so every probe expired and a healthy
    #: remote peer was quarantined (V1.1 h/j).  Active-request liveness is unaffected.
    idle_probe_floor_seconds = 4.0

    def __init__(self, owner, *, node_id, run_id, deployment_id, timeout_seconds):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= MAX_SESSION_TIMEOUT_SECONDS):
            raise ControllerError('node_process_arguments_invalid')
        self.owner = owner
        self.node_id, self.run_id, self.deployment_id = node_id, run_id, deployment_id
        _, self.binding = owner._target(node_id, run_id, deployment_id)
        self.timeout_seconds = float(timeout_seconds)
        self._lock = threading.Lock()
        self._waiters = {}
        self._closed = False
        self._closing = False
        self._close_lock = threading.Lock()
        self.stop_command_id = None
        self.stop_response = None
        self.cleanup_error = None

    @property
    def returncode(self):
        # Compatibility projection for the logical session, not a Unix exit
        # observation. Only a confirmed native stop closes this handle.
        return 0 if self._closed and self.stop_response is not None and self.stop_response['ok'] else None

    @property
    def stderr(self):
        return b''  # Private target diagnostics never cross this interface.

    def send(self, *, command_id: str, command: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return self._send(command_id=command_id, command=command, payload=payload,
            deadline=time.monotonic() + self.timeout_seconds)

    def send_before(self, *, command_id, command, payload, deadline_monotonic_s):
        if (isinstance(deadline_monotonic_s, bool) or not isinstance(deadline_monotonic_s, (int, float))
                or not math.isfinite(deadline_monotonic_s)):
            raise ControllerError('node_process_arguments_invalid')
        try:
            return self._send(command_id=command_id, command=command, payload=payload,
                deadline=deadline_monotonic_s)
        except ControllerError as error:
            if error.code == 'managed_control_response_timeout':
                raise TimeoutError('command_deadline_exceeded') from None
            raise

    def interrupt_command(self, command_id, code):
        if (not isinstance(command_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', command_id)
                or not isinstance(code, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code)):
            raise ControllerError('managed_control_interrupt_invalid')
        with self._lock:
            if self._closed:
                return
            if command_id not in self._waiters and len(self._waiters) >= self.owner.max_commands:
                raise ControllerError('managed_owner_command_limit_exceeded')
            waiter = self._waiters.setdefault(command_id, _CommandWaiter())
        waiter.interrupt(code) # Only the local waiter; not a remote cancellation receipt.

    def _send(self, *, command_id, command, payload, deadline):
        _remaining(deadline)
        try:
            envelope = validate_native_command({'protocol': CONTROL_COMMAND_PROTOCOL,
                'command_id': command_id, 'run_id': self.run_id, 'deployment_id': self.deployment_id,
                'command': command, 'payload': payload})
        except ManagedControlError as error:
            raise ControllerError(error.code) from None
        with _locked_before(self._lock, deadline):
            if self._closed or (self._closing and command_id != self.stop_command_id):
                raise ControllerError('managed_session_closed')
            if command_id not in self._waiters and len(self._waiters) >= self.owner.max_commands:
                raise ControllerError('managed_owner_command_limit_exceeded')
            waiter = self._waiters.setdefault(command_id, _CommandWaiter())
            if command == 'stop':
                self._closing = True
                self.stop_command_id = command_id
            retiring = [value for key, value in self._waiters.items() if key != command_id] if command == 'stop' else []
        for previous in retiring:
            previous.interrupt('managed_session_closed')
        try:
            # Only duplicate callers for THIS command serialize, not the node.
            with _locked_before(waiter.lock, deadline, waiter):
                response = self.owner._exchange(self, envelope, deadline=deadline, interruption=waiter)
                waiter.finish() # Linearizes publication against interruption.
                if command == 'stop':
                    with self._lock:
                        self.stop_response = response
                return response
        except Exception:
            if waiter.is_set():
                raise ControllerError(waiter.code) from None
            raise

    def close(self):
        with self._close_lock:
            if self._closed:
                return
            self._closing = True
            if self.stop_command_id is None:
                self.stop_command_id = 'managed-stop-' + secrets.token_hex(16)
            try:
                if self.stop_response is None:
                    # send durably saves this ID and exact signed intent before HTTP.
                    self.send(command_id=self.stop_command_id, command='stop', payload={})
            except ControllerError as error:
                self.cleanup_error = str(error)
                return  # Keep the same ID for a later close/reconcile, not a new stop.
            self.cleanup_error = None if self.stop_response['ok'] else 'managed_native_stop_rejected'
            self._closed = True  # Local handle closure, not fabricated remote exit evidence.
