"""Small application workflows over the canonical A13 authorities.

No frontend, remote executable, model loader, or manufactured qualification.
The caller supplies local consent and an already-built native controller plan.
"""
from __future__ import annotations
import json
import threading
import time

from .authority import DemoAuthority
from .control_session import ManagedControlOwner
from .controller_staging import ManagedControllerStaging
from .managed_control import ManagedControlClient
from .target_app import TargetApplication


def owner_channel(authority: DemoAuthority) -> ManagedControlOwner:
    """Use this installation's existing owner identity and local TLS listener."""
    if (not isinstance(authority,DemoAuthority) or authority.coordinator is None
            or authority.seed_server is None or authority.control_mailbox is None):
        raise ValueError('managed_workflow_owner_unavailable')
    coordinator=authority.coordinator
    client=ManagedControlClient.for_local_tls(
        base_url=authority.seed_server.base_url,signer=coordinator.signer,
        trusted_owner_key_record=coordinator.signer.public_key_record(),
        swarm_id=coordinator.swarm_id,owner_node_id=coordinator.seed_node_id,
        ca_certificate=authority.seed_server.material.ca_certificate)
    return ManagedControlOwner(authority.state_root/'managed-control'/'owner',
        client=client,current_member=coordinator.member)


def publish_controller_offers(controller, *, authority, run_id, deployment_id,
                              max_archive_bytes=32*1024*1024):
    """Publish exact per-peer native archives before canonical prepare/serve.

    Hash/size-check the entire batch before publication. HF prepositions must
    already be bound into the controller; weights never silently fall back to
    an oversized control-channel transfer. Publication is not target consent.
    Returns offers plus the adapter to pass into the canonical controller.
    """
    from physical_inference_qualification import QualificationController
    if (not isinstance(controller,QualificationController)
            or not isinstance(authority,DemoAuthority)
            or authority.coordinator is None or authority.control_mailbox is None
            or type(max_archive_bytes) is not int or not 0<max_archive_bytes<=32*1024*1024
            or not controller.peers
            or any(p.process_transport!='managed_https' for p in controller.peers)):
        raise ValueError('managed_workflow_plan_invalid')
    controller._validate_transfers()
    members=[authority.coordinator.member(peer.node_id) for peer in controller.peers]
    adapter=ManagedControllerStaging(mailbox=authority.control_mailbox,
        members=members,run_id=run_id,deployment_id=deployment_id)
    batch=[]
    for peer,member in zip(controller.peers,members,strict=True):
        archive,_=controller._archive_identity_for_peer(peer)
        if not 0<len(archive)<=max_archive_bytes:
            raise ValueError('managed_workflow_archive_budget_exceeded')
        preposition=controller._preposition_identity_for_peer(peer)
        batch.append((peer.node_id,member,archive,
            None if preposition is None else json.loads(preposition[0])))
    offers={}
    for node,member,archive,preposition in batch:
        offers[node]=authority.control_mailbox.publish_offer(target=member,
            run_id=run_id,deployment_id=deployment_id,archive=archive,preposition=preposition)
    return offers,adapter


