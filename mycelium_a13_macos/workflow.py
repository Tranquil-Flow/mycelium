# SPDX-License-Identifier: AGPL-3.0-only
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

# ONE bounded budget for the boot-id collection, plus its retry schedule.
#
# WHY: round p80r1 (2026-09-20) refused its only route with node_hello_identity_invalid /
# kind='boot_id_mismatch' (physical_inference_qualification.py:1720): the plan's bound value
# boot-615e5402... came from the round's OWN live hello at 11:44:14, while the node's live value at
# 11:56:46 was boot-424573b4... -- the node's identity moved AFTER the round observed it, and the
# app-side refresh that exists to prevent exactly this never fired. Its collection fired ONCE, here,
# early, and raised ManagedObservationsError('managed_control_response_timeout') because the node was
# not answering on this run yet (app log: "boot_id_observation_unavailable
# ManagedObservationsError('managed_control_response_timeout')"). It has never once succeeded in any
# round of this program (scripts/a13_operator/bootid_guard.py: the success line was never observed).
#
# WHAT A REFUSAL MEANS: not a verdict about the run. The producer refuses when the control channel is
# not up for THIS owner and run yet, and a node that has not migrated onto this run's identities still
# answers with the previous ones. So the collection is retried inside this one budget, with backoff.
#
# BOUNDED, AND INERT AT THE END: the budget bounds the WAITING only -- no per-command timeout or
# deadline constant is widened (managed_observations._DEFAULT_TIMEOUT_SECONDS stays 30.0), and 180 s is
# small against the round's own 2100 s chain wait (scripts/a13_operator/chain-preview80e.sh). When it
# is exhausted the outcome is byte-for-byte the previous inert default: the same ManagedObservationsError
# still reaches the guard below and the binding is left without observations.
_BOOT_ID_OBSERVATION_BUDGET_SECONDS=180.0
_BOOT_ID_OBSERVATION_INITIAL_RETRY_SECONDS=2.0
_BOOT_ID_OBSERVATION_MAX_RETRY_SECONDS=30.0
# WHICH REFUSALS ARE WORTH WAITING FOR: only the ones the passage of time can fix. A refusal that is a
# verdict about THIS caller's own inputs -- a malformed or stale run id, an unusable deployment id, a
# factory that does not match them, no nodes, a clock that moved -- is answered the same way forever, so
# the inert default is reached at once instead of spending the budget on it. Measured: the pinned env's
# test_serving_workflow.py is refused with managed_observation_run_id_invalid, and retrying that turned a
# 2.7 s file into minutes. Everything the NODE or the CHANNEL produces stays retryable, because that is
# exactly the p80r1 shape: the node was still starting and did not answer for this run yet.
#
# managed_observation_captured_stale IS RETRYABLE, and it was measured rather than assumed: the producer
# reads `now` at entry (managed_observations.py:176) and `captured` at exit (:262) and admits only
# -60 <= now - captured <= 600 (:85-87, :40-41). `captured` is read later from the same clock, so the only
# failing branch is `captured - now > 60`, i.e. THIS ATTEMPT took longer than 60 s -- usually because a
# member's hello ran to its own 30 s budget while the node was still coming up. A retry re-reads `now` at
# its own entry, so every attempt gets a NEW window and waiting can genuinely change the answer.
_BOOT_ID_OBSERVATION_TERMINAL_CODES=frozenset((
    'managed_observation_clock_invalid',
    'managed_observation_run_id_invalid',
    'managed_observation_run_id_stale',
    'managed_observation_deployment_invalid',
    'managed_observation_factory_invalid',
    'managed_observation_factory_scope_mismatch',
    'managed_observation_nodes_invalid',
))
# Both are plain module attributes so a test can replace the wait and the clock; the retry schedule is
# then pinned deterministically (tests/a13_macos/test_bootid_bounded_observation_wait.py) without the
# suite ever sleeping.
_boot_id_observation_clock=time.monotonic
_boot_id_observation_wait=time.sleep


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
        self.node_slice_plans=None
        self._progress_identity=None
        self._challenge_request=None
        self._challenge_limit=0
        self._challenge_count=0

    def bind_progress_identity(self, *, run_id, deployment_id, epoch):
        """Private compile authority; called before durable compile publication."""
        if (not isinstance(run_id,str) or not run_id or not isinstance(deployment_id,str)
                or not deployment_id or type(epoch) is not int or epoch < 0):
            raise ValueError('managed_progress_identity_invalid')
        with self._lock:
            if self._state!='prepared' or self._progress_identity is not None:
                raise ValueError('managed_progress_identity_reused')
            self._progress_identity=(run_id,deployment_id,epoch)

    def _challenge_event(self, deployment_id, request_id, kind, index, limit):
        # Only the supervisor's verified-route sink supplies token indices.
        # Never call a user callback, do RPC, or retain token values under this lock.
        import re
        with self._lock:
            if (self.stop_event.is_set() or self._state!='starting'
                    or self._progress_identity is None
                    or deployment_id!=self._progress_identity[1]
                    or type(request_id) is not str
                    or len(request_id)!=40
                    or re.fullmatch(r'startup-[0-9a-f]{32}',request_id) is None
                    or type(limit) is not int or not 0 < limit <= 4096):
                return
            if kind=='begin' and index == -1:
                if self._challenge_request is None:
                    self._challenge_request=request_id
                    self._challenge_limit=limit
                return
            if (kind=='token' and request_id==self._challenge_request
                    and limit==self._challenge_limit and type(index) is int
                    and index==self._challenge_count and index<limit):
                self._challenge_count+=1

    def _startup_failure(self, deployment_id, code):
        """Publish refusal before potentially slow remote teardown, never readiness."""
        import re
        with self._lock:
            if (self._state != 'starting' or self.stop_event.is_set()
                    or deployment_id != self.controller._run_plan.get('deployment_id')):
                return
            self._state = 'failed'
            self._error = (code if isinstance(code, str)
                           and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', code)
                           else 'startup_route_unavailable')
            self._challenge_request = None
            self._challenge_count = 0

    def status(self):
        with self._lock:
            return {'state':self._state,'error_code':self._error,
                'startup':None if self._startup is None else dict(self._startup),
                'challenge_progress':({'verified_token_count':self._challenge_count}
                    if self._state=='starting' and not self.stop_event.is_set()
                    and self._challenge_count else None),
                # Startup is historical. Current qualification/selection must
                # be read from the live registry API, never cached in this adapter.
                'readiness_source':'registry_api'}

    def stop(self):
        # The lock is also the serving-announcement commit boundary.  A stop
        # completed before that commit cannot be overtaken by stale startup.
        with self._lock:
            self.stop_event.set()
            self._challenge_request=None
            self._challenge_count=0

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
                if self.stop_event.is_set() or self._state!='starting':
                    return
                self._startup=dict(document)
                self._state='api_running'
            if on_started is not None:on_started(dict(document))
        try:
            run=self.controller._run_plan
            if (self._progress_identity is not None
                    and (run.get('run_id'),run.get('deployment_id'))!=self._progress_identity[:2]):
                raise ValueError('managed_progress_identity_changed')
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
                stop_event=self.stop_event,on_started=started,
                on_startup_failure=self._startup_failure,
                **({'on_challenge_event':self._challenge_event}
                   if self._progress_identity is not None else {}))
            with self._lock:
                if self._state != 'failed':self._state='stopped'
            return result
        except Exception as exc:
            import re
            code=str(exc)
            with self._lock:
                if self._state != 'failed':
                    self._state='failed'
                    self._error=code if re.fullmatch(r'[a-z][a-z0-9_]{0,79}',code) else 'managed_workflow_failed'
            raise
        finally:
            # Requests only: targets own cleanup and identity retention. Never
            # label a request as a cleanup acknowledgement or erase a receipt.
            for offer in self._offers.values():
                try:self.authority.control_mailbox.request_stage_cleanup(offer)
                except Exception:pass  # Original failure and private offers remain inspectable.


