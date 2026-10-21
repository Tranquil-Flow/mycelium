# SPDX-License-Identifier: AGPL-3.0-only
"""Owner journal storage seam, not command execution or authority evidence."""
from pathlib import Path
from types import SimpleNamespace as NS
import json,stat
import pytest
from mycelium_a13_macos.control_session import ManagedControlOwner
from mycelium_a13_macos.authority import _atomic_private_json,_load_private_json,DemoAuthorityError


def test_owner_journal_has_its_own_bounded_budget(tmp_path):
    document={'fixture':'metadata-only','padding':'x'*300000}
    owner=NS(path=tmp_path/'owner.json',_state=document)
    ManagedControlOwner._save(owner)
    assert _load_private_json(owner.path,max_bytes=4*1024*1024)==document
    assert stat.S_IMODE(owner.path.stat().st_mode)==0o600
    with pytest.raises(DemoAuthorityError):_atomic_private_json(tmp_path/'identity.json',document)
    with pytest.raises(DemoAuthorityError):_load_private_json(owner.path)
    before=owner.path.read_bytes()
    owner._state={'padding':'x'*(4*1024*1024)}
    with pytest.raises(DemoAuthorityError):ManagedControlOwner._save(owner)
    assert owner.path.read_bytes()==before



def test_large_signed_intent_journal_reopens_without_resetting_history(tmp_path):
    from test_control_pump import _world
    from test_control_session import owner as make_owner
    from test_managed_control import RUN_ID,DEPLOYMENT_ID
    import mycelium_a13_macos.control_session as control
    with _world(tmp_path/'world') as world:
        root=tmp_path/'journal';managed=make_owner(root,world)
        member=world['authority'].coordinator.member(world['member']['node_id'])
        session=managed.session(node_id=member['node_id'],run_id=RUN_ID,deployment_id=DEPLOYMENT_ID)
        try:
            # Synthetic pending-intent history; no enqueue or native work occurs.
            with managed._lock:
                for i in range(256):
                    command={'protocol':control.CONTROL_COMMAND_PROTOCOL,'command_id':f'fixture-{i}',
                        'run_id':RUN_ID,'deployment_id':DEPLOYMENT_ID,'command':'hello','payload':{}}
                    prepared=control.prepare(managed.client,'enqueue',target=member,native_command=command,
                        sequence=managed._next_sequence(),nonce=f'nonce-{i}',request_id=f'request-{i}',ttl_seconds=60)
                    managed._state['commands'][command['command_id']]={'binding':session.binding,
                        'digest':control._document_digest(command),'enqueue_request':prepared,'enqueue_confirmed':False}
                    managed._state['write_intents']+=1
                managed._save()
            expected=json.loads(managed.path.read_text())
            assert managed.path.stat().st_size>256*1024
        finally:managed.close()
        reopened=make_owner(root,world)
        try:
            assert reopened._state==expected
            assert reopened._state['write_intents']==256
            assert reopened.max_commands==1024
        finally:reopened.close()
