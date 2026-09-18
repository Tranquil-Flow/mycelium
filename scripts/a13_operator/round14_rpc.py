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


# Measured 2026-09-17 from the three PASSING rounds (68r1/70r1/72r1): a legitimate single call on this
# channel takes tens of seconds -- target WORK_ACCEPTED 79.4 / 78.6 / 104.1 s, previews 49.9-63.3 s,
# owner calls 24.4-44.6 s. The handshake underneath is bounded by app_rpc.py:15's FIXED 10 s, a PINNED
# file whose timeout cannot be raised, so a read can expire while the app is merely LOADING the model.
# That is exactly how 74r1 died: STEP_FAILED stream, `TimeoutError: timed out` at
# `self._sock.recv_into(b)`, immediately after ACTIVATION {'state': 'activation_requested'}.
# So retry a timeout here, bounded -- using the SAME busy-vs-dead classification the handshake already
# uses (_busy_reason). A timeout means BUSY and is retried; everything else still fails closed, because
# a missing surface, a refused connection or a bad credential must never be retried into a deadline.
_OWNER_CALL_BUDGET_S = 120.0     # 1.15 x the 104.123 s maximum actually observed (72r1), same rule the
                                 # RSS bound was rebuilt under (66b756a): derive, do not pick.
# STARTUP GRACE (79r1, 2026-09-18). A refusal is NOT the same thing at startup as mid-run. 79r1 failed at
# bootstrap_compile with `URLError: <urlopen error [Errno 61] Connection refused>` while the owner's
# native-control.json already EXISTED (sweep, 76 bytes) -- the surface was written before the listener
# bound, so require_local passed and the first call was refused. _busy_reason deliberately leaves a
# refusal non-retryable, and that is RIGHT mid-run, where a refusal means the app died. So this tolerates
# a refusal ONLY until the channel has answered once, and only inside a short grace; after that, a
# refusal still fails closed on the first attempt exactly as before.
_OWNER_STARTUP_GRACE_S = 20.0
_OWNER_ANSWERED = []             # mutable: has this process ever got a reply from the owner?

def _is_refused(exc):
    """True for a refused connection, in either shape urllib surfaces it."""
    if isinstance(exc, ConnectionRefusedError):
        return True
    return isinstance(exc, urllib.error.URLError) and 'refused' in str(getattr(exc, 'reason', exc)).lower()

def owner(path,body=None):
    require_local(OWNER)
    deadline=time.monotonic()+_OWNER_CALL_BUDGET_S
    grace=time.monotonic()+(_OWNER_STARTUP_GRACE_S if not _OWNER_ANSWERED else 0.0)
    while True:
        try:
            value=_client(OWNER).call(path,body)
            if not _OWNER_ANSWERED:
                _OWNER_ANSWERED.append(True)
            return value
        except Exception as exc:
            reason=_busy_reason(exc)
            if reason is None:
                if _is_refused(exc) and not _OWNER_ANSWERED and time.monotonic()<grace:
                    print('owner_startup_grace path=%s reason=%s'%(path,repr(exc)[:110]),flush=True)
                    time.sleep(1.0)
                    continue
                raise
            if time.monotonic()>=deadline:
                raise
            print('owner_busy_retry path=%s reason=%s'%(path,reason[:120]),flush=True)
            time.sleep(2.0)

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


# Reachability codes: the peer could not reach the owner THIS attempt. Retrying is only safe for
# operations that re-derive from current state, which is why the retry is path-scoped and opt-in
# rather than applied to every target() call (a POST may be non-idempotent).
# The ssh-level class, added 2026-09-17. Round 75r1 died at the stream step while the laptop's link
# blipped: the cleanup's own quit reported `ssh: connect to host 100.126.111.123 port 22: Operation
# timed out`, and the laptop answered ping and ssh again minutes later (ping stddev 26 ms -- an unstable
# link, not a dead host). _target_once already reports this shape as its own class
# (`remote_handshake_timeout`), raised from the callers' own _TIMEOUT_MARKERS -- retrying it is exactly
# what that class exists for. Without it, a transient blip killed a round that had already staged and
# compiled. Retrying stays SAFE here only because the paths are opt-in (_retry_paths) and scoped to
# operations that re-derive from current state.
REACHABILITY_CODES=('managed_control_unreachable','seed_http_unreachable','peer_transport_unreachable',
                    'remote_handshake_timeout')


def _retry_paths():
    raw=os.environ.get('A13_RETRY_REACHABILITY_PATHS','')
    return frozenset(part.strip() for part in raw.split(',') if part.strip())


def _retry_budget_seconds():
    try:return float(os.environ.get('A13_RETRY_REACHABILITY_BUDGET_S','60'))
    except ValueError:return 60.0


def _target_once_reachability_retry(path,body):
    """Bounded retry for a reachability refusal on a re-derivable operation.

    Measured 2026-09-17 (round 71r1): a single 409 `managed_control_unreachable` from
    `/api/work/accept` ended a round that had already passed staging, native start and both exact
    previews. The operation is safe to repeat because `work_acceptance.accept_work` re-fetches the
    offer, recomputes the preview and requires `current == preview` -- a retry either completes the
    identical accept or fails closed as `work_preview_changed`. It cannot double-accept silently.

    Env-gated and path-scoped on purpose: round14_start_and_activate.py is pinned byte-identical,
    so the retry cannot live at its call site, and a blanket retry inside target() would re-issue
    operations that are NOT re-derivable.
    """
    started=time.monotonic();attempts=0;delay=2.0
    while True:
        attempts+=1
        try:
            return _target_once(path,body)
        except RuntimeError as error:
            text=str(error)
            if not any(code in text for code in REACHABILITY_CODES):
                raise
            if time.monotonic()-started>=_retry_budget_seconds():
                raise
            print('reachability_retry path=%s attempt=%d %s'%(path,attempts,text[-160:]),flush=True)
            time.sleep(delay);delay=min(15.0,delay*1.6)


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
    if path in _retry_paths():
        return _target_once_reachability_retry(path,body)
    return _target_once(path,body)
