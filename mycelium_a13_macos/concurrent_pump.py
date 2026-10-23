# SPDX-License-Identifier: AGPL-3.0-only
"""Bounded concurrent target dispatch with one durable signing/HTTP channel.

The HTTP step never waits for a native command result. One work and two control
claims may be active; native results are saved before their exact signed ack.
No claim is executed again after an unknown process/crash outcome. This owns the
supplied worker on close; it does not establish model grants or route readiness.
"""
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping
import re

from .control_pump import TargetControlPump, ControlPumpError, _OUTBOUND_EXPIRED, _outbound_outcome
from .authority import _atomic_private_json, _load_private_json
from .durable_requests import prepare
from .managed_control import OWNER_COMMAND_PROTOCOL, ManagedControlError, _verify_signed, _binding_from_member, _document_digest, _binding_advanced

_PROTOCOL = 'mycelium.a13.concurrent_claims.v1'


def _install_diagnostic(row):
    from .managed_control import _TARGET_ERROR_CODES
    if not isinstance(row, dict) or row.get('phase') != 'reconciliation_required':
        return {}
    envelope = row.get('reply', {}).get('command_envelope') or {}
    command = envelope.get('statement', {}).get('native_command', {})
    code = row.get('error_code')
    if command.get('command') == 'install_token_path' and isinstance(code, str) and code in _TARGET_ERROR_CODES:
        return {'target_error_code': code}
    return {}

_CLAIM_PHASES = frozenset({'ready', 'executing', 'executed', 'reconciliation_required', 'reconciliation_acked'})

_UNRESOLVED_PHASES = frozenset({'executing', 'reconciliation_required'})
"""Phases that must block new dispatch, because the target still owes the owner an ack.

`reconciliation_acked` is deliberately NOT here.  It means "the owner has been told
this claim's outcome is uncertain" -- the obligation is discharged, so it must not
block forever.  Including it wedged the pump permanently: the row is written at
`_finish_outbound` and never removed, so every later `_dispatch_ready()` saw an
unresolved phase with no future and raised `control_reconciliation_required`, which
`managed_runtime._serve` turns into a permanent exit (round41: 5 claimed of 73, then
silence).  It is retained in `_CLAIM_PHASES` so existing on-disk rows still validate.
"""

_UNCERTAIN_CLAIM_DIAGNOSTIC = 'uncertain-claim.private.json'
"""Written when the fail-closed uncertainty guard fires. Diagnostics ONLY.

The guard raises ``control_reconciliation_required`` and that is correct: an unknown
native outcome must never be silently re-run.  But the loop then exits permanently
and the peer can never recover, and until now nothing recorded *which* claim caused
it or *when* that claim was created.

That distinction is what picks the fix: a row left unresolved by a PREVIOUS process
(safe to retire at startup, because its future cannot exist in this one) is a
different problem from a row that went unresolved DURING the current run (needs an
explicit owner-authorised resolution).  Recording it here does not change the raise,
the disposition, or any control flow.
"""


