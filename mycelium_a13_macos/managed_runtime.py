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
    def __init__(self, state_root, *, client, current_member, run_id, deployment_id,
                 staging:TargetStaging, archive:bytes, archive_digest,
                 resource_policy, limits, sidecar_binary, authorize_load,
                 preposition=None, endpoint_secret_file=None, sequence_root=None, _test_observer=None):
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
        self._state='prepared'; self._error=None
        # Consecutive retriable-outbound backoff, bounded at 1.0 s.  Loop-local
        # pacing only; it never gates a disposition or a terminal state.
        self._outbound_retry_backoff=.1
        self._lock=threading.RLock(); self._stop=threading.Event(); self._thread=None
        # Darwin limits Unix socket paths; installed private state paths can
        # exceed that limit. Only sockets are transient: capacity/journals stay
        # in the original durable run root, never the temporary directory.
        temp_base=Path('/private/tmp') if Path('/private/tmp').is_dir() else Path('/tmp')
        self._socket_parent=Path(tempfile.mkdtemp(prefix='myc-a13-',dir=temp_base))
        try:
            self.worker=ManagedWorker(node_id=staging.node_id,run_id=run_id,deployment_id=deployment_id,
                artifact_root=staging.staging_root,socket_root=self._socket_parent/'s',
                capacity_root=self.root,
                sidecar_binary=sidecar_binary,resource_policy=resource_policy,limits=limits,
                membership_current=self._membership_current,authorize_load=authorize_load,
                endpoint_secret_file=endpoint_secret_file,_test_observer=_test_observer)
            self.pump=ConcurrentTargetControlPump(self.root/'pump',client=client,worker=self.worker,
                current_member=self._member,sequence_root=sequence_root)
        except BaseException:
            self._socket_parent.rmdir()
            raise

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

    def _serve(self):
        try:
            while not self._stop.is_set():
                self._member()
                step=self.pump.step()
                # A retrying outbound is a bounded recovery, not a death: back off a
                # little so a down origin is not hammered at the 50 ms poll cadence.
                # Bounded by the envelope's own signed TTL, and native dispatch is
                # unaffected (it runs on the pump's executor, not on this wait).
                delay=.05
                if isinstance(step,dict) and step.get('state')=='outbound_retryable':
                    self._outbound_retry_backoff=min(1.0,max(.1,self._outbound_retry_backoff*2))
                    delay=self._outbound_retry_backoff
                else:
                    self._outbound_retry_backoff=.1
                self._stop.wait(delay)
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
        with self._lock:
            return {'state':self._state,'error_code':self._error,'route_ready':False,
                'worker_running':self.worker.status()['running'],
                'model_load_authorized':self.worker.status()['model_load_authorized']}

    def close(self,*,cleanup=False):
        self._stop.set()
        # Native shutdown first bounds in-flight response waits, then HTTP/pump
        # ownership is released. No lock is held while joining the pump thread.
        self.worker.close()
        thread=self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=15)
            if thread.is_alive(): raise RuntimeErrorCode('runtime_stop_incomplete')
        self.pump.close()
        if self.worker.process_group_present(): raise RuntimeErrorCode('runtime_stop_incomplete')
        try: self._socket_parent.rmdir()
        except FileNotFoundError: pass
        except OSError: raise RuntimeErrorCode('runtime_socket_cleanup_incomplete') from None
        if cleanup:
            self.staging.cleanup(archive_digest=self.archive_digest,
                preposition_digest=self._marker.get('preposition_digest'))
        with self._lock: self._state='closed'
