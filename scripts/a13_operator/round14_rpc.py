# SPDX-License-Identifier: AGPL-3.0-only
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
import json,os,re,socket,stat,subprocess,shlex,time,urllib.error,uuid,sys
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
from app_rpc import AppClient
from round14_surface import require_local,require_remote,local_state

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
REMOTE_HOST=os.environ.get('A13_REMOTE_HOST','member-mac')
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

# ─────────────────────── ACTIVATION READINESS SEQUENCING (p82r3, 2026-09-20) ───────────────────────
# WHY THIS EXISTS -- read off the four rounds that died at `STEP_FAILED start_and_activate`
# (p82r1, p82r2, p81r2, p82r3): the round asked the app for a CONTROL decision while the app had not
# reported that it was in a state to take one. p82r1 and p82r3 died inside the first
# `/api/serving/status` read of the step as `handshake_timeout:...:attempts=16:TimeoutError('timed out')`
# while the app was ALIVE (surface present; it answered /api/status and /api/quit minutes later): the app
# does not answer inside app_rpc.py:15's FIXED 10 s read deadline while it compiles and then LOADS a 14 GB
# model, and `_client`'s busy retry is bounded too, so which budget expired first -- not the app's own
# state -- decided the round. `round14_rpc.py:100` names that consequence in its own words.
#
# THE FIX IS SEQUENCING, NOT PATIENCE ADDED TO ANY GUARD: no timeout, window, tolerance or identity check
# is changed here -- app_rpc.py's 10 s, `_OWNER_CALL_BUDGET_S`, `_OWNER_STARTUP_GRACE_S`, `_busy_reason`,
# `_is_refused`, `require_local/require_remote` and the reachability retry all stay byte-identical. What is
# added is a WAIT ON THE APP'S OWN READINESS REPORT, taken before a control call is issued, whose exit
# condition is the app's OWN reported field and never a clock:
#
#   `/api/serving/start`    -> `compilation.state == 'compiled'`         (chain_compile.py:83)
#   `/api/serving/activate` -> `serving.state == 'awaiting_activation'`  (chain_compile.py:116)
#
# Each accepted value is quoted from the app's OWN handler, which is the code that decides whether the
# call is accepted at all -- `start_serving`: "if state is None or state.get('state')!='compiled' or
# proposal!=state['execution_proposal']: raise DemoError('model_execution_proposal_changed')";
# `activate_serving`: "if workflow is None or gate is None or
# workflow.status()['state']!='awaiting_activation': raise DemoError('managed_workflow_not_awaiting_activation')".
#
# REJECTED SIGNALS, after reading the app rather than guessing: `route_ready` is hardcoded False on every
# `/api/serving/*` return (app.py:445) and never flips, and `serving_role` is not in the status payload at
# all -- it exists only as the launcher's startup event (__main__.py:59, 'not_yet_serving') and as the
# constant 'role':'not_yet_serving' (app.py:401). Neither can distinguish "still loading" from "ready".
# The report carries no field for "too busy to answer" either: silence is not a state, so the wait below
# treats a silent-but-live surface as work in progress and uses the app's OWN surface (native-control.json,
# unlinked only on shutdown, app.py:771) as the death discriminator.
_READINESS_READ = '/api/serving/status'   # read-only and non-mutating: the same cheap read the pinned
                                          # step polls (round14_start_and_activate.py:10-15)
_READINESS = {'/api/serving/start': ('compilation', 'compiled'),
              '/api/serving/activate': ('serving', 'awaiting_activation')}
_READINESS_TERMINAL = {'compilation': ('failed',), 'serving': ('failed', 'stopped')}
# HOW LONG THE APP MAY TAKE: the app's OWN declaration, not a number picked here. `RESOURCE_PROPOSAL`
# declares max_wall_seconds=1800 for this run (mycelium_a13_macos/serving.py:91), and the pinned step
# already declares the same 1800 s of patience for the loop that waits on this very compile
# (round14_start_and_activate.py:10: `deadline=time.monotonic()+1800`). The gate does not extend either;
# it makes the app's own report, rather than a read deadline, decide when the call may be issued.
# `tests/a13_operator/test_activation_readiness_sequencing.py` fails if those two declarations and this
# constant stop agreeing.
_READINESS_BUDGET_S = 1800.0
_READINESS_RETRY_S = 5.0


