"""Consented target installation facts, not host proof or execution authority."""
import base64,json
from pathlib import Path
import sys,time
from .managed_classes import managed_runtime_backend
from .managed_control import (_binding_from_member,_segment,sign_managed_statement,
                              _verify_signed,ManagedControlError)
from .native_runtime import installed_sidecar

PROTOCOL='mycelium.a13.installation_descriptor.v1'
_FACT_FIELDS={'node_id','process_transport','ssh_target','ssh_identity_file','staging_root',
    'python_executable','sidecar_binary','endpoint_secret_file','endpoint_id',
    'membership_generation','runtime_backend','source_root','manifest_digest','route_ready'}


def installation_descriptor(app,*,run_id,deployment_id,acknowledged,slice_source=None):
    """Disclose exact local install/cache paths only after explicit local consent.

    The native worker still resolves its own executable and sidecar. No path
    here can authorize a remote command. Host/boot IDs require native hello.

    ``slice_source`` (slice mode, relaxed D-1): ``{'source_root', 'manifest_digest'}``
    for a member that holds no whole snapshot.  ``source_root`` is its empty
    per-run slice root inside its artifact cache (where it will fetch ONLY its
    slice); ``manifest_digest`` is the owner-announced digest of the pinned
    revision.  Every slice byte is later verified against owner-compiled
    digests, and the staging consumer re-hashes every prepositioned file.
    """
    if acknowledged is not True:raise ValueError('installation_disclosure_consent_required')
    run_id=_segment(run_id);deployment_id=_segment(deployment_id)
    actor=app.authority if app.installation_role=='target' else app.local_contributor
    if actor is None or not app.running:raise ValueError('installation_member_required')
    member=actor.peer.staging_control_snapshot()
    if slice_source is None:
        prepared=app.acquisition.prepared_source()
        source_root,manifest_digest=str(prepared.source_root),prepared.manifest_digest
    else:
        if (not isinstance(slice_source,dict) or set(slice_source)!={'source_root','manifest_digest'}
                or not isinstance(slice_source['source_root'],str) or not Path(slice_source['source_root']).is_absolute()
                or not isinstance(slice_source['manifest_digest'],str)
                or not slice_source['manifest_digest'].startswith('sha256:')):
            raise ValueError('installation_slice_source_invalid')
        source_root,manifest_digest=slice_source['source_root'],slice_source['manifest_digest']
    from mycelium_node.durable_membership import load_membership_state
    retained=load_membership_state(actor.peer.state_root)
    binding=_binding_from_member(member,run_id,deployment_id)
    root=actor.peer.state_root/'managed'/run_id/'mycelium-stage'
    facts={'node_id':member['node_id'],'endpoint_id':member['endpoint_id'],
        'membership_generation':member['generation'],'staging_root':str(root),
        'python_executable':str(Path(sys.executable).resolve()),
        'sidecar_binary':str(installed_sidecar()),
        'endpoint_secret_file':str(actor._endpoint_identity_file(member)),
        'process_transport':'managed_https','ssh_target':None,'ssh_identity_file':None,
        'runtime_backend':managed_runtime_backend(member.get('peer_class','mac_mlx_iroh')),
        'source_root':source_root,
        'manifest_digest':manifest_digest,'route_ready':False}
    now=time.time()
    return sign_managed_statement(actor.peer._signer,{'protocol':PROTOCOL,**binding,
        'swarm_id':retained['swarm_id'],'issued_at':now,'expires_at':now+300,'facts':facts})


def verify_installation_descriptor(card,*,member,swarm_id,run_id,deployment_id,now=None):
    """Authenticate current target metadata; never certify its hardware claims."""
    try:
        if len(json.dumps(card,allow_nan=False))>16384:raise ValueError()
        key=card['verification_key']
        if key['verification_key_digest']!=member['verification_key_digest']:raise ValueError()
        now=time.time() if now is None else now
        if now>=member['lease_expires_at']:raise ValueError()
        s=_verify_signed(card,expected_protocol=PROTOCOL,expected_key_record=key,
            expected_endpoint_id=member['endpoint_id'],now=now)
        binding=_binding_from_member(member,run_id,deployment_id)
        if set(s)!=set(binding)|{'protocol','swarm_id','issued_at','expires_at','facts'}:raise ValueError()
        if s['swarm_id']!=swarm_id or any(s[k]!=v for k,v in binding.items()):raise ValueError()
        f=s['facts']
        if set(f)!=_FACT_FIELDS:raise ValueError()
        if (f['node_id']!=member['node_id'] or f['endpoint_id']!=member['endpoint_id']
            or f['membership_generation']!=member['generation'] or f['runtime_backend']!=managed_runtime_backend(member.get('peer_class','mac_mlx_iroh'))
            or f['process_transport']!='managed_https' or f['ssh_target'] is not None
            or f['ssh_identity_file'] is not None or f['route_ready'] is not False):raise ValueError()
        for field in ('staging_root','python_executable','sidecar_binary','endpoint_secret_file','source_root'):
            value=f[field]
            if (not isinstance(value,str) or not Path(value).is_absolute() or '..' in Path(value).parts
                or any(ord(c)<32 for c in value)):raise ValueError()
        # Iroh identifies the endpoint by its Ed25519 public key, not by the
        # seed control-plane label. The target uses this same pinned key file.
        activation_key=base64.b64decode(key['verification_key'],validate=True)
        if len(activation_key)!=32:raise ValueError()
        return {**f,'activation_endpoint_id':activation_key.hex()}
    except (KeyError,TypeError,ValueError,ManagedControlError):
        raise ValueError('installation_descriptor_invalid') from None
