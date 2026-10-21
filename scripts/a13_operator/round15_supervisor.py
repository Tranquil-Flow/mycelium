# SPDX-License-Identifier: AGPL-3.0-only
"""Detached two-Mac round supervisor: owns BOTH app daemons AND the chain.

Why this exists (2026-09-16, attempt 63r1). The round's two app daemons were background
children of an interactive chat session. When that session was torn down - provider
exhaustion (DeepSeek 402, ZAI 429) then `Context length exceeded: 516,874 tokens`, then an
interrupt - the session's process group was SIGTERMed, the round died mid-compile, and chain
step 2 reported the misleading `compiler_not_ready` because the app's control surface had
simply gone away. A physical round must not be owned by anything interactive.

This script is the single supported entry point for a physical round. It
  * re-execs itself into its own session (macOS has no setsid(1): detachment uses
    `start_new_session=True`), so nothing a chat session does can reap it;
  * starts both daemons detached, with their own logs;
  * waits for BOTH control surfaces by name, so a dead app fails as `surface_lost` before a
    single API call is made;
  * runs the bounded chain (run_a13_attempt.sh, itself refused unless supervised);
  * always quits both apps and sweeps BOTH Macs for strays (a daemon behind ssh survives the
    local ssh client dying - that asymmetry is how 63r1's orphan was identified).

Usage:
  A13_OWNER_ROOT=<abs> A13_REMOTE_ROOT=<abs> A13_EVIDENCE_DIR=<abs> \\
    python3 -B scripts/a13_operator/round15_supervisor.py <version> [options]

Options:
  --max-seconds N     daemon lifetime bound (default 1800, the app helper's own ceiling)
  --surface-timeout S seconds to wait for both surfaces (default 420)
  --surfaces-only     start daemons, wait for surfaces, report, stop (rehearsal: no chain)
  --no-quit           do not quit the apps at the end (rehearsal/debug)
  --foreground        do not self-detach (tests/CI that own their session already)
Log: $A13_EVIDENCE_DIR/round<label>-supervisor.log (never overwritten; exit 4 if present).
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

from round14_surface import local_state, remote_state, wait_local, wait_remote  # noqa: E402

CLEAN_ENV_DROP = ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV")


def utc():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_env(extra=None):
    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV_DROP}
    env.update(extra or {})
    return env


def require_abs(name):
    value = os.environ.get(name) or ""
    if not value.startswith("/"):
        raise SystemExit("supervisor_env_required:" + name)
    return value


def spawn(argv, log_path, extra_env=None):
    """Start a child in its OWN session, stdout+stderr onto its log."""
    handle = open(log_path, "ab")
    child = subprocess.Popen(
        argv,
        stdout=handle,
        stderr=handle,
        stdin=subprocess.DEVNULL,
        cwd=str(REPO),
        env=clean_env(extra_env),
        start_new_session=True,
    )
    return child


def session_facts(pid=None):
    """What `ps` can actually tell us on macOS.

    `ps -o sess=` prints 0 on macOS, so session leadership cannot be read from it. The
    observable detachment signals are an own process group (pgid == pid), no controlling tty,
    and reparenting to launchd (ppid == 1). Recorded this way so the evidence never claims
    more than it shows - the first rehearsal log said `session_leader: false` purely because
    that field was unreadable, which would have been a false negative.
    """
    pid = pid or os.getpid()
    out = {}
    for key in ("pid", "ppid", "pgid", "sess", "tty"):
        done = subprocess.run(["ps", "-o", "%s=" % key, "-p", str(pid)], capture_output=True, text=True)
        out[key] = done.stdout.strip() if done.returncode == 0 else "?"
    out["own_process_group"] = out.get("pgid") == str(pid)
    out["no_controlling_tty"] = out.get("tty") in ("??", "-")
    out["reparented_to_launchd"] = out.get("ppid") == "1"
    return out


def git_head():
    done = subprocess.run(["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True)
    return done.stdout.strip() if done.returncode == 0 else "?"


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--max-seconds", type=int, default=1800)
    parser.add_argument("--surface-timeout", type=float, default=420.0)
    parser.add_argument("--chain-timeout", type=float, default=2400.0)
    parser.add_argument("--surfaces-only", action="store_true")
    parser.add_argument("--no-quit", action="store_true")
    parser.add_argument("--foreground", action="store_true")
    args = parser.parse_args(argv)

    owner_root = Path(require_abs("A13_OWNER_ROOT"))
    remote_root = require_abs("A13_REMOTE_ROOT")
    evidence = Path(require_abs("A13_EVIDENCE_DIR"))
    host = os.environ.get("A13_REMOTE_HOST", "member-mac")
    python = os.environ.get("A13_PYTHON", sys.executable)
    label = os.environ.get("A13_LOG_LABEL") or args.version
    evidence.mkdir(parents=True, exist_ok=True)
    log = evidence / ("round%s-supervisor.log" % label)
    owner_daemon_log = evidence / ("round%s-owner-daemon.log" % label)
    target_daemon_log = evidence / ("round%s-target-daemon.log" % label)
    already_detached = os.environ.get("A13_SUPERVISOR_DETACHED") == "1"

    if log.exists() and not already_detached:
        print("SUPERVISOR_LOG_EXISTS %s" % log, file=sys.stderr)
        return 4

    detached_now = already_detached
    if not args.foreground and not already_detached and os.getsid(0) != os.getpid():
        child = spawn(
            [sys.executable, "-B", str(Path(__file__).resolve()), *argv],
            log,
            extra_env={"A13_SUPERVISOR_DETACHED": "1"},
        )
        print("SUPERVISOR_DETACHED pid=%s log=%s" % (child.pid, log))
        print("  monitor: tail -f %s" % log)
        return 0

    # Real run. Put our own stdout/stderr on the log as well, so the log is the whole story.
    os.environ["A13_OWNER_DAEMON_LOG"] = str(owner_daemon_log)
    os.environ["A13_TARGET_DAEMON_LOG"] = str(target_daemon_log)
    os.environ["A13_ROUND_SUPERVISED"] = "1"
    handle = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
    os.dup2(handle.fileno(), 1)
    os.dup2(handle.fileno(), 2)
    sys.stdout = sys.stderr = handle

    print(json.dumps({
        "event": "supervisor_start", "at": utc(), "version": args.version, "label": label,
        "head": git_head(), "owner_root": str(owner_root), "remote_root": remote_root, "host": host,
        "max_seconds": args.max_seconds, "surfaces_only": args.surfaces_only,
        "detached_now": detached_now, "session": session_facts(),
    }, sort_keys=True), flush=True)


    owner_daemon = spawn(
        [python, "-B", str(HERE / "packaged_owner_daemon.py"), "--version", args.version,
         "--max-seconds", str(args.max_seconds)],
        owner_daemon_log, extra_env={"A13_OWNER_ROOT": str(owner_root)},
    )
    target_daemon = spawn(
        [python, "-B", str(HERE / "packaged_target_daemon.py"), "--version", args.version,
         "--max-seconds", str(args.max_seconds)],
        target_daemon_log, extra_env={"A13_REMOTE_ROOT": remote_root, "A13_REMOTE_HOST": host},
    )
    print(json.dumps({"event": "daemons_started", "at": utc(), "owner_pid": owner_daemon.pid,
                      "target_pid": target_daemon.pid, "owner_log": str(owner_daemon_log),
                      "target_log": str(target_daemon_log)}, sort_keys=True), flush=True)

    chain_exit = None
    section = "surfaces"
    try:
        surfaces = {"owner": wait_local(owner_root, args.surface_timeout)}
        print(json.dumps({"event": "surface", "label": "owner", "state": surfaces["owner"]}), flush=True)
        surfaces["target"] = wait_remote(remote_root, host, args.surface_timeout)
        print(json.dumps({"event": "surface", "label": "target", "state": surfaces["target"]}), flush=True)
        if not args.surfaces_only:
            section = "chain"
            chain = spawn(["bash", str(HERE / "run_a13_attempt.sh"), args.version], log)
            try:
                chain_exit = chain.wait(timeout=args.chain_timeout)
            except subprocess.TimeoutExpired:
                chain.kill()
                chain_exit = "chain_timeout"
            print(json.dumps({"event": "chain_finished", "at": utc(), "exit": chain_exit}), flush=True)
    except RuntimeError as exc:
        print(json.dumps({"event": "surface_lost", "at": utc(), "section": section,
                          "error": str(exc)[:4000]}), flush=True)
        chain_exit = "surface_lost"
    finally:
        if not args.no_quit:
            quit_done = subprocess.run(
                [python, "-B", str(HERE / "round15_quit.py")], capture_output=True, text=True,
                cwd=str(REPO), env=clean_env(), timeout=600,
            )
            print(quit_done.stdout.strip()[:2000], flush=True)
            time.sleep(5)
        sweep_done = subprocess.run(
            [python, "-B", str(HERE / "round15_strays.py")], capture_output=True, text=True,
            cwd=str(REPO), env=clean_env(), timeout=300,
        )
        print(sweep_done.stdout.strip()[:4000], flush=True)
        print(json.dumps({"event": "supervisor_done", "at": utc(), "version": args.version,
                          "label": label, "head": git_head(), "chain_exit": chain_exit,
                          "surfaces_only": args.surfaces_only, "no_quit": args.no_quit,
                          "owner_daemon_pid": owner_daemon.pid, "target_daemon_pid": target_daemon.pid},
                         sort_keys=True), flush=True)
        print("SUPERVISOR_DONE chain_exit=%s" % chain_exit, flush=True)
    return 0 if chain_exit in (0, None) else (6 if chain_exit == "surface_lost" else 1)


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        sys.exit(130)