class ConcurrentTargetControlPump(TargetControlPump):
    _concurrent = True
    # One outbound coalesced receipt (<=1 MiB), its signed envelope and metadata.
    # Serial pump and all other private-state defaults remain unchanged.
    _state_max_bytes = 2 * 1024 * 1024

    def __init__(self, *args, token_progress_cursor=None, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            if self._state['pending'] is not None:
                raise ControlPumpError('serial_pending_requires_reconciliation')
            self._claims_path = self.root/'concurrent.json'
            value = _load_private_json(self._claims_path)
            if value is None:
                if self._state['outbound'] is not None:
                    raise ControlPumpError('serial_pending_requires_reconciliation')
                value = {'protocol': _PROTOCOL, 'binding': self._binding, 'claims': {}}
                _atomic_private_json(self._claims_path, value)
            if (set(value) != {'protocol', 'binding', 'claims'} or value['protocol'] != _PROTOCOL
                    or not isinstance(value['claims'], dict)
                    or len(value['claims']) > 3):
                raise ControlPumpError('concurrent_state_invalid')
            if value['binding'] != self._binding:
                # An older incarnation of the SAME member is a re-admission, not an
                # invalid state: rebind rather than refuse, or relay rotation ends the
                # run at the next restart (round 64r1).  A different member still fails.
                if not _binding_advanced(value['binding'], self._binding):
                    raise ControlPumpError('concurrent_state_invalid')
                value['binding'] = self._binding
                _atomic_private_json(self._claims_path, value)
            for key, row in value['claims'].items():
                if (not isinstance(row, dict) or set(row) != {'phase', 'reply', 'native_response', 'error_code'}
                        or row['phase'] not in _CLAIM_PHASES
                        or not isinstance(row['reply'], dict) or row['reply'].get('command_id') != key):
                    raise ControlPumpError('concurrent_state_invalid')
            self._claims_state = value
            self._futures = {}
            # Unresolved rows present at construction can only belong to a PREVIOUS
            # process -- this one has no futures yet.  Recorded so a later guard
            # firing can be attributed to a restart wedge or an in-run wedge.
            self._startup_unresolved = sorted(
                key for key, row in value['claims'].items() if row['phase'] in _UNRESOLVED_PHASES
            )
            from .token_progress_pump import PumpTokenProgress
            self._token_progress = PumpTokenProgress(self, token_progress_cursor)
            self._progress_yield = False
            self._executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix='a13-native-dispatch')
        except BaseException:
            super().close()
            raise

    def _save_claims(self):
        _atomic_private_json(self._claims_path, self._claims_state)

    def _collect(self):
        for command_id, future in list(self._futures.items()):
            if not future.done():
                continue
            row = self._claims_state['claims'][command_id]
            try:
                native = future.result()
                if not isinstance(native, Mapping):
                    raise ValueError('worker_command_failed')
                row.update(phase='executed', native_response=dict(native), error_code=None)
            except Exception as error:
                code = str(error)
                if not re.fullmatch(r'[a-z][a-z0-9_]{0,79}', code):
                    code = 'worker_command_failed'
                row.update(phase='reconciliation_required', native_response=None, error_code=code)
            self._save_claims() # Never prepare an ack before the actual result is durable.
            del self._futures[command_id]

    def _record_uncertain_ack(self, command_id, code, remaining):
        """Keep the peer's own reason for an honest "unknown" before the row is retired.

        Measured 2026-09-19 (round p76r1, preview.76): the peer claimed `configure`, could not
        produce a native response, and acked disposition='reconciliation_required' -- honest and
        correct.  But the ack carries no reason (_ACK_FIELDS is command_id, claim_token,
        disposition, native_response) and the row is popped right after the ack is accepted, so the
        owner learned only "unknown" while the peer kept nothing: the round could not name its own
        cause and had to be reconstructed from a timestamp triangle across two machines.  This keeps
        a bounded local record of the reason.  Diagnostic only -- it never gates control flow -- and
        allowlisted like every other private record here: a truncated command id, one reason code,
        a count.  No payloads, no claim token, no key material, no raw stderr.
        """
        try:
            _atomic_private_json(self.root/'uncertain-ack.private.json',{
                'protocol':'mycelium.a13.uncertain_ack.v1',
                'run_id':getattr(getattr(self,'worker',None),'run_id',None),
                'command_id_prefix':str(command_id)[:16],
                'error_code':code if code is None or isinstance(code,str) else str(code),
                'remaining_claims':int(remaining),
            })
        except BaseException:
            pass

    def _record_uncertain_claim(self, unresolved):
        """Record which claim tripped the uncertainty guard, and its provenance.

        Diagnostics ONLY: never changes the raise or any disposition.  Bounded and
        allowlisted -- claim ids are truncated and no reply payload, claim token or
        key material is written, because this private state contains capabilities.
        """
        try:
            phases = {}
            for phase in unresolved.values():
                phases[phase] = phases.get(phase, 0) + 1
            previous = sorted(set(unresolved) & set(self._startup_unresolved))
            _atomic_private_json(self.root / _UNCERTAIN_CLAIM_DIAGNOSTIC, {
                'protocol': 'mycelium.a13.uncertain_claim.v1',
                'run_id': self.worker.run_id,
                'unresolved_count': len(unresolved),
                'phases': phases,
                'in_flight_futures': len(self._futures),
                'from_previous_process': [key[:16] for key in previous],
                'from_previous_process_count': len(previous),
                'in_run_count': len(unresolved) - len(previous),
                'startup_unresolved_count': len(self._startup_unresolved),
            })
        except BaseException:
            # Diagnostics must never perturb control flow, but swallowing the error
            # made "could not write" indistinguishable from "never fired" -- and a
            # wrong root cause was drawn from exactly that ambiguity.  Record the
            # write failure itself, and fall back to stderr, so the ABSENCE of the
            # diagnostic can never again be read as the guard not having fired.
            try:
                (self.root / (_UNCERTAIN_CLAIM_DIAGNOSTIC + '.failed')).write_text(
                    'diagnostic_write_failed\n', encoding='utf-8')
            except BaseException:
                try:
                    import sys
                    print('MYCELIUM-UNCERTAIN-CLAIM-DIAGNOSTIC-WRITE-FAILED', file=sys.stderr)
                except BaseException:
                    pass

    def _dispatch_ready(self):
        claims = self._claims_state['claims']
        unresolved = {key: row['phase'] for key, row in claims.items()
                      if row['phase'] in _UNRESOLVED_PHASES and key not in self._futures}
        if unresolved:
            self._record_uncertain_claim(unresolved)
            raise ControlPumpError('control_reconciliation_required')
        for command_id, row in list(claims.items()):
            if row['phase'] != 'ready':
                continue
            target = self._member()
            reply = row['reply']
            # Revalidate persisted owner command before crossing the process boundary.
            try:
                statement = _verify_signed(reply['command_envelope'], expected_protocol=OWNER_COMMAND_PROTOCOL,
                    expected_key_record=self.client.trusted_owner_key_record,
                    expected_endpoint_id=reply['owner_endpoint_id'], now=self.client._clock())
            except ManagedControlError as error:
                if getattr(error, 'code', None) != 'managed_control_message_expired':
                    raise
                # AN EXPIRED COMMAND IS A NORMAL OUTCOME ON A SLOW PEER, NOT A LOOP FAILURE.
                # Round p75r1, measured end to end: this revalidation raised straight out of step();
                # _serve treats any exception as terminal, so the target went `blocked` and stayed there;
                # no further polls were made; the owner's decode hop then found no peer and died as
                # delivery_deadline_exceeded -> request_cleanup_unproven -> no terminal for the client.
                # The revalidation ABOVE must stay -- native work is never executed on an unrevalidated
                # instruction -- but dying for it must not: the command's signed TTL has passed, so it is
                # unusable, and executing it would be a stale instruction.  Retire THIS claim and keep
                # claiming work; the owner settles the row through its own reconciliation, because the peer
                # no longer lists it in active_claims and _answer_reconciliation_demand answers the demand
                # the owner then makes.  ONLY this exact code is declined -- every other rejection, including
                # a member mismatch or a signature failure, still fails closed.
                claims.pop(command_id, None)
                self._save_claims()
                try:
                    _atomic_private_json(self.root / 'expired-claim.private.json', {
                        'protocol': 'mycelium.a13.expired_claim.v1',
                        'run_id': self.worker.run_id,
                        'command_id_prefix': command_id[:16],
                        'remaining_claims': len(claims),
                        'in_flight_futures': len(self._futures),
                    })
                except BaseException:
                    # Diagnostics must never perturb control flow.  Bounded and allowlisted: the command id
                    # is truncated and no reply payload, claim token or key material is written.
                    pass
                continue
            binding = _binding_from_member(target, self.worker.run_id, self.worker.deployment_id)
            if any(statement[field] != value for field, value in binding.items()):
                raise ControlPumpError('control_membership_changed')
            command = statement['native_command']
            if command['command'] == 'infer_start' and 'token_grant' in command['payload']:
                self._token_progress.open(command)
            row['phase'] = 'executing'
            self._save_claims() # An interruption beyond here is conservatively uncertain.
            future = self._executor.submit(self.worker.send,
                command_id=command['command_id'], command=command['command'], payload=command['payload'])
            # Release the control loop's wait the MOMENT this native command finishes, from
            # the executor thread that finished it.  The ack that reports it is what frees
            # this peer's next delivery slot in the mailbox (its control-claim capacity is
            # counted from claimed rows), so leaving it to the loop's next interval tick is
            # delivery latency -- p81r1's shape, where a cleanup command was claimed at
            # 3 957 ms of a 4 427 ms life and could never complete.  Nothing about the
            # command's own deadline changes; only when the loop next runs.
            future.add_done_callback(lambda _: self.wake())
            self._futures[command_id] = future

    def _finish_outbound(self, target):
        packet = self._state['outbound']
        result = self._replay(target)
        if result is _OUTBOUND_EXPIRED:
            # The base pump handles this inside its own step(); this subclass
            # overrides step() and reaches _replay only through here, so without
            # this guard the sentinel was subscripted as a mapping and raised
            # "TypeError: 'object' is not subscriptable" -- which killed the
            # control loop and wedged the node (rounds 39 and 40).
            # _replay already dropped the provably-unusable envelope.  The
            # durable claims/pending rows are untouched, so a later step()
            # re-prepares the ack or poll and the node recovers.
            self._save()
            return {'state': 'outbound_expired_requeued', 'operation': packet['operation']}
        outcome = _outbound_outcome(result)
        if outcome is not None:
            # Same guard as above, for the two outcomes that are not the expired
            # sentinel: a retryable outcome KEPT the exact signed envelope, a rejected
            # one dropped it inside _replay.  Both are recoverable -- the durable
            # claims/pending rows are untouched, so a later step() re-prepares the
            # request and the node keeps claiming work.  Raising here is what left
            # round 58's peer permanently `blocked` with 0 target operations.
            self._save()
            return {'state': outcome.state, 'operation': packet['operation'],
                    'cause': outcome.cause}
        claims = self._claims_state['claims']
        if packet['operation'] == 'token_progress':
            self._progress_yield = True
            return self._token_progress.accepted(packet, result)
        if packet['operation'] == 'ack':
            statement = packet['envelope']['statement']
            command_id = statement['command_id']
            native = statement['native_response']
            if statement['disposition'] != 'reconciliation_required':
                claims.pop(command_id, None)
            else:
                # Retire the row once the uncertainty is acknowledged.  The ack is
                # durable on the OWNER side (its mailbox records the disposition),
                # which is where reconciliation happens; keeping the row here served
                # nothing and did two harms: it permanently tripped the uncertainty
                # guard (never removed -> `_dispatch_ready` raised forever) and grew
                # the claims map until the durable state failed its own `len > 3`
                # validation on the next load.  Native work is still never re-run.
                # Retain the local reason too. Install-policy diagnostics also
                # travel in the signed ack, separately from the null native result.
                existing = claims.get(command_id)
                self._record_uncertain_ack(command_id,
                    existing.get('error_code') if isinstance(existing, dict) else None,
                    max(0, len(claims) - 1))
                claims.pop(command_id, None)
            self._save_claims() # Replayed ack can safely finish after this write.
            self._token_progress.retire(command_id)
            self._state['outbound'] = None
            self._save()
            return {'state': statement['disposition'], 'command_id': command_id,
                'native_response': native, 'ack': result}
        if packet['operation'] != 'poll':
            raise ControlPumpError('concurrent_state_invalid')
        kind = result['delivery_kind']
        if kind == 'command':
            command_id = result['command_id']
            previous = claims.get(command_id)
            if previous is None:
                if len(claims) >= 3:
                    raise ControlPumpError('concurrent_claim_capacity_exhausted')
                claims[command_id] = {'phase': 'ready', 'reply': result, 'native_response': None, 'error_code': None}
                self._save_claims()
            elif _document_digest(previous['reply']) != _document_digest(result):
                raise ControlPumpError('control_claim_conflict')
        self._state['outbound'] = None
        self._save()
        if kind == 'reconciliation_required':
            return self._answer_reconciliation_demand(target, result)
        self._dispatch_ready()
        return {'state': 'dispatched', 'command_id': result['command_id']} if kind == 'command' else {'state': 'empty'}

    def _answer_reconciliation_demand(self, target, result):
        """Reply to an owner reconciliation demand instead of dying on it.

        The owner withholds the command body and reports
        ``delivery_kind='reconciliation_required'`` whenever it still holds a
        ``claimed`` row that the peer did not list in ``active_claims`` -- for
        example after a committed poll reply was lost and the peer dropped the dead
        envelope without ever recording the claim.

        Raising here (the previous behaviour) meant the peer NEVER sent the
        acknowledgement that settles that row, so the owner re-demanded it on every
        later poll and the peer stopped claiming work for the remainder of the run
        (round45: 5 claimed of 244; round47: qualified, then blocked mid-activation).

        Answering is safe and does not relax the fail-closed rule: nothing is
        re-executed, and the disposition is honest -- a native response already
        collected for this exact claim is reported as its real outcome, otherwise
        the outcome is declared uncertain so the owner can terminally record it.  If
        a native dispatch for this exact claim is still in flight in THIS process an
        outcome may still arrive, so the guard is preserved unchanged.
        """
        command_id = result['command_id']
        claim_token = result['claim_token']
        if command_id in self._futures:
            raise ControlPumpError('control_reconciliation_required')
        row = self._claims_state['claims'].get(command_id)
        native = None if row is None else row.get('native_response')
        if isinstance(native, Mapping):
            disposition = 'completed' if native.get('ok') is True else 'failed'
        else:
            disposition = 'reconciliation_required'
            native = None
        self._state['outbound'] = prepare(self.client, 'ack', target=target,
            run_id=self.worker.run_id, deployment_id=self.worker.deployment_id,
            command_id=command_id, claim_token=claim_token,
            disposition=disposition, native_response=native, **_install_diagnostic(row), **self._next_request())
        self._save()
        return self._finish_outbound(target)

    def step(self):
        with self._mutex:
            if self._closed:
                raise ControlPumpError('control_pump_closed')
            target = self._member()
            # Complete the exact previous HTTP operation before advancing sequence.
            if self._state['outbound'] is not None:
                return self._finish_outbound(target)
            self._collect()
            if self._progress_yield:
                self._progress_yield = False
            else:
                progress = self._token_progress.next(target)
                if progress is not None:
                    self._progress_yield = True
                    return progress
            for command_id, row in self._claims_state['claims'].items():
                if not self._token_progress.ready_to_ack(command_id):
                    continue
                if row['phase'] not in {'executed', 'reconciliation_required'}:
                    continue
                native = row['native_response']
                disposition = ('completed' if native['ok'] else 'failed') if native is not None else 'reconciliation_required'
                self._state['outbound'] = prepare(self.client, 'ack', target=target,
                    run_id=self.worker.run_id, deployment_id=self.worker.deployment_id,
                    command_id=command_id, claim_token=row['reply']['claim_token'],
                    disposition=disposition, native_response=native, **_install_diagnostic(row), **self._next_request())
                self._save()
                return self._finish_outbound(target)
            self._dispatch_ready()
            active = {key: row['reply']['claim_token'] for key, row in self._claims_state['claims'].items()}
            self._state['outbound'] = prepare(self.client, 'poll', target=target,
                run_id=self.worker.run_id, deployment_id=self.worker.deployment_id,
                active_claims=active, **self._next_request())
            self._save()
            return self._finish_outbound(target)

    def close(self):
        super().close()
        self._token_progress.close()
        self.worker.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