class NotReady(RuntimeError):
    """The app ANSWERED, and its own report is not a state that accepts the requested control call.

    Distinct from `HandshakeTimeout` ("the surface exists but did not answer in time") and from
    `surface_lost` ("the app is not running"): here the app is running and speaking, and what it says is
    that it has not reached (or can never reach) the state its own handler requires. The call is NOT
    issued -- issuing it would either be refused by the app or be asked to survive a load on a 10 s read.
    """


def _readiness_budget_s():
    try: return float(os.environ.get('A13_READINESS_BUDGET_S', _READINESS_BUDGET_S))
    except (TypeError, ValueError): return _READINESS_BUDGET_S


def _readiness_retry_s():
    try: return float(os.environ.get('A13_READINESS_RETRY_SLEEP_S', _READINESS_RETRY_S))
    except (TypeError, ValueError): return _READINESS_RETRY_S


def _reported_state(report, section):
    """The app's own reported state for one section of `/api/serving/status`, or None."""
    value = (report or {}).get(section)
    return value.get('state') if isinstance(value, dict) else None


def await_ready(root, path):
    """Block until the APP'S OWN report satisfies `path`'s precondition; then return. Issue nothing else.

    The exit condition is the app's report alone:
      * the required state appears                                   -> return; the caller may now call
      * a terminal state appears (compilation failed / serving failed|stopped) -> NotReady, at once
      * the surface disappears (require_local)                       -> surface_lost, at once (unchanged)
      * the app is SILENT while its surface is still present         -> wait, bounded and logged
      * anything that is not a busy/not-yet class (bad credential, a refusal after first contact) -> raised
    """
    section, accepted = _READINESS[path]
    started = time.monotonic()
    deadline = started + _readiness_budget_s()
    retry = _readiness_retry_s()
    grace = time.monotonic() + (_OWNER_STARTUP_GRACE_S if not _OWNER_ANSWERED else 0.0)
    attempts = 0
    last = 'not_asked'
    while True:
        attempts += 1
        try:
            report = _client(root).call(_READINESS_READ, {})
        except Exception as exc:                                  # classified, never swallowed
            reason = _busy_reason(exc)
            if reason is not None or isinstance(exc, HandshakeTimeout):
                last = reason or repr(exc)                        # alive but not answering yet
            elif _is_refused(exc) and not _OWNER_ANSWERED and time.monotonic() < grace:
                # The same narrow startup rule `owner()` already applies (79r1): a surface written before
                # the listener bound is a startup state, not a death -- inside the grace only.
                print('owner_startup_grace path=%s reason=%s' % (_READINESS_READ, repr(exc)[:110]), flush=True)
                last = repr(exc)
            else:
                raise
        else:
            if not _OWNER_ANSWERED:
                _OWNER_ANSWERED.append(True)
            state = _reported_state(report, section)
            if state in accepted:
                return report
            if state in _READINESS_TERMINAL.get(section, ()):
                print('readiness_refused path=%s state=%s' % (path, state), flush=True)
                raise NotReady('readiness_refused:%s:path=%s:state=%s' % (root, path, state)) from None
            last = 'state=%s' % state
        if time.monotonic() >= deadline:
            print('readiness_timeout path=%s attempts=%d last=%s' % (path, attempts, last), flush=True)
            raise NotReady('readiness_timeout:%s:path=%s:attempts=%d:last=%s'
                           % (root, path, attempts, last)) from None
        print('readiness_wait path=%s attempts=%d last=%s elapsed=%.1f'
              % (path, attempts, last, time.monotonic() - started), flush=True)
        time.sleep(retry)


