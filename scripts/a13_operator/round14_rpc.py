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
import json,os,socket,subprocess,shlex,time,urllib.error,uuid,sys
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
_TARGET_SURFACE_CHECKED=[]
_AWAKE=[]
_TARGET_HANDSHAKE_MARKERS=('Timed out','timed out','TimeoutError','socket.timeout')
_TIMEOUT_MARKERS=('timed out','Timed out','TimeoutError','socket.timeout')

def _busy_reason(exc):
    """Return a short reason string when `exc` means 'the app is ALIVE but BUSY', else None.

    The 63r2 shape, read off the real traceback (`round63r2-attempt.log`): `app_rpc.py:15`'s
    `timeout=10` expired inside `urllib.request.do_open` at `r=h.getresponse()` and surfaced as a
    bare `TimeoutError`. urllib wraps ONLY a failed `h.request()` SEND in `URLError`, so a
    send-phase timeout arrives as `URLError(<timeout>)` - accepted here too, because the app is
    just as busy either way. Nothing else is accepted: a missing surface, a refused connection or
    a bad credential must still fail closed rather than be retried for the whole deadline.
    """
    if isinstance(exc,(TimeoutError,socket.timeout)):
        return repr(exc)
    if isinstance(exc,urllib.error.URLError) and any(m in str(exc.reason) for m in _TIMEOUT_MARKERS):
        return repr(exc)
    return None


class HandshakeTimeout(RuntimeError):
    """The app's private surface exists but did not answer the native-session handshake in time.

    Distinct from `surface_lost` (the app is not running): here the app IS running and simply
    needs longer than `app_rpc.py`'s fixed 10 s handshake timeout - which cannot be raised,
    because that file is pinned byte-identical to the read-only lane source.
    """


def _client(root):
    """Build an AppClient, tolerating a BUSY-BUT-ALIVE app.

    Run 63r2 (2026-09-16): the 7B compile wrote its plan at 18:18:26Z, chain step 2's first call
    hit `app_rpc.py:15`'s `timeout=10` at 18:18:26 and died at 18:18:36 - while the app answered
    /api/quit normally seconds later and both daemons exited 0. The HANDSHAKE is therefore
    retried, bounded and logged. The API call itself is NEVER retried: a POST may be
    non-idempotent, and the failure mode being handled happens before any API call is made.
    """
    deadline = time.monotonic() + float(os.environ.get('A13_HANDSHAKE_DEADLINE_S','180'))
    sleep_s = float(os.environ.get('A13_HANDSHAKE_RETRY_SLEEP_S','2'))
    attempts = 0
    while True:
        try:
            return AppClient(root)
        except Exception as exc:
            reason=_busy_reason(exc)
            if reason is None:
                raise
            attempts += 1
            if time.monotonic() >= deadline:
                raise HandshakeTimeout('handshake_timeout:%s:attempts=%d:%s'%(root,attempts,reason)) from None
            print('handshake_retry root=%s attempt=%d %s'%(root,attempts,reason),flush=True)
            time.sleep(sleep_s)


def owner(path,body=None):
    require_local(OWNER)
    return _client(OWNER).call(path,body)

def _target_once(path,body=None):
    """One remote RPC over the canonical ssh/scp pattern (semantics unchanged)."""
    token=uuid.uuid4().hex;local=PRIVATE/(token+'.json');remote=REMOTE+'/target/'+ROUND
    def run(args):
        x=subprocess.run(args,capture_output=True,text=True,timeout=330)
        if x.returncode:
            text=x.stderr[-1200:] or x.stdout[-1200:]
            # A handshake timeout is a BUSY app, not a dead one: report it as its own class so
            # the caller can retry it (and never retry an actual API failure).
            if any(m in text for m in _TARGET_HANDSHAKE_MARKERS):
                raise HandshakeTimeout('remote_handshake_timeout:'+path+':'+text[-300:])
            raise RuntimeError(text)
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


def _attribute_call(path):
    """Record WHICH site called the target, so item 2's inventory is measured, not hand-counted.

    wp8-03 item 2 has to retire the target RPCs, and the receipt must list every replacement as
    file:line. Attributing each call to its caller turns that list into data from a real run
    instead of a grep someone re-does by eye -- and it shows where the cost actually sits (one
    1 s poll loop accounted for most of the 33 calls in the rounds measured on 2026-09-16).

    Opt-in via A13_TARGET_CALL_AUDIT and strictly best-effort: an audit must never break a round,
    and it must never itself reach the network. Local file append only.
    """
    destination = os.environ.get('A13_TARGET_CALL_AUDIT')
    if not destination:
        return
    try:
        frame = sys._getframe(2)  # 0=_attribute_call, 1=target, 2=our caller
        entry = {'path': path,
                 'site': '%s:%d' % (Path(frame.f_code.co_filename).name, frame.f_lineno)}
        with open(destination, 'a') as stream:
            stream.write(json.dumps(entry, sort_keys=True) + '\n')
    except Exception:  # noqa: BLE001 - diagnostics must never be the reason a round fails
        pass


def target(path,body=None):
    _attribute_call(path)
    # 1. Surface first: a missing control file fails closed as `surface_lost` (the 63r1 class).
    if not _TARGET_SURFACE_CHECKED:
        require_remote(REMOTE,REMOTE_HOST)
        _TARGET_SURFACE_CHECKED.append(True)
    # 2. Then ONE bounded, READ-ONLY wake-up probe per process, so a busy-but-alive laptop app is
    #    retried rather than failing the round (the 63r2 class). Read-only on purpose: it is safe
    #    to repeat, and it happens before any state-changing call is made.
    if not _AWAKE:
        deadline=time.monotonic()+float(os.environ.get('A13_HANDSHAKE_DEADLINE_S','180'))
        sleep_s=float(os.environ.get('A13_HANDSHAKE_RETRY_SLEEP_S','2'))
        attempts=0
        while True:
            try:
                _target_once('/api/status')
                break
            except HandshakeTimeout as exc:
                attempts+=1
                if time.monotonic()>=deadline:
                    raise
                print('handshake_retry root=target attempt=%d %r'%(attempts,exc),flush=True)
                time.sleep(sleep_s)
        _AWAKE.append(True)
    return _target_once(path,body)
