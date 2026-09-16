"""Private local API automation; SSH carries operator input, not core traffic.

Both roots are REQUIRED env inputs (`A13_OWNER_ROOT`, `A13_REMOTE_ROOT`) precisely
so that no default can point at the historical lane roots or the frozen state tree
`~/Library/Application Support/Mycelium A13 Demo/qualification-47587ebe`.

Every call also fails closed on a LOST CONTROL SURFACE. `native-control.json` is unlinked by
the app on shutdown (`mycelium_a13_macos/app.py:771`) and written at startup (`:294`), so its
absence means "the app is not running" - never "bad credentials". Without this guard
`app_rpc.AppClient.__init__` surfaces a bare `FileNotFoundError` from `lstat`, which chain step
2 reported as `compiler_not_ready` on 2026-09-16 and which cost a physical attempt
(`runs/wp8-02-useful-two-mac/ERRATA-1.md`). The failure now names itself as
`surface_lost:<owner|target>:<state>:daemon=<evidence>`.
"""
from pathlib import Path
import json,os,subprocess,shlex,uuid,sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from app_rpc import AppClient
from round14_surface import require_local,require_remote

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
def owner(path,body=None):
    require_local(OWNER)
    return AppClient(OWNER).call(path,body)

_TARGET_SURFACE_CHECKED=[]
def target(path,body=None):
    # Probe the remote surface once per process, before the first target RPC. It costs one
    # extra ssh round trip, and it must not land inside a timing-sensitive stage window.
    if not _TARGET_SURFACE_CHECKED:
        require_remote(REMOTE,REMOTE_HOST)
        _TARGET_SURFACE_CHECKED.append(True)
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