# ───────────────── COMPILE EVIDENCE: THE OBSERVATIONAL WAIT (2026-09-21) ─────────────────
# WHY THIS EXISTS, MEASURED. p82r1/p82r3 died inside the FIRST `/api/serving/status` read of the pinned
# step as `handshake_timeout:...:attempts=16:TimeoutError('timed out')`
# (tests/a13_operator/test_activation_readiness_sequencing.py:3-9) and p83r1 died at the same line --
# `round14_start_and_activate.py:12` -- before any control call was ever issued. While the app compiles
# (7 m 46 s measured, plus the model load) it answers NOTHING inside `app_rpc.py:15`'s FIXED 10 s read
# deadline and `_client`'s busy retry is bounded, so the round dies minutes before the compile can
# possibly finish: which budget expired first -- not the app's own state -- decided the round.
#
# THE FIX IS AN OBSERVATION, NOT PATIENCE ADDED TO ANY GUARD. No timeout, deadline, window, retry count
# or identity check changes: `app_rpc.py`'s 10 s, `A13_HANDSHAKE_DEADLINE_S`, `_OWNER_CALL_BUDGET_S`,
# `_OWNER_STARTUP_GRACE_S`, `_READINESS_BUDGET_S`, `_busy_reason`, `_is_refused` and
# `require_local/require_remote` stay exactly as they were. What is added is a wait BEFORE a call the
# compile blocks is issued, whose exit condition is the APP'S OWN ON-DISK STATE and never a clock:
#
#   SUCCESS  `{OWNER}/chain-runs/{run_id}/compile-complete.private.json`, written atomically
#            by chain_compile AFTER workflow preparation succeeds and `app.guard` releases.
#            Public compiled state is set only after this durable publication succeeds. Match the
#            start record, round scope, exact schema and epoch. An early operator-plan marker
#            cannot show guard release, even if it is the last workflow write.
#   FAILURE  `{OWNER}/chain-runs/{run_id}/compile-error.private.log`.
#
# Neither `compiled/build-report.json` nor the serving-run operator plan proves the compiler
# has exited the guard. The latter is published inside prepare_serving_workflow under the guard.
#
# THE RUN ID IS THE ROUND'S OWN, not a guess: step 1 writes it to `{OWNER}/{ROUND}/current-scope.json`
# before it asks the app to compile (round14_bootstrap_compile.py:242-244, :275) and both steps run
# under one A13_RPC_ROUND (run_a13_attempt.sh:34, :49-50). It is validated with the app's own segment
# rule (managed_control.py:207, :283-285) before it is ever used as a path.
#
# THE WAIT REUSES THE APP'S OWN DECLARED WALL BOUND (`_READINESS_BUDGET_S`, 1800 s -- already derived
# from `serving.py`'s RESOURCE_PROPOSAL.max_wall_seconds and the pinned step's own 1800 s of patience)
# and the existing retry interval: no new number is picked. It engages ONLY for the paths the compile
# blocks (`_COMPILE_DEPENDENT`) and ONLY when the round's own scope record names a run whose root
# exists, so every other caller -- `/api/quit` during a teardown included -- is untouched, and a round
# that is not waiting on a compile behaves exactly as it did before.
#
# IT CANNOT TURN A FAILURE INTO A PASS: it issues no RPC at all, so it can only DELAY the existing call
# path. Whatever ends it -- the marker, the failure record, the surface going away, its own bound -- the
# caller's own read is then issued with its own budgets, unchanged.
_COMPILE_DEPENDENT = frozenset(('/api/serving/status','/api/serving/start','/api/serving/activate'))
_RUN_SCOPE = 'current-scope.json'
_RUN_ID_SEGMENT = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')   # == managed_control.py:207
_COMPILE_ERROR_LOG = 'compile-error.private.log'                      # chain_compile.py:71


def _run_root(run_id):
    """`mycelium_a13_macos/chain_compile.py:31`: the app's run root for this run."""
    return OWNER/'chain-runs'/run_id


def _marker_path(run_id):
    """Early workflow artifact, retained for diagnostics; never compile completion."""
    return OWNER/'authority'/'serving-runs'/run_id/'operator-plan.json'


def _compile_run_id():
    """The run this round is compiling, taken from the round's own scope record; None if there is none."""
    try:
        document=json.loads((PRIVATE/_RUN_SCOPE).read_text())
    except (OSError,ValueError):
        return None
    run_id=document.get('run_id') if isinstance(document,dict) else None
    return run_id if isinstance(run_id,str) and _RUN_ID_SEGMENT.fullmatch(run_id) else None


def _private_compile_record(path):
    """Accept only the producer's private regular, bounded, atomically published JSON."""
    try:
        metadata=path.lstat()
        if (not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode)!=0o600
                or metadata.st_uid!=os.getuid() or metadata.st_size>4096):return None
        document=json.loads(path.read_text('utf-8'))
    except (OSError,ValueError,UnicodeError):
        return None
    return document if isinstance(document,dict) else None


