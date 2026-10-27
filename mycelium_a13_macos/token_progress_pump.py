# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded receipt bridge sharing the concurrent pump's durable signer.

Only a host-supplied pinned cursor can enable registration. Reader threads verify
native receipts, never perform HTTP or ACK. The pump persists each verified
receipt and exact owner ACK before native eviction, then drains before command
completion. Restart never dispatches inference again.
"""
import json
import queue
import secrets
import threading
import time

from .authority import _atomic_private_json, _load_private_json
from .control_pump import ControlPumpError
from .durable_requests import prepare
from .token_receipt_mailbox import PROGRESS_CANCELLED
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path_authority import ReceiptCursor
from mycelium_node.token_progress import ProgressCommandEnded, _snapshot

_PROTOCOL = 'mycelium.a13.token_progress_forwarder.v1'
# Three concurrent command claims, each with <=1 MiB receipt, plus ACK metadata.
# Do not widen the general authority-file or canonical-JSON structural bounds.
_MAX_STATE_BYTES = 4 * 1024 * 1024


class PumpTokenProgress:
    def __init__(self, pump, cursor_factory):
        if cursor_factory is not None and not callable(cursor_factory):
            raise ControlPumpError('token_progress_arguments_invalid')
        self.pump, self.factory = pump, cursor_factory
        self.path = pump.root/'token-progress.private.json'
        self._readers = {}
        self._stop = threading.Event()
        state = _load_private_json(self.path, max_bytes=_MAX_STATE_BYTES)
        if state is None:
            state = {'protocol': _PROTOCOL, 'rows': {}}
        if (not isinstance(state, dict) or set(state) != {'protocol', 'rows'}
                or state['protocol'] != _PROTOCOL or not isinstance(state['rows'], dict)
                or len(state['rows']) > 8):
            raise ControlPumpError('token_progress_state_invalid')
        for command_id, row in state['rows'].items():
            if isinstance(row, dict) and isinstance(row.get('pending'), str):
                try:
                    row['pending'] = json.loads(_snapshot(json.loads(row['pending'])))
                except (ValueError, TypeError, RuntimeError):
                    raise ControlPumpError('token_progress_state_invalid') from None
            if (not isinstance(command_id, str) or not isinstance(row, dict)
                    or set(row) != {'pending', 'owner_ack', 'terminal', 'error'}
                    or row['error'] not in {None, 'token_progress_read_failed', 'token_progress_command_ended',
                                            'token_progress_cancelled'}
                    or type(row['terminal']) is not bool
                    or (row['pending'] is not None and not isinstance(row['pending'], dict))
                    or (row['owner_ack'] is not None and not isinstance(row['owner_ack'], dict))):
                raise ControlPumpError('token_progress_state_invalid')
        self.state = state

    def save(self):
        # Store each independently bounded canonical receipt as text, preventing
        # three maximum token arrays from exhausting the aggregate JSON node cap.
        # Legacy dict-valued pending receipts remain readable on restart.
        state = dict(self.state, rows={key: dict(row, pending=(
            _snapshot(row['pending']).decode('utf-8') if row['pending'] is not None else None))
            for key, row in self.state['rows'].items()})
        _atomic_private_json(self.path, state, max_bytes=_MAX_STATE_BYTES)

    @property
    def pending_count(self):
        return sum(not r['terminal'] for r in self.state['rows'].values())

    def open(self, command):
        if self.factory is None:
            raise ControlPumpError('token_progress_disabled')
        command_id = command['command_id']
        if command_id in self.state['rows'] or len(self.state['rows']) >= 8:
            raise ControlPumpError('token_progress_stream_replayed')
        cursor = self.factory(json.loads(canonical_json_bytes(command)))
        if not isinstance(cursor, ReceiptCursor):
            raise ControlPumpError('token_progress_arguments_invalid')
        # Registration is independent, but it must also bind the precise command
        # about to cross the native boundary, not a different admitted request.
        if cursor.grant.digest != sha256_bytes(canonical_json_bytes(command['payload']['token_grant']['statement'])):
            raise ControlPumpError('token_progress_subject_mismatch')
        inbox = self.pump.worker.open_progress(command_id=command_id, cursor=cursor,
            deadline_monotonic_s=cursor.grant.deadline_monotonic_s)
        q = queue.Queue(maxsize=1)
        self.state['rows'][command_id] = {'pending': None, 'owner_ack': None, 'terminal': False, 'error': None}
        try:
            self.save()
        except BaseException:
            self.pump.worker.close_progress(command_id)
            raise
        row = self.state['rows'][command_id]
        def stopped():
            return self._stop.is_set() or row['error'] == 'token_progress_cancelled'
        def read():
            while not stopped():
                try:
                    item = inbox.read()
                except ProgressCommandEnded:
                    item = 'token_progress_command_ended'
                except BaseException:
                    item = 'token_progress_read_failed'
                while not stopped():
                    try:
                        q.put(item, timeout=.05)
                        self.pump.wake()
                        break
                    except queue.Full:
                        continue
                if isinstance(item, str) or item.status != 'DECODING':
                    return
        thread = threading.Thread(target=read, name='a13-token-progress', daemon=True)
        self._readers[command_id] = (q, thread)
        thread.start()

    def ready_to_ack(self, command_id):
        row = self.state['rows'].get(command_id)
        if row is not None and row['error'] == 'token_progress_cancelled':
            claim = self.pump._claims_state['claims'].get(command_id)
            return claim is not None and claim['phase'] in {'executed', 'reconciliation_required'}
        return row is None or row['terminal'] or (
            row['error'] == 'token_progress_command_ended' and self._native_failed(command_id))

    def _native_failed(self, command_id):
        claim = self.pump._claims_state['claims'].get(command_id)
        return (claim is not None and claim['phase'] == 'executed'
                and isinstance(claim['native_response'], dict)
                and claim['native_response'].get('ok') is False)

    def next(self, target):
        for command_id, row in self.state['rows'].items():
            if row['error'] == 'token_progress_cancelled':
                continue
            if row['error'] == 'token_progress_command_ended':
                # The typed EOF follows every verified prefix and native ACK.
                # Only a durable validated rejection may use the ordinary
                # failed-command channel. No receipt terminal is invented.
                claim = self.pump._claims_state['claims'].get(command_id)
                if self._native_failed(command_id):
                    continue
                if claim is not None and claim['phase'] == 'executed':
                    raise ControlPumpError('token_progress_terminal_missing')
                continue
            if row['error'] is not None:
                raise ControlPumpError(row['error'])
            if row['terminal']:
                continue
            if row['owner_ack'] is not None:
                try:
                    ack = row['owner_ack']['statement']
                    pending = row['pending']
                    matches = (ack['command_id'] == command_id
                        and ack['grant_digest'] == pending['statement']['grant_digest']
                        and type(ack['receipt_sequence']) is int
                        and ack['receipt_sequence'] == pending['statement']['sequence']
                        and ack['receipt_digest'] == pending['signature']['signed_statement_digest'])
                except (KeyError, TypeError):
                    matches = False
                if not matches:
                    raise ControlPumpError('token_progress_state_invalid')
                # No inference replay: a new private ACK command repeats only
                # idempotent receipt eviction. Its independent response wait is
                # bounded; a missing result leaves the exact signed ACK durable.
                try:
                    response = self.pump.worker.send_before(
                        command_id='token-ack-'+secrets.token_hex(12), command='ack_token_progress',
                        payload={'ack': row['owner_ack']}, deadline_monotonic_s=time.monotonic()+.5)
                except TimeoutError:
                    return {'state': 'token_progress_native_ack_retrying', 'command_id': command_id}
                if response.get('ok') is not True:
                    raise ControlPumpError('token_progress_native_ack_failed')
                terminal = row['pending']['statement']['status'] != 'DECODING'
                row.update(pending=None, owner_ack=None, terminal=terminal)
                self.save()
                if terminal:
                    self.pump.worker.close_progress(command_id)
                return {'state': 'token_progress_forwarded', 'command_id': command_id}
            if row['pending'] is None:
                reader = self._readers.get(command_id)
                if reader is None:
                    raise ControlPumpError('token_progress_reconciliation_required')
                try:
                    item = reader[0].get_nowait()
                except queue.Empty:
                    continue
                if isinstance(item, str):
                    row['error'] = item
                    self.save()
                    if item == 'token_progress_command_ended':
                        continue
                    raise ControlPumpError(item)
                row['pending'] = json.loads(item.envelope_bytes)
                self.save()  # Receipt durable BEFORE HTTP or native ACK.
            claim = self.pump._claims_state['claims'].get(command_id)
            if claim is None:
                raise ControlPumpError('token_progress_claim_stale')
            self.pump._state['outbound'] = prepare(self.pump.client, 'token_progress', target=target,
                run_id=self.pump.worker.run_id, deployment_id=self.pump.worker.deployment_id,
                command_id=command_id, claim_token=claim['reply']['claim_token'],
                receipt=row['pending'], **self.pump._next_request())
            self.pump._save()
            return self.pump._finish_outbound(target)
        return None

    def accepted(self, packet, owner_ack):
        command_id = packet['envelope']['statement']['command_id']
        row = self.state['rows'].get(command_id)
        if row is None or row['pending'] != packet['envelope']['statement']['receipt']:
            raise ControlPumpError('token_progress_state_invalid')
        if owner_ack['statement']['protocol'] == PROGRESS_CANCELLED:
            # The client verified the owner's signature, membership, request,
            # command, grant and exact receipt. This retires ONLY forwarding.
            # Retain the signed decision and prefix durably; never send it to
            # native receipt eviction or manufacture a terminal/cleanup result.
            row.update(owner_ack=owner_ack, error='token_progress_cancelled')
            self.save()
            self.pump.worker.close_progress(command_id)
            self.pump._state['outbound'] = None
            self.pump._save()
            return {'state': 'token_progress_cancelled', 'command_id': command_id}
        row['owner_ack'] = owner_ack
        self.save()  # Persist signed owner ACK before native eviction.
        self.pump._state['outbound'] = None
        self.pump._save()
        return {'state': 'token_progress_owner_acked', 'command_id': command_id}

    def retire(self, command_id):
        if command_id in self.state['rows']:
            self.pump.worker.close_progress(command_id)
            self.state['rows'].pop(command_id)
            self._readers.pop(command_id, None)
            self.save()

    def close(self):
        self._stop.set()
        for command_id in self._readers:
            self.pump.worker.close_progress(command_id)
        for _, thread in self._readers.values():
            thread.join(.2)
