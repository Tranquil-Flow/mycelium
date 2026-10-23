# SPDX-License-Identifier: AGPL-3.0-only
"""Target application ownership of verified staging, native worker and pump.

No model is authorized implicitly. A target-local exact load authorizer is
required for configure/inference. HTTPS command intake cannot select executables,
roots, resource policies or authorizers. Failed/ambiguous runs retain private
journals and staged content for reconciliation rather than automatically retrying.
"""
from __future__ import annotations
import math
from pathlib import Path
import re
import shutil
import threading
import tempfile
import time
import traceback

from .authority import _private_directory, _atomic_private_json
from .concurrent_pump import ConcurrentTargetControlPump
from .managed_control import _binding_from_member, _binding_advanced
from .managed_staging import TargetStaging, StagingError
# _NATIVE_DIAGNOSTIC_FRAME is imported rather than re-declared: one regex decides what a bounded frame
# looks like for BOTH boundaries (the worker's native diagnostics and the runtime's control-loop death).
from .worker import ManagedWorker, _NATIVE_DIAGNOSTIC_FRAME


class RuntimeErrorCode(ValueError):
    pass


# One replacement per worker death is the contract; the bound exists so a worker that cannot start
# (stale socket, missing staging, load refused) cannot be respawned in a loop.
_MAX_WORKER_REPLACEMENTS = 3
_WORKER_REPLACEMENT_PROTOCOL = 'mycelium.a13.worker_replacement.v1'

_CONTROL_FAILURE_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_.:]{0,63}")


def _bounded_control_reason(exc: BaseException) -> dict:
    """Bounded, allowlisted identity for the exception that stopped the loop.

    Collapsing every unrecognised failure into ``runtime_control_failed`` threw the
    real cause away and cost a whole debug cycle: the target's control loop died,
    the node went ``blocked``, every owner command after that expired unanswered,
    and the only code reported anywhere was the generic fallback.  Keep the
    exception class name plus a few allowlisted tokens from the message so the next
    occurrence is self-describing.  No URLs, no paths, no free text: only
    ``[A-Za-z_][A-Za-z0-9_.:]*`` tokens of bounded length survive.
    """

    tokens: list[str] = []
    for candidate in _CONTROL_FAILURE_TOKEN.findall(str(exc)):
        if candidate not in tokens:
            tokens.append(candidate[:64])
        if len(tokens) == 4:
            break
    return {'exception_type': type(exc).__name__[:64], 'tokens': tokens,
            'frames': _bounded_control_frames(exc)}


def _bounded_control_frames(exc: BaseException) -> list[dict]:
    """WHERE the control loop died, bounded the same way the native diagnostics are.

    WHY: the reason record named the exception class and the codes but never the site.  Measured on
    round p75r1's target artifact, ``runtime-control-failure.private.json`` held

        error_code: managed_control_message_expired · exception_type: ManagedControlError · no frames

    and the same wedge class has recurred after being fixed twice at the pump level (round 38's dead
    poll envelope, round 54's ``reconciliation_required`` reply -- both in ``control_pump.py``).  A
    class name cannot distinguish "the pump raised from a path that is already handled" from "the pump
    raised from a path that is not", which is exactly the question a fix turns on.

    Same allowlist discipline as the native diagnostics, through the SAME regex: file basename, line
    and function only, capped at 24, never the message and never a path.
    """
    frames: list[dict] = []
    try:
        text = ''.join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    except Exception:
        return frames
    for file, line, function in _NATIVE_DIAGNOSTIC_FRAME.findall(text):
        name = Path(file).name
        if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]{0,80}[.]py', name):
            frames.append({'file': name, 'line': int(line), 'function': function[:80]})
    return frames[-24:]


def _current_binding(member, run_id, deployment_id):
    """Binding of `member` for this run, or None when it cannot be derived."""

    try:
        return _binding_from_member(member, run_id, deployment_id)
    except Exception:
        return None