def _compile_evidence(run_id):
    """Only an exact post-guard completion for the scoped run/epoch is success."""
    root=_run_root(run_id)
    if (root/_COMPILE_ERROR_LOG).exists():return 'failed'
    try:scope=json.loads((PRIVATE/_RUN_SCOPE).read_text('utf-8'))
    except (OSError,ValueError):return None
    if not isinstance(scope,dict) or scope.get('run_id')!=run_id:return None
    deployment=scope.get('deployment_id')
    if not isinstance(deployment,str) or not deployment:return None
    start=_private_compile_record(root/'compile-start.private.json')
    complete=_private_compile_record(root/'compile-complete.private.json')
    if start is None or complete is None:return None
    epoch=start.get('epoch')
    if type(epoch) is not int or epoch<0:return None
    identity={'run_id':run_id,'deployment_id':deployment,'epoch':epoch}
    if start!={'protocol':'mycelium.chain_compile_start.v1',**identity}:return None
    if complete!={'protocol':'mycelium.chain_compile_complete.v1',**identity,'state':'compiled'}:return None
    return 'completed'


def await_compile_end():
    """Wait, OBSERVATIONALLY, until the app's own on-disk state says this round's compile has ended.

    Issues no RPC of any kind, so it cannot expire inside a read deadline and costs the handshake budget
    nothing. Returns the evidence it stopped on:
      'completed' exact post-guard producer completion matched to this round's run/deployment/epoch
      'failed'    the app's own failure record exists
      'absent'    no run to watch, or the control surface is gone (app.py:794): the caller's EXISTING
                  path decides, in its own vocabulary (`surface_lost`, `compiler_not_ready`, ...)
      'expired'   the app's own declared wall bound elapsed: the caller's existing path runs unchanged
    """
    run_id=_compile_run_id()
    if run_id is None:return 'absent'
    if not _run_root(run_id).is_dir():return 'absent'
    started=time.monotonic();deadline=started+_readiness_budget_s();retry=_readiness_retry_s();attempts=0
    while True:
        attempts+=1
        evidence=_compile_evidence(run_id)
        if evidence is not None:
            print('compile_evidence run_id=%s evidence=%s attempts=%d elapsed=%.1f'
                  %(run_id,evidence,attempts,time.monotonic()-started),flush=True)
            return evidence
        if local_state(OWNER).get('present') is not True:
            print('compile_wait_surface_lost run_id=%s attempts=%d'%(run_id,attempts),flush=True)
            return 'absent'
        if time.monotonic()>=deadline:
            print('compile_wait_expired run_id=%s attempts=%d'%(run_id,attempts),flush=True)
            return 'expired'
        print('compile_wait run_id=%s attempts=%d elapsed=%.1f'
              %(run_id,attempts,time.monotonic()-started),flush=True)
        time.sleep(retry)