def bind_member_artifacts(controller, *, prepared, target_source_roots, slice_plan=None):
    """Remove model bytes from the per-node archives; prepositions replace them.

    Without a slice plan every member binds the whole pinned snapshot file it
    already holds (``bind_hf_prepositions``).  With a slice plan (relaxed D-1,
    Qwen3 demo) each member binds only ITS verified slice files under the slice
    root it disclosed (``target_source_roots`` then holds slice roots), and the
    per-member node plans are returned so the owner can hand each member its own
    plan (pinned by ``node_plan_digest``) before publishing offers.
    Returns ``(controller, node_plans | None)``.
    """
    if slice_plan is None:
        from .managed_transfers import bind_hf_prepositions
        return bind_hf_prepositions(controller,prepared=prepared,
            target_source_roots=target_source_roots),None
    from .slice_acquisition import bind_slice_prepositions,node_slice_plan
    node_plans={peer['node_id']:node_slice_plan(slice_plan,peer['node_id'])
                for peer in controller['peers']}
    return bind_slice_prepositions(controller,node_plans=node_plans,
        target_slice_roots=target_source_roots),node_plans


def prepare_serving_workflow(*, authority, channel, prepared, operator_plan,
                             target_source_roots, slice_plan=None):
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
    from .session_factory import ManagedSessionFactory
    # Imported ABOVE the try that uses it. A function-local import written BELOW its use makes the name
    # local for the whole scope, so the call site raised UnboundLocalError on every round and the except
    # reported it as an expected unavailability (soak04, 2026-09-19: the collection had never once run,
    # in any bundle, while every source check and digest compare said the change had shipped).
    from .managed_observations import ManagedObservationsError, collect_managed_hello_observations
    from mycelium_seed.plan_binding import bind_operator_plan_document
    from physical_inference_qualification import QualificationController, PeerIdentity
    if (not isinstance(authority,DemoAuthority) or authority.coordinator is None
            or not isinstance(channel,ManagedControlOwner)
            or channel.client.swarm_id!=authority.coordinator.swarm_id
            or channel.client.signer.verification_key_digest!=authority.coordinator.signer.verification_key_digest):
        raise ValueError('managed_workflow_owner_unavailable')
    document=_read_json_object(Path(operator_plan),reason='managed_workflow_plan_invalid')
    document['controller'],node_plans=bind_member_artifacts(document['controller'],prepared=prepared,
        target_source_roots=target_source_roots,slice_plan=slice_plan)
    coordinator=authority.coordinator
    # Collect the node hello observations BEFORE the binding, so the binding can prefer a LIVE boot id
    # over the one the plan document recorded. Measured 2026-09-19: soak03's plan carried
    # boot-af00774d... while the node's hello said boot-a0423521..., because every round relaunches the
    # apps so every round the boot ids move -- a capture-vs-use race that made qualification a coin flip
    # (7 qualified / 7 unavailable).
    #
    # WHY THIS ORDER IS SAFE (checked, not assumed): bind_operator_plan_document does not touch run_plan --
    # it sets the snapshot fields, controller['now'], and controller['peers'][].boot_id -- so the factory's
    # run_id/deployment_id are the same either side; and `members` is built from the LIVE coordinator in
    # both places, so the binding cannot change whose identity is signed.
    #
    # AND WHY IT CANNOT HURT: the nodes may not be talking yet at plan-compile time. If the producer
    # refuses -- ManagedObservationsError is its ONLY refusal type: no session, stale run id, malformed
    # or non-distinct hello -- the binding is left without observations, which is its inert default, and
    # the round proceeds byte-for-byte as it did before this change.
    #
    # AND WHAT MUST NOT BE INERT: a programming error. Catching bare Exception here hid a function-local
    # import bug for an entire session: UnboundLocalError was reported as boot_id_observation_unavailable,
    # i.e. as though the nodes were simply not talking, so no round log ever disagreed. Only the
    # producer's own refusal is an expected condition. Anything else is a defect and must be loud.
    observed_boot_ids=None
    try:
        document_peers=[peer for peer in document['controller']['peers'] if isinstance(peer,dict)]
        pre_run=document['controller']['run_plan']
        pre_factory=ManagedSessionFactory(owner=channel,run_id=pre_run['run_id'],
            deployment_id=pre_run['deployment_id'],
            members=[coordinator.member(peer['node_id']) for peer in document_peers])
        # BOUNDED RETRY (p80r1, 2026-09-20 -- see _BOOT_ID_OBSERVATION_BUDGET_SECONDS above for the
        # measurement). This fired once and timed out, so the peers kept the values the plan document
        # carried; the node's identity then moved and qualification refused them. One attempt is not a
        # measurement of the run: it is a measurement of how early the owner asked. The retry spends ONE
        # bounded budget with backoff, and the only thing it catches is the producer's own refusal --
        # never a programming error, which must stay loud AND loud in its own vocabulary.
        deadline=_boot_id_observation_clock()+_BOOT_ID_OBSERVATION_BUDGET_SECONDS
        delay=_BOOT_ID_OBSERVATION_INITIAL_RETRY_SECONDS
        while True:
            try:
                observed=collect_managed_hello_observations(pre_factory,run_id=pre_run['run_id'],
                    deployment_id=pre_run['deployment_id'],
                    node_ids=[peer['node_id'] for peer in document_peers])
                break
            except ManagedObservationsError as exc:
                if exc.code in _BOOT_ID_OBSERVATION_TERMINAL_CODES:
                    raise  # A verdict about this run's own inputs: waiting cannot change it.
                remaining=deadline-_boot_id_observation_clock()
                if remaining<=0:raise  # Budget spent: the outcome is exactly today's inert default.
                _boot_id_observation_wait(min(delay,remaining))
                delay=min(_BOOT_ID_OBSERVATION_MAX_RETRY_SECONDS,delay*2)
        observed_boot_ids={item['node_id']:item['boot_id'] for item in (observed.get('observations') or [])
            if isinstance(item,dict) and isinstance(item.get('node_id'),str)
            and isinstance(item.get('boot_id'),str) and item['boot_id']}
        print('boot_id_observations_collected nodes=%d' % len(observed_boot_ids),flush=True)
    except ManagedObservationsError as exc:
        print('boot_id_observation_unavailable %r' % (exc,),flush=True)
        observed_boot_ids=None
    bound=bind_operator_plan_document(document,signer=coordinator.signer,
        swarm_id=coordinator.swarm_id,seed_node_id=coordinator.seed_node_id,
        members=coordinator.members(),now=time.time(),observed_boot_ids=observed_boot_ids)
    # The plan's controller field set is CLOSED (mycelium_physical_runner/config.py:40-54): storing the
    # refresh inside the plan made load_operator_plan refuse the document outright (plan_unknown_field),
    # so the audit lives HERE, in the round log, where the round that proves the boot-id fix fires can
    # also show which peers it moved.
    _before={peer.get('node_id'):peer.get('boot_id') for peer in document['controller'].get('peers') or []
        if isinstance(peer,dict)}
    _moved={peer.get('node_id'):[_before.get(peer.get('node_id')),peer.get('boot_id')]
        for peer in bound['controller'].get('peers') or []
        if isinstance(peer,dict) and peer.get('boot_id')!=_before.get(peer.get('node_id'))}
    if _moved:print('boot_id_refreshed %s' % json.dumps(_moved,sort_keys=True,default=str),flush=True)
    c=bound['controller'];run=c['run_plan']
    members=[coordinator.member(peer['node_id']) for peer in c['peers']]
    factory=ManagedSessionFactory(owner=channel,run_id=run['run_id'],
        deployment_id=run['deployment_id'],members=members)
    from .managed_control import _segment
    run_id=_segment(run['run_id'])
    root=_private_directory(authority.state_root/'serving-runs'/run_id,create=True)
    from mycelium_live.token_path_activation import token_path_enabled
    if token_path_enabled():
        from .token_owner import ManagedRouteTokenOwner
        token_deadline=time.monotonic()+1800
        factory.token_path_owner_factory=lambda:ManagedRouteTokenOwner(
            signer=coordinator.signer,current_member=coordinator.member,mailbox=authority.control_mailbox,
            evidence_root=root/'token-receipts',process_deadline_monotonic_s=token_deadline)
    path=root/'operator-plan.json'
    if path.exists() or path.is_symlink():raise ValueError('managed_workflow_plan_already_exists')
    _atomic_private_json(path,bound)
    # Each managed node runs its own class's stage runtime (mlx on macOS, numpy
    # on Windows); the plan must assign exactly that runtime per node.
    from .managed_classes import managed_runtime_backend
    node_runtime_backends={member['node_id']:managed_runtime_backend(member['peer_class'])
        for member in members}
    launch=prepare_registry_launch(prepared=prepared,operator_plan=path,
        private_state_root=root/'registry',seed_state_root=authority._seed_root,session_factory=factory,
        node_runtime_backends=node_runtime_backends)
    controller=QualificationController(mode='physical',
        peers=[PeerIdentity(**peer) for peer in c['peers']],source_root=Path(c['source_root']),
        transfer_manifest=c['transfer_manifest'],node_transfer_manifests=c.get('node_transfer_manifests'),
        prepositioned_artifacts=c.get('prepositioned_artifacts'),membership_snapshot=c['membership_snapshot'],
        run_plan=run,now=c['now'],session_factory=factory)
    controller._validate_transfers()
    workflow=ManagedServingWorkflow(authority=authority,controller=controller,launch=launch)
    #: Slice mode: each member's own node plan, handed out before offers publish.
    workflow.node_slice_plans=node_plans
    return workflow
