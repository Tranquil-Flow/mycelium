# SPDX-License-Identifier: AGPL-3.0-only
"""Real signed ON provisioning for the local MLX multi-process fixture.

All transport stays in-process pipes or local-only Iroh. These are synthetic
members on one host, never physical qualification. ACKs use a private test owner
and follow verified, durably sealed native receipts.
"""
import json
from pathlib import Path
import queue
import threading
import time

from cryptography.hazmat.primitives import serialization
from mycelium_a13_macos.token_owner import ManagedRouteTokenOwner
from mycelium_a13_macos.managed_control import sign_managed_statement
from mycelium_live.token_path_activation import token_path_enabled
from mycelium_qualification.signing import generate_ed25519_signer


class NativeProgress:
    def __init__(self, session, command_id, cursor, seal, signer):
        self.session, self.command_id, self.cursor = session, command_id, cursor
        self.queue = queue.Queue(maxsize=64)
        self.closed = threading.Event()
        self.error = None
        inbox = session._process.open_progress(command_id=command_id, cursor=cursor,
            deadline_monotonic_s=cursor.grant.deadline_monotonic_s)

        def receive():
            try:
                while not self.closed.is_set():
                    receipt = inbox.read()
                    cursor._fence()
                    seal(receipt.envelope_bytes)
                    cursor._fence()
                    # Publish before ACK can release the native start response.
                    self.queue.put_nowait(receipt)
                    scope = cursor.grant.statement()['scope']
                    now = time.time()
                    ack = dict(protocol='mycelium.managed_control.token_progress_ack.v1',
                        request_id=command_id+'-receipt-'+str(receipt.sequence),
                        command_id=command_id, swarm_id='local-fixture',owner_node_id='local-owner',
                        owner_endpoint_id=signer.endpoint_id,issued_at=now,expires_at=now+60,
                        target_endpoint_id=scope['entry_endpoint_id'],
                        target_node_id=scope['entry_node_id'],target_key_digest=scope['entry_key_digest'],
                        membership_generation=next(p['generation'] for p in scope['participants']
                            if p['node_id']==scope['entry_node_id']),member_incarnation='local-fixture',
                        run_id=scope['run_id'],deployment_id=scope['control']['deployment_id'],
                        grant_digest=cursor.grant.digest,receipt_sequence=receipt.sequence,
                        receipt_digest=receipt.receipt_digest)
                    response = session.send(command_id=command_id+'-ack-'+str(receipt.sequence),
                        command='ack_token_progress',payload={'ack':sign_managed_statement(signer,ack)})
                    if response.get('ok') is not True:
                        raise RuntimeError('local_token_ack_refused:'+str(response.get('error')))
                    if receipt.status != 'DECODING':
                        return
            except BaseException as error:
                if not self.closed.is_set():
                    self.error = error
                    self.queue.put_nowait(error)

        self.thread = threading.Thread(target=receive,name='local-token-progress',daemon=True)
        self.thread.start()

    def check(self):
        self.cursor._fence()
        if self.error is not None:
            raise self.error

    def read(self, *, timeout):
        self.check()
        try:
            value = self.queue.get(timeout=timeout)
        except queue.Empty as error:
            raise TimeoutError('local progress timeout') from error
        if isinstance(value, BaseException):
            raise value
        return value

    def close(self):
        self.closed.set()
        self.session._process.close_progress(self.command_id)
        self.thread.join(5)
        assert not self.thread.is_alive(), 'local receipt thread leaked'


class LocalTokenAuthority:
    def __init__(self, local):
        self.local = local
        self.files = {}
        self.owner = None
        # Option A leaves legacy fixtures unprovisioned when fast mode is OFF.
        # ON still follows the exact signed owner/participant path below.
        if not token_path_enabled():
            return
        self.signer = generate_ed25519_signer(endpoint_id='local-owner')
        self.streams = {}
        self.members = {}
        deadline = time.monotonic()+1800
        lease = time.time()+1800
        for node in local.node_ids:
            identity = generate_ed25519_signer(endpoint_id=node)
            endpoint = identity._public_key_bytes.hex()
            member = dict(node_id=node,endpoint_id=endpoint,control_endpoint_id=endpoint,
                verification_key_digest=identity.verification_key_digest,
                generation=local.endpoints_template[node]['membership_generation'],lease_expires_at=lease)
            self.members[node] = member
            directory = local.root/'token-authority'/node
            directory.mkdir(parents=True,exist_ok=True,mode=0o700)
            key = directory/'endpoint.key'
            key.write_bytes(identity._private_key.private_bytes(serialization.Encoding.Raw,
                serialization.PrivateFormat.Raw,serialization.NoEncryption()))
            key.chmod(0o600)
            # Native signing derives its endpoint from the same private bytes.

            pin = directory/'host-pin.private.json'
            pin.write_text(json.dumps(dict(protocol='mycelium.token_path_host_pin.v1',
                run_id=local.run_plan['run_id'],deployment_id=local.run_plan['deployment_id'],node_id=node,
                owner_endpoint_id=self.signer.endpoint_id,owner_key=self.signer.public_key_record(),
                member={k:member[k] for k in ('node_id','endpoint_id','control_endpoint_id','generation')} |
                    {'verification_key':identity.public_key_record()},
                process_deadline_monotonic_s=deadline,lease_expires_at_unix_ms=int(lease*1000),
                clock_uncertainty_ms=100)))
            pin.chmod(0o600)
            self.files[node]=(key,pin)
        self.owner = ManagedRouteTokenOwner(signer=self.signer,current_member=self.members.__getitem__,
            mailbox=self,evidence_root=local.root/'token-receipts',
            process_deadline_monotonic_s=deadline,clock_uncertainty_ms=100)
        assert self.owner.enabled, 'local fixture must exercise default ON'

    def open_token_progress(self, *, command_id, cursor, seal, target_endpoint_id):
        node = cursor.grant.statement()['scope']['entry_node_id']
        assert target_endpoint_id == self.members[node]['control_endpoint_id']
        stream = NativeProgress(self.local.session_factory.sessions[node],command_id,cursor,seal,self.signer)
        self.streams[command_id]=stream
        return stream

    def close_token_progress(self, command_id, *, cancelled=False):
        stream=self.streams.pop(command_id,None)
        if stream is not None:
            stream.close()
