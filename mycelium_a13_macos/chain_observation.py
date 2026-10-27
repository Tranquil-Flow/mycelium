# SPDX-License-Identifier: AGPL-3.0-only
"""Application-owned bootstrap observation before compilation or model work."""
import io,json,tarfile,copy,time,math
from dataclasses import dataclass,field

_reuse_clock = time.time
from .installation_descriptor import verify_installation_descriptor
from .managed_observations import _valid_run_id,collect_managed_hello_observations
from .session_factory import ManagedSessionFactory
from .workflow import owner_channel
from .managed_control import _segment


def release_owner_channel(context):
    """Release a run's ``ManagedControlOwner`` (its ``owner.lock`` flock); safe to repeat, safe on a partial context."""
    owner=getattr(getattr(context,'factory',None),'owner',None)
    close=getattr(owner,'close',None)
    if callable(close):close()


@dataclass
class ObservationContext:
    run_id:str
    deployment_id:str
    members:tuple
    facts:dict
    offers:dict
    factory:ManagedSessionFactory
    mailbox:object
    observations:dict|None=None
    _verified_observations:dict|None=field(default=None, init=False, repr=False)

    def reuse_boot_ids(self, *, channel, run, peers):
        """Reuse only this run's native result, inside its original 600s window.

        Worker retirement does not change the host's run-scoped boot identity.
        A later host reboot still fails the unchanged live qualification hello;
        this is metadata binding, never liveness or serving admission evidence.
        """
        from .managed_observations import _current_binding, _fresh_enough
        try:
            doc=self._verified_observations
            now=_reuse_clock()
            if (doc is None or doc!=self.observations or not math.isfinite(now)
                    or self.factory.owner is not channel
                    or run['run_id']!=self.run_id or run['deployment_id']!=self.deployment_id
                    or doc['run_id']!=self.run_id or doc['deployment_id']!=self.deployment_id
                    or not _fresh_enough(now,doc['captured_at_unix_ms']/1000)):
                raise ValueError()
            rows={r['node_id']:r for r in doc['observations']}
            nodes={p['node_id'] for p in peers}
            if (len(rows)!=len(doc['observations']) or len(nodes)!=len(peers)
                    or nodes!=set(rows) or nodes!=set(self.factory._bindings)):
                raise ValueError()
            for peer in peers:
                node=peer['node_id']
                _current_binding(self.factory,node)
                if (now>=channel.current_member(node)['lease_expires_at']
                        or any(rows[node][k]!=peer[k] for k in ('host_id','boot_id'))):
                    raise ValueError()
            return {n:r['boot_id'] for n,r in rows.items()}
        except Exception:
            raise ValueError('bootstrap_observation_reuse_refused') from None

    def close(self):
        """Local only: cancels this run's owner waits and drops its flock; remote cleanup proof is not touched."""
        release_owner_channel(self)

    def retire_bootstrap(self):
        return {node:self.mailbox.retire_offer(offer) for node,offer in self.offers.items()}

    def collect(self):
        if self.observations is not None:raise ValueError('observations_already_collected')
        self.observations=collect_managed_hello_observations(self.factory,run_id=self.run_id,
            deployment_id=self.deployment_id,node_ids=list(self.facts))
        self._verified_observations=copy.deepcopy(self.observations)
        for offer in self.offers.values():self.mailbox.request_stage_cleanup(offer)
        return {'state':'observed','route_ready':False,**self.observations}


def prepare_observation(app,*,run_id,deployment_id,descriptors,acknowledged):
    """Publish metadata-only offers; targets separately consent to native hello."""
    if (app.installation_role!='owner' or not app.running or acknowledged is not True):
        raise ValueError('bootstrap_owner_consent_required')
    run_id=_valid_run_id(run_id);deployment_id=_segment(deployment_id)
    if not isinstance(descriptors,list) or not 1<=len(descriptors)<=8:
        raise ValueError('bootstrap_descriptors_invalid')
    prepared=app.acquisition.prepared_source();coordinator=app.authority.coordinator
    facts={};members=[]
    # Validate the whole batch before the first offer is published.
    for card in descriptors:
        try:node=card['statement']['target_node_id'];member=coordinator.member(node)
        except Exception:raise ValueError('bootstrap_member_invalid') from None
        if node in facts:raise ValueError('bootstrap_duplicate_member')
        f=verify_installation_descriptor(card,member=member,swarm_id=coordinator.swarm_id,
            run_id=run_id,deployment_id=deployment_id)
        if f['manifest_digest']!=prepared.manifest_digest:raise ValueError('bootstrap_model_mismatch')
        facts[node]=f;members.append(member)
    archive=io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w') as tar:
        payload=json.dumps({'protocol':'mycelium.a13.hello_bootstrap.v1','run_id':run_id,
            'deployment_id':deployment_id,'model_load_authorized':False},sort_keys=True).encode()
        info=tarfile.TarInfo('bootstrap.json');info.size=len(payload);info.mode=0o600
        tar.addfile(info,io.BytesIO(payload))
    factory=ManagedSessionFactory(owner=owner_channel(app.authority),run_id=run_id,
        deployment_id=deployment_id,members=members)
    offers={m['node_id']:app.authority.control_mailbox.publish_offer(target=m,
        run_id=run_id,deployment_id=deployment_id,archive=archive.getvalue()) for m in members}
    return ObservationContext(run_id,deployment_id,tuple(members),facts,offers,factory,app.authority.control_mailbox)
