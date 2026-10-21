# SPDX-License-Identifier: AGPL-3.0-only
"""Independent behavioral controls for the private failure record proposal."""
from pathlib import Path
import ast,gc,weakref
import pytest
from tests.node_agent.test_membership_failure_diagnostics import sandbox,args,private_parent,record,node

@pytest.mark.parametrize('diagnostics',[False,True])
def test_failure_metadata_does_not_retain_private_exception_graph(tmp_path,sandbox,monkeypatch,diagnostics):
 refs=[]
 class Marker:pass
 class PrivateFailure(Exception):
  marker: Marker
 def fail(**kwargs):
  marker=Marker();error=PrivateFailure('fixture-private-error');error.marker=marker
  refs.append((weakref.ref(marker),weakref.ref(error)))
  raise error
 monkeypatch.setattr(node,'_renew_lease',fail)
 target=private_parent(tmp_path)/'failure.jsonl' if diagnostics else None
 with pytest.raises(node._EntrypointFailure) as caught:node.run(args(target))
 assert caught.value.exit_status==4
 if diagnostics:assert record(target)['exception_class']=='Other'
 gc.collect()
 assert refs and all(ref() is None for pair in refs for ref in pair),'diagnostic metadata retained private exception/trace locals'

assert node.__file__ is not None
_source=Path(node.__file__).resolve().parents[1]/'mycelium_seed/http.py'
_tree=ast.parse(_source.read_text())
LOCAL_CODES=sorted({call.args[0].value for call in ast.walk(_tree)
 if isinstance(call,ast.Call) and isinstance(call.func,ast.Name) and call.func.id=='SeedHTTPError'
 and call.args and isinstance(call.args[0],ast.Constant) and isinstance(call.args[0].value,str)})
assert LOCAL_CODES
@pytest.mark.parametrize('code',LOCAL_CODES)
def test_known_local_seed_error_code_survives_main_path(tmp_path,sandbox,code):
 _,client,_=sandbox
 def fail(**kwargs):raise node.SeedHTTPError(code)
 client.identity=fail
 target=private_parent(tmp_path)/'failure.jsonl'
 with pytest.raises(node._EntrypointFailure) as caught:node.run(args(target))
 assert caught.value.exit_status==4
 data=record(target)
 assert data.get('seed_code')==code
 assert data['phase']=='initial_identity' and data['exception_class']=='SeedHTTPError'
 assert len(target.read_bytes())<=256

_remote=set()
for _name in ('coordinator.py','state.py'):
 for _call in ast.walk(ast.parse((_source.parent/_name).read_text())):
  if (isinstance(_call,ast.Call) and isinstance(_call.func,ast.Name)
      and _call.func.id in {'SeedCoordinatorError','SeedStateError'} and _call.args
      and isinstance(_call.args[0],ast.Constant) and isinstance(_call.args[0].value,str)
      and _call.args[0].value.startswith('seed_resume_')):
   _remote.add(_call.args[0].value)
REMOTE_MEMBERSHIP_CODES=sorted(_remote|{'seed_member_unknown','seed_message_replayed'})
@pytest.mark.parametrize('code',REMOTE_MEMBERSHIP_CODES)
def test_declared_remote_resume_codes_survive_membership_exchange(tmp_path,sandbox,monkeypatch,code):
 _,client,_=sandbox
 original=node._preflight
 def persisted_preflight(arguments):
  values=list(original(arguments))
  values[-1]={'endpoint_id':'fake','restart_count':1,'membership_generation':2,'incarnation':'fake-r1'}
  return tuple(values)
 def resume_request(self,*,previous_generation,previous_incarnation,endpoint_addrs,peer_profile):return {}
 def fail(*,resume_envelope):raise node.SeedHTTPError(code,status=400)
 monkeypatch.setattr(node,'_preflight',persisted_preflight)
 monkeypatch.setattr(node.NodeMembershipSession,'resume_request',resume_request,raising=False)
 client.resume=fail
 target=private_parent(tmp_path)/'resume.jsonl'
 with pytest.raises(node._EntrypointFailure) as caught:node.run(args(target))
 assert caught.value.exit_status==4
 data=record(target)
 assert data['phase']=='membership_exchange' and data['exception_class']=='SeedHTTPError'
 assert data.get('seed_code')==code and data['http_status']==400

def test_existing_unannotated_preflight_failure_keeps_preflight_phase(tmp_path,sandbox,monkeypatch):
 effects,_,_=sandbox
 def fail(_args):
  effects.append('preflight')
  raise node._EntrypointFailure('node_preflight_failed',2)
 monkeypatch.setattr(node,'_preflight',fail)
 target=private_parent(tmp_path)/'failure.jsonl'
 with pytest.raises(node._EntrypointFailure) as caught:node.run(args(target))
 assert caught.value.exit_status==2 and effects==['preflight']
 assert record(target)['phase']=='preflight'

@pytest.mark.parametrize('diagnostics',[False,True])
def test_actual_main_keeps_public_exit_and_redacts(tmp_path,sandbox,monkeypatch,capsys,diagnostics):
 _,client,_=sandbox
 def fail(**kwargs):raise ValueError('fixture-private-exception-text')
 client.identity=fail
 target=private_parent(tmp_path)/'failure.jsonl' if diagnostics else None
 monkeypatch.setattr(node.sys,'argv',['mycelium_node',*args(target)])
 with pytest.raises(SystemExit) as caught:node.main()
 output=capsys.readouterr()
 assert caught.value.code==4 and output.out=='' and output.err=='node_runtime_failed\n'
 if diagnostics:
  assert target is not None
  assert record(target)['phase']=='initial_identity'
  assert 'fixture-private-exception-text' not in target.read_text()