class ManagedTargetRuntime:
    # A16 defaults (also for partially-constructed runtimes): never withdrawn, no drain tick.
    _withdrawn=False
    _adaptive_drain=None

    def __init__(self, state_root, *, client, current_member, run_id, deployment_id,
                 staging:TargetStaging, archive:bytes, archive_digest,
                 resource_policy, limits, sidecar_binary, authorize_load,
                 preposition=None, endpoint_secret_file=None, sequence_root=None, _test_observer=None,
                 adaptive_overlay=None, adaptive_drain=None):
        if not callable(current_member) or not isinstance(staging,TargetStaging):
            raise RuntimeErrorCode('runtime_configuration_invalid')
        self._current_member=current_member
        self.run_id=run_id; self.deployment_id=deployment_id
        try:
            member=current_member()
            self._binding=_binding_from_member(member,run_id,deployment_id)
            if (staging.node_id!=self._binding['target_node_id'] or
                client.signer.verification_key_digest!=self._binding['target_key_digest']):
                raise ValueError()
        except Exception: raise RuntimeErrorCode('runtime_member_invalid') from None
        self._archive=archive
        self._preposition=preposition
        self.archive_digest=archive_digest
        self.staging=staging
        self._verify_stage()
        self.root=_private_directory(Path(state_root),create=True)
        # A16 optional adaptive host-resource overlay; None (default) publishes nothing.
        self._adaptive=None
        # A16 drain-to-withdraw tick (callable taking this runtime); None = never unloads.
        self._adaptive_drain=adaptive_drain
        self._withdrawn=False
        if adaptive_overlay is not None:
            from .adaptive_host import OverlayPublisher
            self._adaptive=OverlayPublisher(self.root,adaptive_overlay)
        self._state='prepared'; self._error=None
        # Consecutive retriable-outbound backoff, bounded at 1.0 s.  Loop-local
        # pacing only; it never gates a disposition or a terminal state.
        self._outbound_retry_backoff=.1
        self._lock=threading.RLock(); self._stop=threading.Event(); self._thread=None
        # Darwin limits Unix socket paths; installed private state paths can
        # exceed that limit. Only sockets are transient: capacity/journals stay
        # in the original durable run root, never the temporary directory.
        import mycelium_host_platform
        if mycelium_host_platform.is_win32():
            # Windows sidecars use the loopback channel; the directory is unused
            # by the transport and only needs to be a private temporary path.
            temp_base=Path(tempfile.gettempdir())
        else:
            temp_base=Path('/private/tmp') if Path('/private/tmp').is_dir() else Path('/tmp')
        self._socket_parent=Path(tempfile.mkdtemp(prefix='myc-a13-',dir=temp_base))
        # Retain construction inputs for bounded pre-load process replacement.
        # A configured worker instead requires a fresh run and re-qualification.
        self._sidecar_binary=sidecar_binary
        self._resource_policy=resource_policy
        self._limits=limits
        self._authorize_load=authorize_load
        self._endpoint_secret_file=endpoint_secret_file
        self._test_observer=_test_observer
        self._worker_generation=0
        self._worker_replacements=0
        self._replacement_worker=None
        self._token_client=client
        self._token_host_pin=None
        try:
            from .token_policy import prepare_host_pin
            from mycelium_router.token_path_authority import TokenPathAuthorityError
            try:
                self._token_host_pin=prepare_host_pin(root=self.root,client=client,current_member=self._member,
                    run_id=run_id,deployment_id=deployment_id,max_wall_seconds=limits.max_wall_seconds)
            except TokenPathAuthorityError:
                # Stale optional capability cannot break OFF; ON installation
                # still refuses, never silently selects ordinary inference.
                self._token_host_pin=None
            self.worker=self._make_worker()
            self.pump=ConcurrentTargetControlPump(self.root/'pump',client=client,worker=self.worker,
                current_member=self._member,sequence_root=sequence_root,
                token_progress_cursor=self._token_progress_cursor)
        except BaseException:
            self._socket_parent.rmdir()
            raise

    def _token_progress_cursor(self, command):
        return self.worker.token_progress_cursor(command)

    def _token_member(self):
        from .token_policy import member_pin
        return member_pin(self._token_client,self._member())

    def _refresh_token_host_pin(self):
        from .token_policy import prepare_host_pin
        return prepare_host_pin(root=self.root, client=self._token_client, current_member=self._member,
            run_id=self.run_id, deployment_id=self.deployment_id,
            max_wall_seconds=self._limits.max_wall_seconds)

    def _make_worker(self):
        """Build one worker process.  Called once at start and again per replacement.

        Each generation gets its OWN socket directory inside the runtime's private parent.  A Unix socket
        file outlives the process that bound it, so re-using the same path for a replacement would fail
        to bind -- which is one of the reasons a replacement needs its own path rather than the first
        worker's.
        """

        suffix='s' if self._worker_generation==0 else 's%d' % self._worker_generation
        return ManagedWorker(node_id=self.staging.node_id,run_id=self.run_id,
            deployment_id=self.deployment_id,
            artifact_root=self.staging.staging_root,socket_root=self._socket_parent/suffix,
            capacity_root=self.root,
            sidecar_binary=self._sidecar_binary,resource_policy=self._resource_policy,limits=self._limits,
            membership_current=self._membership_current,authorize_load=self._authorize_load,
            endpoint_secret_file=self._endpoint_secret_file,_test_observer=self._test_observer,
            token_policy_trust_file=self._token_host_pin,token_policy_member=self._token_member,
            token_policy_refresh=self._refresh_token_host_pin)

    def _verify_stage(self):
        try:
            self._marker=self.staging.verify_prepared(archive=self._archive,
                archive_digest=self.archive_digest,preposition=self._preposition)
        except StagingError: raise RuntimeErrorCode('runtime_staging_binding_mismatch') from None

    def _member(self):
        try:
            member=self._current_member()
            lease=member.get('lease_expires_at')
            if (type(lease) not in (int,float) or not math.isfinite(lease) or time.time()>=lease):
                raise ValueError()
            binding=_current_binding(member,self.run_id,self.deployment_id)
            if binding!=self._binding:
                # Relay rotation re-admits the SAME node with a newer incarnation and
                # that must not read as stale.  Round 64r1 failed closed right here: the
                # node's own re-admission made _membership_current() false, the worker's
                # guard raised worker_membership_not_current, and the native worker
                # closed for good one second before the run-level configure became
                # unknowable.  Rebind and carry on; a different member, or a rewind,
                # still fails closed.
                if binding is None or not _binding_advanced(self._binding,binding):
                    raise ValueError()
                self._binding=binding
            return member
        except Exception: raise RuntimeErrorCode('runtime_member_stale') from None

    def _membership_current(self):
        try: self._member(); return True
        except RuntimeErrorCode: return False

    def start(self):
        with self._lock:
            if self._state!='prepared': raise RuntimeErrorCode('runtime_restart_requires_reconciliation')
            try:
                self._member(); self._verify_stage()
                self.worker.start()
                self._state='running'
                self._thread=threading.Thread(target=self._serve,name='a13-target-control',daemon=True)
                self._thread.start()
            except Exception:
                self._state='blocked'; self._error='runtime_start_failed'
                self.pump.close()
                raise
            return self.status()

    def _replace_worker_if_dead(self):
        """Replace only a pre-load process; configured runs require cold restart.

        No replay, request continuity, readiness or old-worker cleanup receipt is
        inferred from a new process. The route owner must start a fresh run and
        re-qualify after configure has been dispatched, even if its reply was lost.
        """

        if self._stop.is_set() or self._withdrawn:
            return  # an A16 withdrawal is deliberate: never mint a replacement model process
        with self._lock:
            if self._state!='running':
                return
        try:
            status=self.worker.status()
        except Exception:
            return
        if status.get('running'):
            return
        # Freeze command admission before deciding whether any native model
        # state could exist. A concurrent configure either committed before close
        # (and requires cold restart), or cannot commit after close.
        previous=self.worker
        # Capture the *current* worker before close/reap changes its return code.
        # The older native-exit.private.json can belong to a bootstrap lifetime.
        previous_cause=previous.private_cause_snapshot()
        try:
            previous.close()
            if not previous.cleanup_proven() or previous.process_group_present():
                raise RuntimeErrorCode('runtime_stop_incomplete')
        except Exception:
            self._stop.set()
            _atomic_private_json(self.root/'worker-replacement.private.json',{
                'protocol':_WORKER_REPLACEMENT_PROTOCOL,'run_id':self.run_id,
                'attempt':self._worker_replacements,'outcome':'failed',
                'error_code':'runtime_replacement_cleanup_unproven',
                'max_attempts':_MAX_WORKER_REPLACEMENTS,
                'recorded_at_utc_ms':int(time.time()*1000),**previous_cause})
            raise RuntimeErrorCode('runtime_replacement_cleanup_unproven')
        if previous.requires_cold_restart():
            with self._lock:
                self._stop.set()
                self._state='blocked'; self._error='runtime_cold_restart_required'
            _atomic_private_json(self.root/'worker-replacement.private.json',{
                'protocol':_WORKER_REPLACEMENT_PROTOCOL,'run_id':self.run_id,
                'attempt':self._worker_replacements,'outcome':'refused',
                'error_code':'runtime_cold_restart_required',
                'max_attempts':_MAX_WORKER_REPLACEMENTS,
                'recorded_at_utc_ms':int(time.time()*1000),**previous_cause})
            raise RuntimeErrorCode('runtime_cold_restart_required')
        refusal=self.worker.replacement_refusal()
        if refusal is not None:
            if getattr(self,'_replacement_refusal_recorded',None)!=refusal:
                _atomic_private_json(self.root/'worker-replacement.private.json',{
                    'protocol':_WORKER_REPLACEMENT_PROTOCOL,'run_id':self.run_id,
                    'attempt':self._worker_replacements,'outcome':'refused',
                    'error_code':refusal,'max_attempts':_MAX_WORKER_REPLACEMENTS,
                    'recorded_at_utc_ms':int(time.time()*1000),**previous_cause})
                self._replacement_refusal_recorded=refusal
            return
        with self._lock:
            if (self._state!='running' or self._stop.is_set()
                    or self._worker_replacements>=_MAX_WORKER_REPLACEMENTS):
                return
            self._worker_replacements+=1
            attempt=self._worker_replacements
        record={'protocol':_WORKER_REPLACEMENT_PROTOCOL,'run_id':self.run_id,'attempt':attempt,
            'recorded_at_utc_ms':int(time.time()*1000),**previous_cause,
            'max_attempts':_MAX_WORKER_REPLACEMENTS}
        previous=self.worker
        replacement=None
        cleanup_unproven=False
        try:
            self._worker_generation+=1
            replacement=self._make_worker()
            replacement.inherit_execution_budget(previous)
            # Own the unpublished process too: close must be able to interrupt
            # native startup before it joins this control thread.
            with self._lock:
                if self._stop.is_set() or self._state!='running' or self.worker is not previous:
                    raise RuntimeErrorCode('runtime_replacement_stopped')
                self._replacement_worker=replacement
            replacement.start()

            with self._lock:
                if self._stop.is_set() or self._state!='running' or self.worker is not previous:
                    raise RuntimeErrorCode('runtime_replacement_stopped')
                if not replacement.status().get('running'):
                    raise RuntimeErrorCode('runtime_replacement_not_running')
                self.worker=replacement
                self.pump.worker=replacement
                record['outcome']='replaced'
        except Exception as exc:
            if replacement is not None:
                try:
                    replacement.close()
                    if not replacement.cleanup_proven() or replacement.process_group_present():
                        raise RuntimeErrorCode('runtime_stop_incomplete')
                except Exception:
                    # Retain ownership for explicit close; a failed teardown is
                    # not permission to forget the process or mint another one.
                    cleanup_unproven=True
                    record['cleanup_error_code']='runtime_replacement_cleanup_unproven'
                    with self._lock:
                        self._replacement_worker=replacement
                        self._stop.set()
            record['outcome']='failed'
            code=getattr(exc,'code',None)
            record['error_code']=code if isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',code) else None
            cause=str(exc)
            if re.fullmatch(r'[a-z][a-z0-9_]{0,63}',cause):
                record['cause_code']=cause
        finally:
            with self._lock:
                if not cleanup_unproven and self._replacement_worker is replacement:
                    self._replacement_worker=None
        try: _atomic_private_json(self.root/'worker-replacement.private.json',record)
        except BaseException: pass
        if cleanup_unproven:
            raise RuntimeErrorCode('runtime_replacement_cleanup_unproven')
        if record['outcome']!='replaced':
            return


    @property
    def model_unloaded(self):
        return self._withdrawn

    def unload_model(self):
        """A16 withdraw: close the model worker with cleanup proof; membership and control stay up.

        Only the caller (the drain tick) decides *when*; it calls this once no request is in
        flight.  A closed, configured worker needs a fresh run to serve again (cold restart).
        """
        with self._lock:
            if self._withdrawn:
                return True
            if self._stop.is_set() or self._state!='running':
                return False
            worker=self.worker
        try:
            worker.close()
            proven=bool(worker.cleanup_proven()) and not worker.process_group_present()
        except Exception:
            return False
        if proven:
            with self._lock: self._withdrawn=True
        return proven

    def _serve(self):
        try:
            while not self._stop.is_set():
                self._member()
                if self._adaptive is not None: self._adaptive.tick()
                if self._adaptive_drain is not None:
                    try: self._adaptive_drain(self)
                    except Exception: pass  # a drain fault leaves the model loaded; it never kills control
                self._replace_worker_if_dead()
                step=self.pump.step()
                # A retrying outbound is a bounded recovery, not a death: back off a
                # little so a down origin is not hammered at the 50 ms poll cadence.
                # Bounded by the envelope's own signed TTL, and native dispatch is
                # unaffected (it runs on the pump's executor, not on this wait).
                delay=.05
                if isinstance(step,dict) and step.get('state')=='outbound_retryable':
                    self._outbound_retry_backoff=min(1.0,max(.1,self._outbound_retry_backoff*2))
                    delay=self._outbound_retry_backoff
                    # The retry backoff keeps its own wait on purpose: a peer that owes an
                    # undone outbound cannot send anything new until that retry resolves, so
                    # waking it early could only hammer a down origin -- the thing this
                    # backoff exists to prevent.  Delivery promptness is not at stake here.
                    self._stop.wait(delay)
                else:
                    self._outbound_retry_backoff=.1
                    # Wait on the delivery wake-up with the cadence as the ceiling: a native
                    # command finishing (which frees this peer's next claim slot, because the
                    # ack is what retires the mailbox's claimed row), or an enqueue arriving
                    # on an in-process mailbox, ends the wait at once.  The interval is
                    # unchanged and still bounds the wait; only its promptness is.  This is
                    # the delivery side of D9: p81r1's inversion was claim latency +
                    # execution latency against a ~4.4 s window, not a window too small.
                    self.pump.wait_for_wake(delay)
        except Exception as exc:
            code=str(exc)
            if not re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code): code='runtime_control_failed'
            # Record WHY before stopping. The generic fallback above is all the
            # operator sees otherwise, and a hidden control-loop death is
            # indistinguishable from a node that simply stopped claiming work --
            # every later owner command expires unanswered and the gateway loses
            # all observation of this node. Diagnostics only: never changes the
            # fail-closed disposition below.
            try:
                record={'protocol':'mycelium.a13.runtime_control_failure.v1',
                    'run_id':self.run_id,'state':'blocked','error_code':code,
                    **_bounded_control_reason(exc)}
                # Keep the underlying rejection code when the raiser carries one.  Round
                # 58's peer recorded only `control_poll_reconciliation_required`, which
                # cannot discriminate a transient transport drop from a signed-request
                # conflict -- two classes with different recoveries.
                cause=getattr(exc,'cause_code',None)
                if isinstance(cause,str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}',cause):
                    record['cause_code']=cause
                _atomic_private_json(self.root/'runtime-control-failure.private.json',record)
            except BaseException:
                pass
            # Stop the process before publishing a terminal state. Never turn
            # transport uncertainty into an automatic replay of native work.
            self.pump.close()
            with self._lock:
                if self._state!='closed':
                    self._state='blocked'; self._error=code
        finally:
            self._stop.set()

    def status(self):
        # work_admitted: this runtime was started for accepted work (a target-local
        # load authorizer is attached), as opposed to bootstrap hello-only control.
        # model_load_authorized only turns true after the owner's configure, which
        # the owner sends after activation -- so "work accepted and running" must
        # be read from work_admitted, never from model_load_authorized.
        with self._lock:
            return {'state':self._state,'error_code':self._error,'route_ready':False,
                'worker_running':self.worker.status()['running'],
                'model_load_authorized':self.worker.status()['model_load_authorized'],
                'work_admitted':self._authorize_load is not None}

    def close(self,*,cleanup=False):
        with self._lock:
            self._stop.set()
            worker=self.worker
            replacement=self._replacement_worker
        # Release the control loop's delivery wait at once, so a stop is never delayed by
        # the interval (or the retry backoff) the loop happens to be sitting out.  Waiting
        # is bounded by the same numbers as before; this only makes it end sooner.
        self.pump.wake()
        # Native shutdown first bounds in-flight response waits, then HTTP/pump
        # ownership is released. No lock is held while joining the pump thread.
        worker.close()
        if replacement is not None and replacement is not worker:
            replacement.close()
        thread=self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
            if thread.is_alive(): raise RuntimeErrorCode('runtime_stop_incomplete')
        self.pump.close()
        if (not worker.cleanup_proven() or worker.process_group_present()
                or (replacement is not None and
                    (not replacement.cleanup_proven() or replacement.process_group_present()))):
            raise RuntimeErrorCode('runtime_stop_incomplete')
        try: shutil.rmtree(self._socket_parent)
        except FileNotFoundError: pass
        except OSError: raise RuntimeErrorCode('runtime_socket_cleanup_incomplete') from None
        if cleanup:
            self.staging.cleanup(archive_digest=self.archive_digest,
                preposition_digest=self._marker.get('preposition_digest'))
        with self._lock: self._state='closed'
