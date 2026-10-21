# SPDX-License-Identifier: AGPL-3.0-only
"""Target-initiated authenticated mailbox -> owned native process adapter.

Only genuine node replies can be acknowledged as native execution. Admission
refusals and ambiguous network/crash outcomes remain reconciliation-required;
this adapter never retries model work merely to recover a missing receipt.
Private state contains claim capabilities and MUST NOT be exported as evidence.
"""
from __future__ import annotations
try:  # POSIX lock; win32 goes through mycelium_host_platform.
    import fcntl
except ImportError:  # pragma: no cover - win32
    fcntl = None
import os
from pathlib import Path
import secrets
import stat
import threading
from typing import Callable, Mapping, Any

import mycelium_host_platform
from mycelium_host_platform import HostPlatformError

from .authority import _private_directory, _atomic_private_json, _load_private_json
from .managed_control import ManagedControlClient, _binding_from_member, _binding_advanced
from .worker import ManagedWorker, WorkerError

from .durable_requests import prepare, replay

_PROTOCOL='mycelium.a13.target_control_pump.v2'

class ControlPumpError(RuntimeError):
    """Stable control-pump failure with a bounded cause.

    ``cause_code`` is the allowlisted code of the underlying rejection, which the
    previous `from None` discarded.  Round58's peer was left ``blocked`` with
    ``control_poll_reconciliation_required`` and no way to tell a transient transport
    drop from a signed-request conflict -- two classes with different recoveries.
    Diagnostics only: raising, disposition and fail-closed behaviour are unchanged.
    """
    def __init__(self,code,cause_code=None):
        self.code=code;self.cause_code=cause_code
        super().__init__(code)


class _OutboundOutcome:
    """A replay that returned no verified reply, with a bounded cause.

    ``state`` is the recoverable status the caller reports; ``cause`` is the allowlisted
    underlying code.  Only the two NEW outcomes carry ``cause`` in their status dict,
    because ``{'state':'outbound_expired_requeued','operation':...}`` is pinned exactly
    by the rounds-39/40 regression tests.

    All three outcomes are recoverable and none of them replays native work: an outbound
    poll establishes no claim at all, and an ack is idempotent by ``command_id`` from the
    durable native response.
    """
    __slots__=('state','cause')
    def __init__(self,state,cause):
        self.state=state;self.cause=cause


_OUTBOUND_EXPIRED = object()
"""Returned by ``_replay`` when a durable envelope outlived its signed validity.

The dead envelope has already been dropped from durable state.  The caller must
return a *recoverable* status rather than raising: raising here is exactly what
permanently wedged the target control loop in round38 (one expired poll envelope
-> ``step()`` raised -> ``managed_runtime._serve`` caught it and exited forever,
so the node went ``blocked`` and 99/102 later owner commands expired unanswered).
"""

_UNUSABLE_REPLAY_CODES = frozenset({
    'managed_control_member_stale',
    'managed_control_sequence_replayed',
    'managed_control_nonce_replayed',
    'managed_control_conflicting_retry',
    'managed_control_prepared_request_invalid',
})
"""Rejections that make the persisted request permanently unusable.

The mailbox can never accept these exact bytes (stale member binding, a replayed
sequence or nonce, a conflicting retry, a malformed persisted envelope), so retrying
them can only fail again.  Dropping the envelope and signing a fresh sequence/nonce is
the only recovery, and it is safe: a rejected request established no claim, and an ack
is rebuilt from the durable native response.
"""

_RETRYABLE_REPLAY_CODES = frozenset({
    'managed_control_unreachable',
    'managed_control_response_timeout',
    'managed_control_internal_error',
    'managed_control_response_invalid',
    'managed_control_response_signature_invalid',
})
"""Transient or unverifiable failures where the exact signed bytes remain valid.

These are KEPT and retried unchanged.  Re-signing here would burn a fresh
sequence/nonce for a request that may still be in flight, and on a poll it would
abandon a claim the mailbox may already have made.  Nothing is accepted on retry unless
the reply verifies, so fail-closed is untouched; the envelope's own signed TTL bounds
the retrying -- once it expires it becomes an ``_OUTBOUND_EXPIRED`` requeue.
"""

_OUTBOUND_RETRY_DIAGNOSTIC='outbound-retry.private.json'
"""Bounded private diagnostic for a retrying outbound.  Diagnostics ONLY."""


def _outbound_outcome(value):
    return value if isinstance(value,_OutboundOutcome) else None