# ───────────────── SERVING EVIDENCE: THE SAME WAIT, ONE PHASE LATER (2026-09-21) ─────────────────
# WHY THIS EXISTS. The compile fix above (commit 266615f) names this wait as NOT fixed and NOT claimed:
# "the post-activation wait for `serving.state == 'api_running'` (round14_start_and_activate.py:42-46)
# is the SAME unbounded-duration exposure: that phase has no on-disk success marker either (only
# serve-error.private.json, chain_compile.py:96)". It does have one, and it is the app's own last disk
# write before that state -- the same shape as the compile marker, one phase later.
#
# READ OFF SOURCE, NOT GUESSED -- what the app writes on its way to `api_running`:
#   * `mycelium_a13_macos/workflow.py:233-234` runs `self.launch.run(...)` = `RegistryLaunch.run`
#     (`mycelium_a13_macos/serving.py:211-258`), whose runner is `_native_registry_runner`
#     (`serving.py:493-496`) = `mycelium_live.supervisor.run_registry_server`.
#   * `run_registry_server` (supervisor.py:2211-2455) QUALIFIES each plan FIRST -- the route open and
#     the model load, `_qualified_runtime` (:2273-2286) -- and only THEN builds the registry:
#     `LiveDeploymentRegistry(..., state_path=registry_state, ...)` (:2287-2292).
#   * that constructor's LAST statement persists the registry to disk (`mycelium_live/registry.py:149`
#     -> `_persist`, :785-815: temp file + fsync + os.replace), and `state_path` is never None here:
#     `serving.py:599` sets `registry_state = root / "registry-state.json"`, :608 passes it, with
#     `private_state_root=root/'registry'` from `workflow.py:362`, where
#     `root = authority.state_root/'serving-runs'/run_id` (:357).
#   * only AFTER that does the HTTP server exist (`create_server`, :2390-2404) and only then does
#     `on_started` (:2427-2428) run the callback that sets `_state='api_running'` (`workflow.py:213`)
#     -- in memory, never persisted.
# So `{OWNER}/authority/serving-runs/{run_id}/registry/registry-state.json` is the durable form of the
# verdict the operator waits for: its ABSENCE cannot coexist with an app that reports 'api_running'.
# MEASURED IN A REAL ROUND (not inferred): the owner-root mtime timeline of round 64r1 lists this file
# at the same second as the pinned step's own terminal write,
# `authority/serving-runs/<run>/registry/registry-state.json ; rpc-round64/serving-load-result.json`
# (runs/wp8-02-useful-two-mac/RESULT-4.md:125; the second file is round14_start_and_activate.py:47).
#
# WHY IT DOES NOT FIRE BEFORE THE APP CAN ANSWER -- the compile fix's own discriminator, re-applied.
# That fix rejected `compiled/build-report.json` because `chain_compile.py:54` then holds `app.guard`
# (`app.py:872` takes the same guard for the handshake POST) for up to
# `_BOOT_ID_OBSERVATION_BUDGET_SECONDS=180`. This phase has no such holder: the only `with app.guard`
# outside `app.py` is that compile one, and it is in `start_compile`, not in what runs after
# activation. This marker is also written AFTER the qualification (supervisor.py:2273-2286) and BEFORE
# the remaining steps to `api_running`, which are composition and a socket bind (`build_registry_stack`
# :2374-2382 -> :469-497; `create_server` :2390-2404 -> :1578-1617) -- so the app's control surface has
# no pending long-running work of the kind that starved it during the compile.
#
# WHERE IT ENGAGES, AND WHERE IT MUST NOT: only after THIS process has itself asked the app to
# activate (`/api/serving/activate`, chain_compile.py:103-119). Before activation there is no serving
# phase to observe, and the pre-activation reads ARE the app's readiness report (`await_ready`'s own
# probe) and the pinned step's first read -- gating those would delay a round that has not started
# this phase. So `/api/quit` during a teardown, the soak steps' own polls and every pre-activation
# read keep the path they had.
#
# AND WHAT IT WATCHES IS A *PREPARED* SERVING RUN -- the app's own durable statement that there is one:
# `workflow.py:361-362` runs `prepare_registry_launch(private_state_root=root/'registry', ...)` while
# the compile still holds, and that call creates `{serving-runs}/{run_id}/registry/` and its `product/`
# (`serving.py:596-598`). No such root means there is no serving run for this run id to observe, so the
# wait disengages immediately ('absent') -- the same rule and the same idiom the compile wait uses
# ("no run to watch"), one level down. A round that has not prepared this phase is untouched.
#
# IT CANNOT TURN A FAILURE INTO A PASS: it issues no RPC of any kind, so it can only DELAY the existing
# call. A `failed` serving state has its own durable record (`chain_compile.py:96`); a `stopped` one
# does not (`workflow.py:235` returns normally when the stop event is set), so in that one case the wait
# ends on its own bound and the caller's existing read -- with its own budgets -- reports it.
_SERVING_DEPENDENT = frozenset(('/api/serving/status',))
_SERVING_REQUESTS = frozenset(('/api/serving/activate',))     # the call that starts this phase
_SERVING_ERROR = 'serve-error.private.json'                   # chain_compile.py:96
_SERVING_STATE = 'registry-state.json'                        # mycelium_live/registry.py:149
_SERVING_REQUESTED = []   # mutable: has THIS process asked the app to activate? (mirrors _OWNER_ANSWERED)


def _serving_root(run_id):
    """`workflow.py:357-362`: the app's own registry root for this run."""
    return OWNER/'authority'/'serving-runs'/run_id/'registry'


def _serving_marker_path(run_id):
    """`serving.py:599` + `mycelium_live/registry.py:149`: persisted before the app reports api_running."""
    return _serving_root(run_id)/_SERVING_STATE


def _serving_error_path(run_id):
    """`chain_compile.py:95-97`: the app's own failure record for this phase."""
    return _run_root(run_id)/_SERVING_ERROR


def _serving_evidence(run_id):
    """The app's OWN on-disk verdict for this run: 'failed' | 'completed' | None while still qualifying."""
    if _serving_error_path(run_id).exists():return 'failed'
    if _serving_marker_path(run_id).exists():return 'completed'
    return None


