# SPDX-License-Identifier: AGPL-3.0-only
"""Application adapter for a target-owned Mac; never creates a seed."""
import time
from . import a13_test_seams as _seams
from .peer import RemoteMacMember
from .capability import observe_mac
from .managed_classes import managed_runtime_backend

CONSENT = {'cooperative_peer_visibility_acknowledged': True,
           'resource_use_acknowledged': True, 'contribution_enabled': False}


class TargetApplication:
    #: A16: ``overlay(wall_clock=...)`` provider set by the owning app; None = no publication.
    adaptive_overlay = None
    #: A16: ``drain(runtime)`` tick set by the owning app; None = the model is never unloaded.
    adaptive_drain = None
    #: A13 gate test seam: only a joined (member) role honours it; the owner's own local contributor sets False.
    HONOURS_TEST_CLOCK_SEAM = True

    def __init__(self, root, *, _test_peer_factory=None):
        # A13 gate test seam (inert unless MYCELIUM_A13_TEST_SEAMS=1 and an offset is set; see a13_test_seams).
        self.test_clock_offset_seconds = _seams.clock_offset_seconds() if self.HONOURS_TEST_CLOCK_SEAM else None
        #: Whether the gate was open at LAUNCH: only then may the offset be changed at runtime (V1.2).
        self.test_seams_enabled = bool(self.HONOURS_TEST_CLOCK_SEAM and _seams.gate_open())
        factory = _test_peer_factory or RemoteMacMember
        if self.test_clock_offset_seconds is None:
            self.peer = factory(root)
        else:
            _seams.announce(self.test_clock_offset_seconds)
            self.peer = factory(root, clock=_seams.member_clock())
        self.capability = None
        self._runtime = None

    def set_test_clock_offset(self, offset):
        """Runtime form of the clock-skew seam (``None`` clears); only if the gate was open at launch."""
        if not self.test_seams_enabled:
            raise ValueError('test_seams_unavailable')
        value = _seams.checked_offset(offset)
        if value is None:
            self.peer._clock_source = time.time
        else:
            self.peer._clock_source = lambda: time.time() + value
            _seams.announce(value)
        self.test_clock_offset_seconds = value
        return _seams.status(value)

    @property
    def member(self):
        return self.peer.member

    # -- platform hooks (the Windows managed host overrides these) -------------

    def _new_staging(self, run_id, *, node_id, max_archive_bytes, max_disk_bytes,
                     current_member, authorize, artifact_cache_root=None):
        from .managed_staging import TargetStaging
        return TargetStaging(self.peer.state_root/'managed'/run_id,node_id=node_id,
            max_archive_bytes=max_archive_bytes,max_disk_bytes=max_disk_bytes,
            current_member=current_member,authorize=authorize,
            artifact_cache_root=artifact_cache_root)

    def _installed_sidecar(self):
        from .native_runtime import installed_sidecar
        return installed_sidecar()

    def _observe_policy(self):
        from .resources import observe_policy
        return observe_policy()

    def _observe_capability(self):
        return observe_mac(self.peer.state_root)

    def _preposition_cache_root(self, offer):
        return self.peer.state_root/'serving' if offer.get('preposition') is not None else None

    def start(self):
        self.capability = None
        return self.peer.start()

    def pending_stage(self, *, _test_ca_certificate=None):
        from .managed_offers import ManagedOfferClient
        return ManagedOfferClient(self.peer.staging_control_client(
            _test_ca_certificate=_test_ca_certificate)).pending(self.peer.staging_control_snapshot())

    def stage_offer(self, offer, *, max_bytes, max_disk_bytes, authorize,
                    _test_ca_certificate=None):
        """Target-local consent consumes exact signed bytes; never starts work."""
        from .managed_offers import ManagedOfferClient, _offer_valid
        from .managed_control import _binding_from_member, _BINDING_FIELDS
        from .managed_staging import StagingError
        if self._runtime is not None:
            raise StagingError('stage_runtime_active')
        _offer_valid(offer)
        member=self.peer.staging_control_snapshot()
        binding=_binding_from_member(member,offer['run_id'],offer['deployment_id'])
        if any(offer[k]!=binding[k] for k in _BINDING_FIELDS):
            raise StagingError('stage_member_mismatch')
        def current():
            try:
                return (time.time()<offer['expires_at'] and
                    _binding_from_member(self.peer.staging_control_snapshot(),
                        offer['run_id'],offer['deployment_id'])==binding)
            except Exception:return False
        intent={key:offer[key] for key in ('offer_id','archive_digest','archive_bytes','run_id','deployment_id')}
        def admitted(stage_intent):
            return authorize({**stage_intent,**intent}) is True
        if not current() or not callable(authorize) or not admitted({}):
            raise StagingError('stage_not_authorized')
        client=ManagedOfferClient(self.peer.staging_control_client(_test_ca_certificate=_test_ca_certificate))
        archive=client.fetch(member,offer,max_bytes=max_bytes,authorize=lambda:current() and admitted({}))
        staging=self._new_staging(offer['run_id'],
            node_id=member['node_id'],max_archive_bytes=max_bytes,max_disk_bytes=max_disk_bytes,
            current_member=current,authorize=admitted,
            artifact_cache_root=self._preposition_cache_root(offer))
        ack=staging.prepare(archive=archive,archive_digest=offer['archive_digest'],preposition=offer.get('preposition'))
        if not current():raise StagingError('stage_member_mismatch')
        client.acknowledge(self.peer.staging_control_snapshot(),offer,ack)
        return ack

    def pending_cleanup(self, *, _test_ca_certificate=None):
        from .managed_offers import ManagedOfferClient
        return ManagedOfferClient(self.peer.staging_control_client(
            _test_ca_certificate=_test_ca_certificate)).pending_cleanup(self.peer.staging_control_snapshot())

    def cleanup_offer(self,offer,*,acknowledged,_test_ca_certificate=None):
        """Explicit target-local cleanup of one run; identity/evidence survive."""
        from .managed_offers import ManagedOfferClient,MAX_ARCHIVE,_offer_valid
        from .managed_staging import StagingError
        from .managed_control import _binding_from_member,_BINDING_FIELDS
        if acknowledged is not True:raise StagingError('stage_not_authorized')
        if self._runtime is not None:raise StagingError('stage_runtime_active')
        _offer_valid(offer)
        member=self.peer.staging_control_snapshot()
        binding=_binding_from_member(member,offer['run_id'],offer['deployment_id'])
        if any(offer[k]!=binding[k] for k in _BINDING_FIELDS):raise StagingError('stage_member_mismatch')
        client=ManagedOfferClient(self.peer.staging_control_client(_test_ca_certificate=_test_ca_certificate))
        state=client.cleanup_status(member,offer)
        if state['ack'] is not None:return state['ack']
        staging=self._new_staging(offer['run_id'],node_id=member['node_id'],
            max_archive_bytes=MAX_ARCHIVE,max_disk_bytes=MAX_ARCHIVE,
            current_member=lambda:False,authorize=lambda intent:acknowledged is True and
                intent['operation']=='cleanup' and intent['archive_digest']==offer['archive_digest'])
        ack=staging.cleanup(archive_digest=offer['archive_digest'],preposition_digest=offer.get('preposition_digest'))
        client.cleanup_status(self.peer.staging_control_snapshot(),offer,ack=ack)
        return ack

    def _endpoint_identity_file(self, member, supplied=None):
        from pathlib import Path
        from mycelium_node.identity import load_node_signer, NodeIdentityError
        path=self.peer.state_root/'identity'/'node.key'
        if supplied is not None and Path(supplied)!=path:
            raise ValueError('endpoint_identity_path_invalid')
        try:
            signer=load_node_signer(path)
        except NodeIdentityError as error:
            code='endpoint_identity_missing' if error.code=='node_identity_missing' else 'endpoint_identity_invalid'
            raise ValueError(code) from None
        if (signer.endpoint_id!=member['endpoint_id'] or
            signer.verification_key_digest!=member['verification_key_digest']):
            raise ValueError('endpoint_identity_mismatch')
        return path

    def start_control(self,offer,*,acknowledged,resource_policy,max_disk_bytes,
                      _test_observer=None,_test_ca_certificate=None):
        """Explicitly start a bounded control worker with model loading denied."""
        from .worker import WorkerLimits
        return self._start_offer(offer,acknowledged=acknowledged,resource_policy=resource_policy,
            limits=WorkerLimits(max_rss_bytes=512*1024*1024,max_disk_bytes=max_disk_bytes,
                max_wall_seconds=300,max_commands=1000),authorize_load=None,
            _test_observer=_test_observer,_test_ca_certificate=_test_ca_certificate)

    def start_work(self,offer,*,acknowledged,resource_policy,limits,authorize_load,
                   _test_observer=None,_test_ca_certificate=None):
        """Activate a signed offer under an explicit target-local load authorizer.

        Python integration API only: callers must obtain exact execution consent
        before constructing the authorizer. It is consulted for each guarded
        native command; activation itself neither loads nor qualifies a model.
        HTTP payloads cannot supply this callable or expand resource limits.
        """
        from .worker import WorkerLimits
        if acknowledged is not True:raise ValueError('control_consent_required')
        if not callable(authorize_load):raise ValueError('work_load_authorizer_required')
        if not isinstance(limits,WorkerLimits):raise ValueError('work_limits_invalid')
        return self._start_offer(offer,acknowledged=acknowledged,resource_policy=resource_policy,
            limits=limits,authorize_load=authorize_load,_test_observer=_test_observer,
            _test_ca_certificate=_test_ca_certificate)

    def _start_offer(self,offer,*,acknowledged,resource_policy,limits,authorize_load,
                     _test_observer=None,_test_ca_certificate=None):
        max_disk_bytes=limits.max_disk_bytes
        if acknowledged is not True:raise ValueError('control_consent_required')
        if self._runtime is not None:raise ValueError('runtime_already_attached')
        from .managed_offers import ManagedOfferClient,MAX_ARCHIVE
        from .worker import WorkerLimits
        from .managed_control import _binding_from_member
        from .resources import policy_violation
        member=self.peer.staging_control_snapshot()
        endpoint_secret_file=self._endpoint_identity_file(member)
        binding=_binding_from_member(member,offer['run_id'],offer['deployment_id'])
        observe=_test_observer or self._observe_policy
        def admitted(intent):
            return (policy_violation(resource_policy,observe()) is None and
                intent.get('archive_digest')==offer['archive_digest'])
        # No contribution transition, process or native read before exact staging.
        self.stage_offer(offer,max_bytes=MAX_ARCHIVE,max_disk_bytes=max_disk_bytes,
            authorize=admitted,_test_ca_certificate=_test_ca_certificate)
        client=ManagedOfferClient(self.peer.staging_control_client(_test_ca_certificate=_test_ca_certificate))
        archive=client.fetch(member,offer,max_bytes=MAX_ARCHIVE,
            authorize=lambda:admitted({'archive_digest':offer['archive_digest']}))
        sidecar=self._installed_sidecar()
        self.peer.set_contribution_enabled(enabled=True,expected_generation=member['generation'])
        def current():
            try:return _binding_from_member(self.peer.managed_control_snapshot(),offer['run_id'],offer['deployment_id'])==binding
            except Exception:return False
        staging=self._new_staging(offer['run_id'],node_id=member['node_id'],
            max_archive_bytes=MAX_ARCHIVE,max_disk_bytes=max_disk_bytes,current_member=current,authorize=admitted,
            artifact_cache_root=self._preposition_cache_root(offer))
        runtime=self.prepare_runtime(run_id=offer['run_id'],deployment_id=offer['deployment_id'],
            staging=staging,archive=archive,archive_digest=offer['archive_digest'],resource_policy=resource_policy,
            limits=limits,sidecar_binary=sidecar,authorize_load=authorize_load,
            endpoint_secret_file=endpoint_secret_file,preposition=offer.get('preposition'),
            _test_observer=_test_observer,_test_ca_certificate=_test_ca_certificate)
        try:return self.activate_runtime(runtime)
        except Exception:
            runtime.close()
            raise

    def provision_assignment(self, *, prepared, assignment, expected_assignment_digest,
                             authorize, max_artifact_bytes):
        """Consume acquired bytes under exact locally admitted owner intent."""
        from .assignment_provisioning import provision_selected_assignment
        return provision_selected_assignment(prepared=prepared,assignment=assignment,
            expected_assignment_digest=expected_assignment_digest,
            member_binding=self.peer.managed_control_snapshot(),
            current_member=self.peer.managed_control_snapshot,authorize=authorize,
            max_artifact_bytes=max_artifact_bytes,
            runtime_backend=managed_runtime_backend(self.peer.PEER_CLASS))

    def prepare_runtime(self, *, run_id, deployment_id, staging, archive, archive_digest,
                        resource_policy, limits, sidecar_binary, authorize_load,
                        preposition=None, endpoint_secret_file=None,
                        _test_observer=None, _test_ca_certificate=None):
        """Internal admitted-work path; no HTTP caller may supply these objects.

        Identity, current-membership callback, owner pin and control origin all
        come from this target's encrypted join, never the controller process.
        Staging and load consent are still separately checked by their owners.
        """
        from .managed_control import _binding_from_member
        from .managed_runtime import ManagedTargetRuntime
        member=self.peer.managed_control_snapshot()
        endpoint_secret_file=self._endpoint_identity_file(member,endpoint_secret_file)
        binding=_binding_from_member(member,run_id,deployment_id)
        client=self.peer.managed_control_client(_test_ca_certificate=_test_ca_certificate)
        return ManagedTargetRuntime(self.peer.state_root/'managed'/binding['run_id'],
            client=client,current_member=self.peer.managed_control_snapshot,
            run_id=run_id,deployment_id=deployment_id,staging=staging,
            archive=archive,archive_digest=archive_digest,preposition=preposition,
            resource_policy=resource_policy,limits=limits,sidecar_binary=sidecar_binary,
            authorize_load=authorize_load,endpoint_secret_file=endpoint_secret_file,
            sequence_root=self.peer.state_root,_test_observer=_test_observer,
            adaptive_overlay=self.adaptive_overlay, adaptive_drain=self.adaptive_drain)

    def activate_runtime(self, runtime):
        """Attach an already admitted target-local runtime, never a web payload."""
        from .managed_runtime import ManagedTargetRuntime
        if self._runtime is not None:
            raise ValueError('runtime_already_attached')
        member=self.peer.member
        if (not isinstance(runtime,ManagedTargetRuntime) or member is None
            or runtime._binding['target_node_id']!=member.node_id
            or runtime._binding['membership_generation']!=member.generation
            or self.peer.status()['lease_state']!='fresh'):
            raise ValueError('runtime_member_mismatch')
        result=runtime.start()
        self._runtime=runtime
        return result

    def stop_control(self):
        if self._runtime is not None:
            self._runtime.close()
            self._runtime=None
        return {'state':'stopped','identity_retained':True,'route_ready':False}

    def stop(self):
        # Draining must finish while membership and recovery identities remain
        # available. A failed drain must not hide behind a successful UI stop.
        if self._runtime is not None:
            self._runtime.close()
            self._runtime=None
        self.capability = None
        return self.peer.stop()

    def renew(self):
        if self.peer.member is not None:
            return self.peer.renew()
        return self.status()

    def pair_request(self):
        # The HTTP application validates and persists the explicit resource
        # policy before selecting this target-only role.
        return self.peer.pair_request(self.peer._load_consent() or CONSENT)

    def accept_pairing(self, handoff):
        return self.peer.accept_handoff(handoff)

    def report_capability(self):
        status = self.peer.status()
        if status['membership_state'] != 'current' or status['lease_state'] != 'fresh':
            raise ValueError('membership_required')
        observation = self._observe_capability()
        if not observation['backends'] or not observation['precisions']:
            raise ValueError('capability_backend_unavailable')
        result = self.peer.capability_report(**{key: observation[key] for key in (
            'platform', 'architecture', 'memory_bytes', 'available_storage_bytes', 'backends', 'precisions')})
        self.capability = {**observation, 'state': 'self_report_accepted',
            'generation': result['membership_generation'],
            'expires_at': result['report']['message']['expires_at']}
        return dict(self.capability)

    def status(self):
        value = self.peer.status()
        capability = self.capability
        current = (capability is not None and value['membership_state'] == 'current'
            and value['lease_state'] == 'fresh'
            and value['membership_generation'] == capability['generation']
            and capability['expires_at'] > time.time())
        return {**value, 'swarm_created': False, 'member_count': 0,
            'native_control':self._runtime.status() if self._runtime is not None else {'state':'stopped','model_load_authorized':False,'work_admitted':False},
            'capability_state': 'self_report_accepted' if current else 'not_reported',
            'capability': dict(capability) if current else None}
