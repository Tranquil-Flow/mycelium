# SPDX-License-Identifier: AGPL-3.0-only
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


#: managed-control envelope refusals -> the descriptor check they stand for.
_SIGNED_CODES={'managed_control_message_expired':'installation_descriptor_expired',
    'managed_control_message_from_future':'installation_descriptor_from_future',
    'managed_control_key_pin_mismatch':'installation_descriptor_key_mismatch',
    'managed_control_signature_invalid':'installation_descriptor_signature_invalid'}


class InstallationDescriptorError(ValueError):
    """A bounded ``installation_descriptor_<check>`` code; never echoes card values."""


def _check(condition,code):
    if not condition:raise InstallationDescriptorError('installation_descriptor_'+code)


def verify_installation_descriptor(card,*,member,swarm_id,run_id,deployment_id,now=None):
    """Authenticate current target metadata; never certify its hardware claims.

    Every refusal names the check that failed (``installation_descriptor_``
    + ``expired`` / ``from_future`` / ``member_lease_expired`` /
    ``generation_mismatch`` / ``key_mismatch`` / ``binding_mismatch`` /
    ``signature_invalid`` / ``facts_invalid`` / ``malformed``).
    """
    try:
        _check(len(json.dumps(card,allow_nan=False))<=16384,'malformed')
        key=card['verification_key']
        _check(key['verification_key_digest']==member['verification_key_digest'],'key_mismatch')
        now=time.time() if now is None else now
        _check(now<member['lease_expires_at'],'member_lease_expired')
        try:
            s=_verify_signed(card,expected_protocol=PROTOCOL,expected_key_record=key,
                expected_endpoint_id=member['endpoint_id'],now=now)
        except ManagedControlError as error:
            raise InstallationDescriptorError(_SIGNED_CODES.get(str(error),'installation_descriptor_malformed')) from None
        binding=_binding_from_member(member,run_id,deployment_id)
        _check(set(s)==set(binding)|{'protocol','swarm_id','issued_at','expires_at','facts'},'malformed')
        # A re-admitted member (new generation or incarnation) must describe itself again.
        _check(all(s[k]==binding[k] for k in ('membership_generation','member_incarnation')),'generation_mismatch')
        _check(s['swarm_id']==swarm_id and all(s[k]==v for k,v in binding.items()),'binding_mismatch')
        f=s['facts']
        _check(isinstance(f,dict) and set(f)==_FACT_FIELDS,'facts_invalid')
        _check(f['membership_generation']==member['generation'],'generation_mismatch')
        _check(f['node_id']==member['node_id'] and f['endpoint_id']==member['endpoint_id'],'binding_mismatch')
        _check(f['runtime_backend']==managed_runtime_backend(member.get('peer_class','mac_mlx_iroh'))
            and f['process_transport']=='managed_https' and f['ssh_target'] is None
            and f['ssh_identity_file'] is None and f['route_ready'] is False,'facts_invalid')
        for field in ('staging_root','python_executable','sidecar_binary','endpoint_secret_file','source_root'):
            value=f[field]
            _check(isinstance(value,str) and Path(value).is_absolute() and '..' not in Path(value).parts
                and not any(ord(c)<32 for c in value),'facts_invalid')
        # Iroh identifies the endpoint by its Ed25519 public key, not by the
        # seed control-plane label. The target uses this same pinned key file.
        activation_key=base64.b64decode(key['verification_key'],validate=True)
        _check(len(activation_key)==32,'malformed')
        return {**f,'activation_endpoint_id':activation_key.hex()}
    except InstallationDescriptorError:
        raise
    except (KeyError,TypeError,ValueError,ManagedControlError):
        raise InstallationDescriptorError('installation_descriptor_malformed') from None