def run_target_offer(app:TargetApplication, offer, *, acknowledged, resource_policy,
                     limits, authorize_load, stop_event:threading.Event,
                     cleanup_on_owner_request:bool, _test_observer=None,
                     _test_ca_certificate=None):
    """Run one consented offer; drain and acknowledge its requested cleanup.

    This is blocking and intended for an application's worker thread. It never
    accepts a different offer automatically or retries uncertain model work.
    Caller stop drains the worker but preserves staging for reconciliation.
    Only an explicit owner cleanup request plus the local cleanup opt-in may
    remove this offer's validated staging; identity and journals remain intact.
    """
    if (not isinstance(app,TargetApplication) or not isinstance(stop_event,threading.Event)
            or type(cleanup_on_owner_request) is not bool):
        raise ValueError('managed_workflow_target_invalid')
    if stop_event.is_set():
        raise ValueError('managed_workflow_stopped')
    started=False
    try:
        app.start_work(offer,acknowledged=acknowledged,resource_policy=resource_policy,
            limits=limits,authorize_load=authorize_load,_test_observer=_test_observer,
            _test_ca_certificate=_test_ca_certificate)
        started=True
        while not stop_event.wait(.1):
            if cleanup_on_owner_request:
                requested=app.pending_cleanup(_test_ca_certificate=_test_ca_certificate)
                if requested is not None:
                    if requested!=offer:
                        raise ValueError('managed_workflow_cleanup_offer_mismatch')
                    app.stop_control();started=False
                    ack=app.cleanup_offer(offer,acknowledged=True,
                        _test_ca_certificate=_test_ca_certificate)
                    return {'state':'cleaned','cleanup_ack':ack,'route_ready':False}
            state=app.status()['native_control']
            if state['state']!='running':
                raise ValueError(state.get('error_code') or 'managed_workflow_worker_stopped')
        return {'state':'stopped_staging_retained','route_ready':False}
    finally:
        if started:app.stop_control()


class ManagedServingWorkflow:
    """One owner-controlled, already-compiled run; no implicit model approval."""
    def __init__(self, *, authority, controller, launch):
        self.authority=authority
        self.controller=controller
        self.launch=launch
        self.stop_event=threading.Event()
        self._lock=threading.RLock()
        self._state='prepared'
        self._offers={}
        self._startup=None
        self._error=None

    def status(self):
        with self._lock:
            return {'state':self._state,'error_code':self._error,
                'startup':None if self._startup is None else dict(self._startup),
                # Startup is historical. Current qualification/selection must
                # be read from the live registry API, never cached in this adapter.
                'readiness_source':'registry_api'}

    def stop(self):
        self.stop_event.set()

    def run(self, *, approval, port, on_started=None, activation_event=None):
        """Publish -> target-owned stage -> actual native qualification -> API.

        The existing registry supervisor owns loaded routes, HTTP and drain.
        Every target must independently call run_target_offer with local consent.
        The caller owns the channel/authority and keeps them alive until return.
        """
        if activation_event is not None and not isinstance(activation_event,threading.Event):
            raise ValueError('managed_workflow_activation_invalid')
        from .serving import _validate_execution_approval, _plan_digest, _validate_dynamic_port, _assert_loopback_port_free
        _validate_execution_approval(approval,self.launch.prepared)
        if (approval.operator_plan_digest!=self.launch.operator_plan_digest
                or approval.physical_host_ids!=self.launch.physical_host_ids
                or _plan_digest(self.launch.operator_plan)!=self.launch.operator_plan_digest):
            raise ValueError('managed_workflow_approval_mismatch')
        _validate_dynamic_port(port)
        _assert_loopback_port_free(port)
        if on_started is not None and not callable(on_started):
            raise ValueError('managed_workflow_callback_invalid')
        with self._lock:
            if self._state!='prepared' or self.stop_event.is_set():
                raise ValueError('managed_workflow_not_startable')
            self._state='publishing'
        def started(document):
            with self._lock:
                self._startup=dict(document)
                self._state='api_running'
            if on_started is not None:on_started(dict(document))
        try:
            run=self.controller._run_plan
            offers,adapter=publish_controller_offers(self.controller,authority=self.authority,
                run_id=run['run_id'],deployment_id=run['deployment_id'])
            with self._lock:
                self._offers=offers
                self._state='staging'
            self.controller._staging_adapter=adapter
            self.controller.execute('prepare')
            if activation_event is not None:
                with self._lock:self._state='awaiting_activation'
                expires=min(offer['expires_at'] for offer in self._offers.values())
                while not activation_event.wait(.05):
                    if self.stop_event.is_set():raise ValueError('managed_workflow_stopped')
                    if time.time()>=expires:raise ValueError('managed_workflow_activation_expired')
                if time.time()>=expires:raise ValueError('managed_workflow_activation_expired')
            if self.stop_event.is_set():raise ValueError('managed_workflow_stopped')
            with self._lock:self._state='starting'
            result=self.launch.run(approval=approval,port=port,api_only=True,
                stop_event=self.stop_event,on_started=started)
            with self._lock:self._state='stopped'
            return result
        except Exception as exc:
            import re
            code=str(exc)
            with self._lock:
                self._state='failed'
                self._error=code if re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) else 'managed_workflow_failed'
            raise
        finally:
            # Requests only: targets own cleanup and identity retention. Never
            # label a request as a cleanup acknowledgement or erase a receipt.
            for offer in self._offers.values():
                try:self.authority.control_mailbox.request_stage_cleanup(offer)
                except Exception:pass  # Original failure and private offers remain inspectable.