def _expired_envelope(error: BaseException) -> bool:
    """True only for the provably-unusable expired-envelope rejection.

    A signed envelope past its validity can never be accepted by the server, so
    this is one replay failure guaranteed safe to discard -- replaying it
    could only ever fail again, and dropping it loses nothing recoverable.
    ``_UNUSABLE_REPLAY_CODES`` covers the rest of that class; every other failure
    still fails closed.
    """
    return getattr(error, 'code', None) == 'managed_control_message_expired'

class TargetControlPump:
    def __init__(self,state_root:Path,*,client:ManagedControlClient,worker:ManagedWorker,
                 current_member:Callable[[],Mapping[str,Any]], sequence_root=None):
        self.root=_private_directory(Path(state_root),create=True)
        self.client=client;self.worker=worker;self.current_member=current_member
        self._sequence_root=sequence_root
        self._mutex=threading.RLock();self._closed=False;self._fd=-1;self._win32_lock=None
        # Delivery wake-up for the control loop (see `wake` / `wait_for_wake`).  The loop
        # may not be claim-latency-bound by its own interval: the ack that frees this
        # peer's next delivery slot, and the enqueue that fills it, must be able to end
        # the wait at once.
        self._delivery_wakeup=threading.Event()
        # Consecutive retriable outbound attempts in THIS process, for the bounded
        # diagnostic only.  Never consulted for control flow.
        self._outbound_retry_attempts=0
        self.path=self.root/'state.json'
        self._binding=_binding_from_member(current_member(),worker.run_id,worker.deployment_id)
        if self._binding['target_node_id']!=worker.node_id:raise ControlPumpError('control_worker_identity_mismatch')
        try:
            if mycelium_host_platform.is_win32():
                try:
                    self._win32_lock=mycelium_host_platform.hold_exclusive_lock(self.root,'owner.lock',blocking=False)
                except HostPlatformError as exc:
                    if exc.code=='host_lock_busy':raise ControlPumpError('control_pump_already_running') from None
                    raise ControlPumpError('control_state_invalid') from None
            else:
                self._fd=os.open(self.root/'owner.lock',os.O_CREAT|os.O_RDWR|getattr(os,'O_NOFOLLOW',0),0o600)
                info=os.fstat(self._fd)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600:
                    raise ControlPumpError('control_state_invalid')
                try:fcntl.flock(self._fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise ControlPumpError('control_pump_already_running') from None
            if not getattr(self,'_concurrent',False) and os.path.lexists(self.root/'concurrent.json'):
                raise ControlPumpError('concurrent_state_requires_concurrent_pump')
            value=_load_private_json(self.path)
            if value is None:
                value={'protocol':_PROTOCOL,'binding':self._binding,'sequence':0,'pending':None,'outbound':None}
                _atomic_private_json(self.path,value)
            legacy=value.get('protocol')=='mycelium.a13.target_control_pump.v1'
            if legacy:
                value['protocol']=_PROTOCOL;value['outbound']=None
                pending=value.get('pending')
                if isinstance(pending,dict) and pending.get('native_response') is None:
                    pending['phase']='reconciliation_required'
            if set(value)!={'protocol','binding','sequence','pending','outbound'} or value['protocol']!=_PROTOCOL or type(value['sequence']) is not int or value['sequence']<0:
                raise ControlPumpError('control_state_invalid')
            if value['binding']!=self._binding:
                # A resumed pump whose durable binding is an OLDER incarnation of the
                # same member must rebind rather than refuse: relay rotation re-admits
                # the node, and refusing here ends the run at the next restart.
                if not _binding_advanced(value['binding'],self._binding):
                    raise ControlPumpError('control_membership_changed')
                value['binding']=self._binding
                _atomic_private_json(self.path,value)
            if value['pending'] is not None and not isinstance(value['pending'],dict):raise ControlPumpError('control_state_invalid')
            if value['outbound'] is not None and not isinstance(value['outbound'],dict):raise ControlPumpError('control_state_invalid')
            self._state=value
            if legacy:self._save()
        except BaseException:
            if self._fd>=0:os.close(self._fd);self._fd=-1
            if self._win32_lock is not None:self._win32_lock.release();self._win32_lock=None
            raise

    def _save(self):
        _atomic_private_json(self.path,self._state)

    def _next_request(self):
        if self._sequence_root is None:
            self._state['sequence']+=1
        else:
            from .control_sequence import next_target_sequence
            self._state['sequence']=next_target_sequence(self._sequence_root,
                signer_key_digest=self.client.signer.verification_key_digest,
                swarm_id=self.client.swarm_id,at_least=self._state['sequence'])
        self._save()
        return {'sequence':self._state['sequence'],'nonce':secrets.token_hex(16),'request_id':secrets.token_hex(16)}

    def _member(self):
        target=self.current_member()
        binding=_binding_from_member(target,self.worker.run_id,self.worker.deployment_id)
        if binding!=self._binding:
            # Relay rotation re-admits the SAME node with a newer incarnation; the
            # durable binding captured at construction then no longer matches and the
            # pump used to raise here, which ended the control loop for the rest of the
            # run (round 64r1).  Rebind and carry on.  A different member, or a rewind,
            # still fails closed.
            if not _binding_advanced(self._binding,binding):
                raise ControlPumpError('control_membership_changed')
            self._binding=binding
            self._state['binding']=binding
            self._save()
        return target

    def _replay(self,target):
        operation=self._state['outbound']['operation']
        try:
            reply=replay(self.client,self._state['outbound'],target=target)
        except Exception as error:
            cause=getattr(error,'code',None)
            expired=_expired_envelope(error)
            if expired or cause in _UNUSABLE_REPLAY_CODES:
                # Provably unusable, and leaving it durable is what wedged the
                # pump: every later step would replay this same dead envelope and
                # the control loop would die on the raised error.  Drop it and
                # report recoverable so step() signs a fresh one.  Nothing native
                # is replayed: a rejected request established no claim, and an
                # ack is rebuilt from the durable native response.
                self._state['outbound']=None
                self._save()
                return (_OUTBOUND_EXPIRED if expired
                    else _OutboundOutcome('outbound_rejected_requeued',cause))
            if cause in _RETRYABLE_REPLAY_CODES:
                # Transient or unverifiable: the exact signed bytes are still valid,
                # so they are KEPT and a later step retries them unchanged.  Re-signing
                # would burn a fresh sequence/nonce for a request that may still be in
                # flight, and on a poll would abandon a claim the mailbox may already
                # have made.  Round58's peer died here instead.
                self._record_outbound_retry(operation,cause)
                return _OutboundOutcome('outbound_retryable',cause)
            # Unclassified (uncoded, or a fail-closed guard such as a corrupt own
            # signature): keep the raise, and keep the cause instead of discarding it
            # with `from None`, which is what left round58's peer unexplained.
            raise ControlPumpError('control_'+operation+'_reconciliation_required',
                cause_code=cause) from error
        self._clear_outbound_retry()
        return reply

    def _record_outbound_retry(self,operation,cause):
        """Record a retried outbound.  Diagnostics ONLY; never changes control flow."""
        try:
            self._outbound_retry_attempts+=1
            _atomic_private_json(self.root/_OUTBOUND_RETRY_DIAGNOSTIC,
                {'protocol':'mycelium.a13.outbound_retry.v1','run_id':self.worker.run_id,
                 'operation':operation,'cause':cause,'attempts':self._outbound_retry_attempts})
        except BaseException:
            pass

    def _clear_outbound_retry(self):
        """A verified reply retires the retry diagnostic; nothing else does."""
        if not self._outbound_retry_attempts:
            return
        self._outbound_retry_attempts=0
        try:(self.root/_OUTBOUND_RETRY_DIAGNOSTIC).unlink()
        except OSError:
            pass

    def step(self):
        with self._mutex:
            if self._closed:raise ControlPumpError('control_pump_closed')
            target=self._member()
            pending=self._state['pending']
            # A pending command RESUMED from durable state is the case the guard below
            # exists for.  One created in THIS step (by the poll a few lines down) is
            # not: the mailbox has just asked us to reconcile, and `outbound` was
            # cleared moments earlier, so the guard cannot tell the two apart on state
            # alone.  Measured 2026-09-15 (round 54, preview.57): the first
            # `delivery_kind='reconciliation_required'` reply raised straight out of
            # `step()`, `managed_runtime._serve` treats any exception as terminal, the
            # node went `blocked`, and 117 later `health` commands expired unclaimed
            # while the owner's own node completed 120/120.
            resumed = pending is not None
            if pending is None:
                if self._state['outbound'] is None:
                    self._state['outbound']=prepare(self.client,'poll',target=target,
                        run_id=self.worker.run_id,deployment_id=self.worker.deployment_id,**self._next_request())
                    self._save() # Save the signed poll BEFORE the server can claim work.
                reply=self._replay(target)
                if reply is _OUTBOUND_EXPIRED:
                    self._save()
                    return {'state':'outbound_expired_requeued'}
                outcome=_outbound_outcome(reply)
                if outcome is not None:
                    # Recoverable, not fatal: a retryable outcome kept the exact signed
                    # envelope for a later attempt, and a rejected one was already
                    # dropped by _replay so the next step signs a fresh request.
                    self._save()
                    return {'state':outcome.state,'operation':'poll','cause':outcome.cause}
                self._state['outbound']=None
                if reply['delivery_kind']=='empty':
                    self._save();return {'state':'empty'}
                pending={'phase':'ready','reply':reply,'native_response':None,'error_code':None}
                if reply['delivery_kind']=='reconciliation_required':pending['phase']='reconciliation_required'
                self._state['pending']=pending;self._save()
            if resumed and pending['phase'] in {'executing','reconciliation_required'} and self._state['outbound'] is None:
                # Unknown native outcome is not safe to re-run. Recoverable network
                # loss is different: its exact outbound request remains available.
                raise ControlPumpError('control_reconciliation_required')
            reply=pending['reply']
            command=reply['command_envelope']['statement']['native_command']
            if command['run_id']!=self.worker.run_id or command['deployment_id']!=self.worker.deployment_id:
                raise ControlPumpError('control_worker_identity_mismatch')
            if pending['phase']=='ready':
                target=self._member()
                pending['phase']='executing';self._save() # Crash here is conservative uncertainty.
                response=None;error_code=None
                try:
                    response=self.worker.send(command_id=command['command_id'],command=command['command'],payload=command['payload'])
                except WorkerError as error:error_code=str(error)
                pending.update(phase='executed' if response is not None else 'reconciliation_required',
                    native_response=response,error_code=error_code)
                self._save() # Actual native output is durable before an ack exists.
            response=pending['native_response']
            disposition=('completed' if response['ok'] else 'failed') if response is not None else 'reconciliation_required'
            target=self._member()
            if self._state['outbound'] is None:
                self._state['outbound']=prepare(self.client,'ack',target=target,
                    run_id=self.worker.run_id,deployment_id=self.worker.deployment_id,
                    command_id=command['command_id'],claim_token=reply['claim_token'],
                    disposition=disposition,native_response=response,**self._next_request())
                self._save()
            ack=self._replay(target)
            if ack is _OUTBOUND_EXPIRED:
                # Keep 'pending': the executed native response stays durable so a
                # later step rebuilds the ack from it (an ack is idempotent by
                # command_id).  Returning recoverable here keeps the control loop
                # alive instead of wedging the node.
                self._save()
                return {'state':'ack_expired_requeued','command_id':command['command_id'],
                    'native_response':response,'error_code':pending.get('error_code')}
            outcome=_outbound_outcome(ack)
            if outcome is not None:
                # Keep 'pending': an ack is idempotent by command_id and is rebuilt from
                # the durable native response, so a rejected or retrying ack is
                # recoverable.  Dying here is what permanently blocked the peer.
                self._save()
                return {'state':outcome.state,'operation':'ack','cause':outcome.cause,
                    'command_id':command['command_id'],'native_response':response,
                    'error_code':pending.get('error_code')}
            self._state['outbound']=None
            # Reaching here means the ack was accepted (`_replay` raises otherwise), so
            # the mailbox now owns the outcome -- including a declared
            # `reconciliation_required`.  Keeping the pending for that disposition made
            # the NEXT step re-enter the guard above with `resumed` set and raise,
            # wedging the loop one step later; the durable record has been handed over,
            # so it is retired here.
            self._state['pending']=None
            self._save()
            return {'state':disposition,'command_id':command['command_id'],'native_response':response,
                'error_code':pending.get('error_code'),'ack':ack}

    def wake(self):
        """Release the control loop's wait: the delivery path changed.

        Called when a native command finishes (so the ack that frees this peer's next
        delivery slot goes out at once) and on close (so a waiting loop exits at once).
        A wake-up only ever *ends* a wait early; it never lengthens one and it is not an
        outcome -- the loop's next `step()` decides everything as before.
        """
        self._delivery_wakeup.set()

    def wait_for_wake(self,timeout):
        """Wait up to ``timeout`` for a delivery wake-up; True if woken.

        ``timeout`` is the loop's own interval, unchanged and still the ceiling, so a
        missed wake-up parks the loop no longer than the interval it used before.
        """
        woken=self._delivery_wakeup.wait(max(0.0,float(timeout)))
        self._delivery_wakeup.clear()
        return bool(woken)

    def close(self):
        with self._mutex:
            self._closed=True
            if self._fd>=0:
                fcntl.flock(self._fd,fcntl.LOCK_UN);os.close(self._fd);self._fd=-1
            if self._win32_lock is not None:
                self._win32_lock.release();self._win32_lock=None
        # Release a loop parked in wait_for_wake so closing never waits out an interval.
        self.wake()
