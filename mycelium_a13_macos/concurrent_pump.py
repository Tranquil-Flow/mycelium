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
from .managed_control import OWNER_COMMAND_PROTOCOL, _verify_signed, _binding_from_member, _document_digest

_PROTOCOL = 'mycelium.a13.concurrent_claims.v1'

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

    def __init__(self, *args, **kwargs):
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
                    or value['binding'] != self._binding or not isinstance(value['claims'], dict)
                    or len(value['claims']) > 3):
                raise ControlPumpError('concurrent_state_invalid')
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
        for command_id, row in claims.items():
            if row['phase'] != 'ready':
                continue
            target = self._member()
            reply = row['reply']
            # Revalidate persisted owner command before crossing the process boundary.
            statement = _verify_signed(reply['command_envelope'], expected_protocol=OWNER_COMMAND_PROTOCOL,
                expected_key_record=self.client.trusted_owner_key_record,
                expected_endpoint_id=reply['owner_endpoint_id'], now=self.client._clock())
            binding = _binding_from_member(target, self.worker.run_id, self.worker.deployment_id)
            if any(statement[field] != value for field, value in binding.items()):
                raise ControlPumpError('control_membership_changed')
            command = statement['native_command']
            row['phase'] = 'executing'
            self._save_claims() # An interruption beyond here is conservatively uncertain.
            self._futures[command_id] = self._executor.submit(self.worker.send,
                command_id=command['command_id'], command=command['command'], payload=command['payload'])

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
                claims.pop(command_id, None)
            self._save_claims() # Replayed ack can safely finish after this write.
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
            disposition=disposition, native_response=native, **self._next_request())
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
            for command_id, row in self._claims_state['claims'].items():
                if row['phase'] not in {'executed', 'reconciliation_required'}:
                    continue
                native = row['native_response']
                disposition = ('completed' if native['ok'] else 'failed') if native is not None else 'reconciliation_required'
                self._state['outbound'] = prepare(self.client, 'ack', target=target,
                    run_id=self.worker.run_id, deployment_id=self.worker.deployment_id,
                    command_id=command_id, claim_token=row['reply']['claim_token'],
                    disposition=disposition, native_response=native, **self._next_request())
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
        self.worker.close()
        self._executor.shutdown(wait=False, cancel_futures=True)
