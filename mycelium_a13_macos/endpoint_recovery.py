"""Explicit signed relocation of a retained seed; never key/membership reset."""
import math
from .authority import _atomic_private_json
from mycelium_internet.bootstrap import canonical_https_origin
from mycelium_qualification.evidence import canonical_json_bytes
from mycelium_qualification.signing import build_ed25519_verifier

_PROTOCOL='mycelium.a13.endpoint_recovery.v1'
_FIELDS={'protocol','swarm_id','seed_node_id','seed_key_digest','previous_origin','public_origin','issued_at','expires_at'}


def endpoint_card(*,signer,swarm_id,seed_node_id,previous_origin,public_origin,now):
    previous=canonical_https_origin(previous_origin);new=canonical_https_origin(public_origin)
    if previous==new:raise ValueError('endpoint_unchanged')
    statement={'protocol':_PROTOCOL,'swarm_id':swarm_id,'seed_node_id':seed_node_id,
        'seed_key_digest':signer.verification_key_digest,'previous_origin':previous,
        'public_origin':new,'issued_at':now,'expires_at':now+300}
    return {'statement':statement,'signature':signer.sign(statement)}


def verify_endpoint_card(card,*,membership,now):
    try:
        if not isinstance(card,dict) or set(card)!={'statement','signature'}:raise ValueError()
        if len(canonical_json_bytes(card))>8192:raise ValueError()
        s=card['statement']
        if not isinstance(s,dict) or set(s)!=_FIELDS or s['protocol']!=_PROTOCOL:raise ValueError()
        if any(s[k]!=membership[k] for k in ('swarm_id','seed_node_id','seed_key_digest')):raise ValueError()
        old=canonical_https_origin(s['previous_origin']);new=canonical_https_origin(s['public_origin'])
        if old==new or membership['seed_url'] not in (old,new):raise ValueError()
        if any(type(t) not in (int,float) or not math.isfinite(t) for t in (now,s['issued_at'],s['expires_at'])):raise ValueError()
        if not s['issued_at']-30<=now<s['expires_at'] or not 0<s['expires_at']-s['issued_at']<=300:raise ValueError()
        verify=build_ed25519_verifier(membership['seed_key_records'])
        if card['signature'].get('verification_key_digest')!=membership['seed_key_digest'] or not verify(canonical_json_bytes(s),card['signature']):raise ValueError()
        return new
    except (KeyError,TypeError,ValueError,AttributeError):raise ValueError('endpoint_recovery_invalid') from None


def relocate_authority(authority,*,public_origin,port,acknowledged):
    """Stop/reopen only this app's seed on a newly approved canonical origin."""
    from .authority import DemoAuthorityError
    with authority._lock:
        authority._require_started()
        old=authority._network_configuration()
        if acknowledged is not True or old is None or authority.coordinator is None:
            raise DemoAuthorityError('network_recovery_requires_retained_authority')
        origin=canonical_https_origin(public_origin)
        if type(port) is not int or not 49152<=port<=65535:raise DemoAuthorityError('network_configuration_invalid')
        card=endpoint_card(signer=authority.coordinator.signer,swarm_id=authority.coordinator.swarm_id,
            seed_node_id=authority.coordinator.seed_node_id,previous_origin=old['public_origin'],
            public_origin=origin,now=authority._clock())
        authority.stop()
        _atomic_private_json(authority.state_root/'network.json',{**old,'public_origin':origin,'port':port})
        # Do not silently restore an obsolete network address on partial failure.
        # Identity, SQLite journals and the recovery card remain inspectable.
        _atomic_private_json(authority.state_root/'endpoint-recovery.json',card)
        authority.start()
        return card


def reconnect_peer(peer,*,card,acknowledged):
    """Verify the same seed at its new TLS address before persisting relocation."""
    from mycelium_node.durable_membership import load_membership_state,save_membership_state
    with peer._lock:
        if acknowledged is not True:raise ValueError('endpoint_recovery_consent_required')
        membership=load_membership_state(peer.state_root)
        if membership is None:raise ValueError('endpoint_recovery_requires_membership')
        new=verify_endpoint_card(card,membership=membership,now=peer._clock())
        client=peer._make_client(membership={**membership,'seed_url':new})
        identity=client.identity(now=peer._clock)
        if (identity.get('seed_url')!=new or identity.get('swarm_id')!=membership['swarm_id']
            or identity.get('seed_node_id')!=membership['seed_node_id']):raise ValueError('endpoint_seed_identity_mismatch')
        peer.stop()
        save_membership_state(peer.state_root,{**membership,'seed_url':new})
        _atomic_private_json(peer.state_root/'endpoint-recovery.json',card)
        return peer.start()


def refresh_endpoint_card(authority,*,acknowledged,previous_origin=None):
    """Reissue to the current seed address for an owner-confirmed old origin."""
    from .authority import _load_private_json
    with authority._lock:
        authority._require_started()
        if acknowledged is not True:raise ValueError('endpoint_recovery_consent_required')
        old=_load_private_json(authority.state_root/'endpoint-recovery.json')
        network=authority._network_configuration()
        c=authority.coordinator
        if old is None or network is None or c is None:
            raise ValueError('endpoint_recovery_requires_retained_authority')
        membership={'swarm_id':c.swarm_id,'seed_node_id':c.seed_node_id,
            'seed_url':network['public_origin'],'seed_key_digest':c.signer.verification_key_digest,
            'seed_key_records':[c.signer.public_key_record()]}
        # The old signature is checked at its original issue time only to
        # authenticate the persisted transition; the newly issued card gets
        # its own normal short validity window for the recipient.
        verify_endpoint_card(old,membership=membership,now=old['statement']['issued_at'])
        if old['statement']['public_origin']!=network['public_origin']:
            raise ValueError('endpoint_recovery_origin_changed')
        card=endpoint_card(signer=c.signer,swarm_id=c.swarm_id,seed_node_id=c.seed_node_id,
            previous_origin=(old['statement']['previous_origin'] if previous_origin is None
                             else canonical_https_origin(previous_origin)),public_origin=network['public_origin'],
            now=authority._clock())
        _atomic_private_json(authority.state_root/'endpoint-recovery.json',card)
        return card
