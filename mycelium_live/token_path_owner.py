# SPDX-License-Identifier: AGPL-3.0-only
"""Opt-in owner orchestration over independently pinned route authority.

Capability refusal permits fallback only BEFORE registration/bind/start. Once an
attempt exists it is single-use: failures require ordinary owner reconciliation,
never a second start on the legacy path. Receipt terminals do not prove cleanup
or stage liveness. Host provisioning remains separate from this module.
"""
from concurrent.futures import Future
import json
import threading
import time

from mycelium_node.token_admission import EntryAdmissionPolicy
from mycelium_qualification.evidence import canonical_json_bytes, sha256_bytes
from mycelium_router.token_path_authority import ReceiptCursor


def _snapshot(value):
    return json.loads(canonical_json_bytes(value))


class OwnerTokenPath:
    def __init__(self, *, enabled=None, signer, scope_pins, entry_key, mailbox,
                 seal, current, capable, max_new_tokens, stop_token_ids,
                 process_deadline_monotonic_s, lease_deadline_monotonic_s,
                 clock_uncertainty_ms, clock=time.monotonic,
                 unix_ms=lambda: int(time.time()*1000), control_endpoint_id=None):
        from .token_path_activation import token_path_enabled
        self.required = enabled is None and token_path_enabled()
        if enabled is None:
            enabled = self.required
        if type(enabled) is not bool or not all(callable(f) for f in (seal,current,capable)):
            raise ValueError('token_path_owner_configuration_invalid')
        self.enabled = enabled
        if control_endpoint_id is not None and (not isinstance(control_endpoint_id,str) or not control_endpoint_id):
            raise ValueError('token_path_owner_configuration_invalid')
        self.control_endpoint_id = control_endpoint_id
        self.signer, self.mailbox = signer, mailbox
        self.pins, self.entry_key = _snapshot(scope_pins), _snapshot(entry_key)
        self.seal, self.current, self.capable = seal, current, capable
        self.stops = tuple(stop_token_ids)
        self.clock, self.unix_ms = clock, unix_ms
        self.deadline = min(process_deadline_monotonic_s,lease_deadline_monotonic_s)
        self.policy = EntryAdmissionPolicy(scope_pins=self.pins,owner_key=signer.public_key_record(),
            max_new_tokens=max_new_tokens,stop_token_ids=self.stops,
            process_deadline_monotonic_s=process_deadline_monotonic_s,
            lease_deadline_monotonic_s=lease_deadline_monotonic_s,
            clock_uncertainty_ms=clock_uncertainty_ms,clock=clock,unix_ms=unix_ms)

    def prepare(self, *, command_id, request, control, manifest,
                participating_node_ids, stop_token_ids):
        # No network effect or minted authority on the default-OFF path.
        if not self.enabled:
            return None
        nodes = {p['node_id'] for p in self.pins['participants']}
        if (nodes != set(participating_node_ids) or tuple(stop_token_ids) != self.stops
                or self.signer.endpoint_id != self.pins['owner_endpoint_id']
                or self.signer.verification_key_digest != self.pins['owner_key_digest']
                or self.entry_key['verification_key_digest'] != self.pins['entry_key_digest']
                or sha256_bytes(canonical_json_bytes(manifest)) != control['path_digest']):
            raise ValueError('token_path_owner_scope_mismatch')
        if self.current() is not True:
            raise RuntimeError('token_path_grant_stale')
        if not all(self.capable(node) is True for node in sorted(nodes)):
            if self.required:
                raise RuntimeError('token_path_activation_unsupported')
            return None
        request, control, manifest = map(_snapshot,(request,control,manifest))
        # Anchor before wall-clock sampling; never extend immutable ceilings.
        now = self.clock()
        remaining = min(self.deadline-now, control['absolute_deadline_ms']/1000-now)
        if remaining <= 0:
            raise RuntimeError('token_path_grant_expired')
        scope = {**self.pins,'request_id':request['request_id'],'control':control,
            'request_digest':sha256_bytes(canonical_json_bytes(request))}
        body = dict(protocol='mycelium.token_path_grant.v1',scope=scope,
            max_new_tokens=request['max_new_tokens'],stop_token_ids=list(self.stops),
            expires_at_unix_ms=self.unix_ms()+int(remaining*1000))
        envelope = {'statement':body,'signature':self.signer.sign(body)}
        grant = self.policy.admit(envelope,request=request,bound_control=control)
        cursor = ReceiptCursor(grant=grant,entry_key=self.entry_key,clock=self.clock,current=self.current)
        stream = self.mailbox.open_token_progress(command_id=command_id,cursor=cursor,seal=self.seal,
            **({'target_endpoint_id':self.control_endpoint_id} if self.control_endpoint_id is not None else {}))
        return OwnerTokenAttempt(self,command_id,grant,stream,
            dict(request=request,control=control,path_manifest=manifest,token_grant=envelope))


class OwnerTokenAttempt:
    def __init__(self, owner, command_id, grant, stream, payload):
        self.owner,self.command_id,self.grant,self.stream = owner,command_id,grant,stream
        self._payload = canonical_json_bytes(payload)
        self._lock = threading.Lock()
        self._started = False
        self.token_ids = ()
        self.status = None

    def payload(self):
        return json.loads(self._payload)

    def bind_payload(self):
        payload = self.payload()
        control = payload.pop('control')
        return {'request_id':payload['request']['request_id'],'control':control,'token_path':payload}

    def close(self):
        self.owner.mailbox.close_token_progress(self.command_id)

    def run(self, *, send, consume, interrupt=lambda: None, cancelled=lambda: False):
        with self._lock:
            if self._started:
                raise RuntimeError('token_path_grant_replay')
            self._started = True
        result = Future()
        def dispatch():
            try:
                result.set_result(send(self.payload()))
            except BaseException as exc:
                result.set_exception(exc)
        thread = threading.Thread(target=dispatch,name='token-path-owner-start',daemon=True)
        def fence():
            if cancelled():
                raise RuntimeError('request_cancelled')
            self.stream.check()
        try:
            fence()
            thread.start()
            while self.status is None or self.status == 'DECODING':
                fence()
                try:
                    receipt = self.stream.read(timeout=.05)
                except TimeoutError:
                    if not result.done():
                        continue
                    # The producer may have enqueued/ACKed receipts after the
                    # empty read but before command completion. Once complete,
                    # recheck the queue under its authority fence before raising
                    # the ordinary refusal or declaring a missing terminal.
                    try:
                        receipt = self.stream.read(timeout=0)
                    except TimeoutError:
                        fence()
                        result.result()  # preserve native refusal, never retry
                        raise RuntimeError('token_progress_terminal_missing')
                fence()
                self.token_ids += receipt.token_ids
                self.status = receipt.status
                consume(receipt)
            if self.status != 'COMPLETED':
                raise RuntimeError('token_progress_noncompleted_terminal')
            # Progress precedes the ordinary response. Still require the latter;
            # it carries transport completion, not another token source.
            while not result.done():
                fence()
                thread.join(.05)
            fence()
            return result.result()
        except BaseException:
            interrupt()
            raise
        finally:
            self.close()
            if thread.ident is not None:
                thread.join(.1)