def await_serving_end():
    """Wait, OBSERVATIONALLY, until the app's own on-disk state says this round's serving phase ended.

    Issues no RPC of any kind, so it cannot expire inside a read deadline. Returns the evidence it
    stopped on:
      'completed' the registry the app persists before `api_running` exists -- the phase ended
      'failed'    the app's own failure record (chain_compile.py:96) exists
      'absent'    no run to watch, no *prepared* serving run for this run id (serving.py:596-598), or
                  the control surface is gone (app.py:794): the caller's EXISTING path decides, in its
                  own vocabulary (`surface_lost`, ...)
      'expired'   the app's own declared wall bound elapsed: the caller's existing path runs unchanged
    """
    run_id=_compile_run_id()
    if run_id is None:return 'absent'
    if not _run_root(run_id).is_dir():return 'absent'
    if not _serving_root(run_id).is_dir():return 'absent'
    started=time.monotonic();deadline=started+_readiness_budget_s();retry=_readiness_retry_s();attempts=0
    while True:
        attempts+=1
        evidence=_serving_evidence(run_id)
        if evidence is not None:
            print('serving_evidence run_id=%s evidence=%s attempts=%d elapsed=%.1f'
                  %(run_id,evidence,attempts,time.monotonic()-started),flush=True)
            return evidence
        if local_state(OWNER).get('present') is not True:
            print('serving_wait_surface_lost run_id=%s attempts=%d'%(run_id,attempts),flush=True)
            return 'absent'
        if time.monotonic()>=deadline:
            print('serving_wait_expired run_id=%s attempts=%d'%(run_id,attempts),flush=True)
            return 'expired'
        print('serving_wait run_id=%s attempts=%d elapsed=%.1f'
              %(run_id,attempts,time.monotonic()-started),flush=True)
        time.sleep(retry)


def owner(path,body=None):
    require_local(OWNER)
    # OBSERVATIONAL WAIT (block above): a call the compile blocks is not issued while the app is still
    # compiling -- the round watches the app's own on-disk evidence instead of polling into a read
    # deadline it cannot survive. The call itself, and every budget it uses, is unchanged.
    if path in _COMPILE_DEPENDENT:
        await_compile_end()
    # THE SAME WAIT, ONE PHASE LATER (block above): once THIS process has asked the app to activate,
    # the serving state is read from the app's own on-disk evidence rather than polled while the app
    # qualifies. Absent that request the read path is exactly what it was.
    if path in _SERVING_DEPENDENT and _SERVING_REQUESTED:
        await_serving_end()
    # SEQUENCING: a control call is issued only after the app has itself reported the state its own
    # handler requires. Read-only calls are untouched -- they ARE the report (`/api/serving/status` is the
    # probe the gate uses, so it cannot be gated by it).
    if path in _READINESS:
        await_ready(OWNER,path)
    deadline=time.monotonic()+_OWNER_CALL_BUDGET_S
    grace=time.monotonic()+(_OWNER_STARTUP_GRACE_S if not _OWNER_ANSWERED else 0.0)
    while True:
        try:
            value=_client(OWNER).call(path,body)
            if not _OWNER_ANSWERED:
                _OWNER_ANSWERED.append(True)
            # THE SERVING PHASE HAS NOW BEEN ASKED FOR (block above): from here on this process reads the
            # serving state from the app's own on-disk evidence. A refusal never reaches this line, so a
            # round whose activation was refused keeps the read path it had.
            if path in _SERVING_REQUESTS and not _SERVING_REQUESTED:
                _SERVING_REQUESTED.append(True)
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
# blipped: the cleanup's own quit reported `ssh: connect to host 203.0.113.12 port 22: Operation
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
    # NO READINESS GATE HERE, and that is a finding rather than an omission (p82r3 investigation,
    # 2026-09-20): the TARGET app refuses `/api/serving/status` outright -- app.py:427-428 routes those
    # paths to `owner_action_required` whenever `installation_role!='owner'` -- so the laptop's app
    # publishes NO field the round could wait on. The target-side sequencing that exists is the app's own
    # offer report (`/api/staging/check`), which the pinned step already polls to completion before it
    # previews (round14_start_and_activate.py:26-28). A gate here would need a readiness read the target
    # app does not serve; it is not invented. p81r2's `local_api_http_409:action_not_completed` at
    # `/api/work/preview` is therefore NOT addressed by this change -- it is not a timeout class at all.
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
