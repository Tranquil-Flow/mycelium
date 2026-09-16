"""Private local API automation; SSH carries operator input, not core traffic.

Both roots are REQUIRED env inputs (`A13_OWNER_ROOT`, `A13_REMOTE_ROOT`) precisely
so that no default can point at the historical lane roots or the frozen state tree
`~/Library/Application Support/Mycelium A13 Demo/qualification-47587ebe`.
"""
from pathlib import Path
import json,os,subprocess,shlex,uuid,sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from app_rpc import AppClient

def _required(name):
    value=os.environ.get(name)
    if not value or not value.startswith('/'):
        raise SystemExit('a13_operator_env_required:'+name)
    return value

def _required_tag(name):
    value=os.environ.get(name)
    if not value:
        raise SystemExit('a13_operator_env_required:'+name)
    return value

OWNER=Path(_required('A13_OWNER_ROOT'))
REMOTE=_required('A13_REMOTE_ROOT')
REMOTE_HOST=os.environ.get('A13_REMOTE_HOST','mycelium-laptop')
PREVIEW=_required_tag('A13_TARGET_PREVIEW')
ROUND=_required_tag('A13_RPC_ROUND')
PRIVATE=OWNER/ROUND;PRIVATE.mkdir(mode=0o700,parents=True,exist_ok=True)
def owner(path,body=None):return AppClient(OWNER).call(path,body)
def target(path,body=None):
    token=uuid.uuid4().hex;local=PRIVATE/(token+'.json');remote=REMOTE+'/target/'+ROUND
    def run(args):
        x=subprocess.run(args,capture_output=True,text=True,timeout=330)
        if x.returncode:raise RuntimeError(x.stderr[-1200:] or x.stdout[-1200:])
        return x
    run(['ssh','-o','BatchMode=yes',REMOTE_HOST,shlex.join(['/bin/mkdir','-p','-m','700',remote])])
    args=['/usr/bin/python3',REMOTE+'/'+PREVIEW+'/app_rpc.py','--root',REMOTE+'/target','--path',path,'--output',remote+'/'+token+'-out.json']
    if body is not None:
        local.write_text(json.dumps(body));local.chmod(0o600)
        run(['scp',str(local),REMOTE_HOST+':'+remote+'/'+token+'.json'])
        args+=['--body',remote+'/'+token+'.json']
    run(['ssh','-o','BatchMode=yes',REMOTE_HOST,shlex.join(args)])
    run(['scp',REMOTE_HOST+':'+remote+'/'+token+'-out.json',str(local)])
    return json.loads(local.read_text())