def prepare_serving_workflow(*, authority, channel, prepared, operator_plan,
                             target_source_roots):
    """Bind a native builder result to current members and this owner's seed.

    Metadata/files only: no model load, publication, process or route starts.
    Model compilation remains the canonical build_qwen_live_route entrypoint,
    with its own explicit model-execution approval. This factory connects its
    result to managed provisioning and API-only serving without manual rebinding.
    """
    from pathlib import Path
    import time
    from .authority import _private_directory, _atomic_private_json
    from .serving import _read_json_object, prepare_registry_launch
    from .managed_transfers import bind_hf_prepositions
    from .session_factory import ManagedSessionFactory
    from mycelium_seed.plan_binding import bind_operator_plan_document
    from physical_inference_qualification import QualificationController, PeerIdentity
    if (not isinstance(authority,DemoAuthority) or authority.coordinator is None
            or not isinstance(channel,ManagedControlOwner)
            or channel.client.swarm_id!=authority.coordinator.swarm_id
            or channel.client.signer.verification_key_digest!=authority.coordinator.signer.verification_key_digest):
        raise ValueError('managed_workflow_owner_unavailable')
    document=_read_json_object(Path(operator_plan),reason='managed_workflow_plan_invalid')
    document['controller']=bind_hf_prepositions(document['controller'],prepared=prepared,
        target_source_roots=target_source_roots)
    coordinator=authority.coordinator
    bound=bind_operator_plan_document(document,signer=coordinator.signer,
        swarm_id=coordinator.swarm_id,seed_node_id=coordinator.seed_node_id,
        members=coordinator.members(),now=time.time())
    c=bound['controller'];run=c['run_plan']
    members=[coordinator.member(peer['node_id']) for peer in c['peers']]
    factory=ManagedSessionFactory(owner=channel,run_id=run['run_id'],
        deployment_id=run['deployment_id'],members=members)
    from .managed_control import _segment
    run_id=_segment(run['run_id'])
    root=_private_directory(authority.state_root/'serving-runs'/run_id,create=True)
    path=root/'operator-plan.json'
    if path.exists() or path.is_symlink():raise ValueError('managed_workflow_plan_already_exists')
    _atomic_private_json(path,bound)
    launch=prepare_registry_launch(prepared=prepared,operator_plan=path,
        private_state_root=root/'registry',seed_state_root=authority._seed_root,session_factory=factory)
    controller=QualificationController(mode='physical',
        peers=[PeerIdentity(**peer) for peer in c['peers']],source_root=Path(c['source_root']),
        transfer_manifest=c['transfer_manifest'],node_transfer_manifests=c.get('node_transfer_manifests'),
        prepositioned_artifacts=c.get('prepositioned_artifacts'),membership_snapshot=c['membership_snapshot'],
        run_plan=run,now=c['now'],session_factory=factory)
    controller._validate_transfers()
    return ManagedServingWorkflow(authority=authority,controller=controller,launch=launch)
