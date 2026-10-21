# SPDX-License-Identifier: AGPL-3.0-only
"""Control-surface liveness for the two packaged apps: a lost surface is a NAMED failure.

`mycelium_a13_macos/app.py` unlinks `native-control.json` on shutdown (:771) and writes it
at startup (:294), so its absence means "the app is not running" - never "bad credentials".
`app_rpc.AppClient.__init__` would otherwise surface a bare `FileNotFoundError` from `lstat`
(:8); on 2026-09-16 chain step 2 reported exactly that as `compiler_not_ready` and consumed a
physical attempt (runs/wp8-02-useful-two-mac/ERRATA-1.md, decisions.log 63r1 entry).

This lives beside the chain rather than inside a chain step because `round14_rpc.py` is the
single owner of the path/env contract and the five files that must stay byte-identical to the
read-only lane source (`README.md` identity ledger) cannot be edited. Roots and host are
passed in, so this module imports nothing from `round14_rpc` (no import cycle).
"""

import json
import os
import shlex
import stat
import subprocess
import time
from pathlib import Path

CONTROL = "native-control.json"
_LOG_ENV = {"owner": "A13_OWNER_DAEMON_LOG", "target": "A13_TARGET_DAEMON_LOG"}
_MARKERS = (
    "packaged_app_closed",
    "OWNER_DAEMON_EXIT",
    "TARGET_DAEMON_EXIT",
    "LAUNCH ",
    "launcher_started",
)


def local_state(root):
    """Control-file metadata for a local app root, without following symlinks."""
    p = Path(root) / CONTROL
    try:
        info = p.lstat()
    except FileNotFoundError:
        return {"present": False, "path": str(p)}
    except OSError as exc:
        return {"present": None, "path": str(p), "error": repr(exc)[:200]}
    return {
        "present": stat.S_ISREG(info.st_mode),
        "path": str(p),
        "mode": oct(stat.S_IMODE(info.st_mode)),
        "uid": info.st_uid,
        "bytes": info.st_size,
        "mtime": info.st_mtime,
    }


_REMOTE_SCRIPT = (
    "import json,stat,sys\n"
    "from pathlib import Path\n"
    "p=Path(sys.argv[1])\n"
    "try:\n"
    "    i=p.lstat()\n"
    "except FileNotFoundError:\n"
    "    print(json.dumps({'present':False}));raise SystemExit\n"
    "print(json.dumps({'present':stat.S_ISREG(i.st_mode),'mode':oct(stat.S_IMODE(i.st_mode)),"
    "'bytes':i.st_size,'mtime':i.st_mtime}))\n"
)


def remote_state(remote_root, host):
    """Ask the laptop itself, via its /usr/bin/python3 (3.9-safe, stdlib only)."""
    path = str(remote_root) + "/target/" + CONTROL
    cmd = [
        "ssh", "-T", "-o", "BatchMode=yes", host,
        shlex.join(["/usr/bin/python3", "-c", _REMOTE_SCRIPT, path]),
    ]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return {"present": None, "error": "ssh_timeout", "path": path}
    if done.returncode:
        return {"present": None, "path": path, "error": (done.stderr or done.stdout)[-300:].strip()}
    try:
        return json.loads(done.stdout.strip().splitlines()[-1])
    except Exception:
        return {"present": None, "path": path, "error": "unparseable:" + done.stdout[-200:]}


def daemon_hint(label, log_path=None):
    """Best-effort evidence from that app's daemon log: its own last markers."""
    path = log_path or os.environ.get(_LOG_ENV.get(label, ""), "")
    if not path:
        return {"log": None}
    # Stringify: callers may pass a Path, and the hint is json.dumps'd into the
    # surface_lost message. A Path here raised TypeError INSIDE the failure path,
    # replacing the named `surface_lost` cause with an unhelpful serialization error
    # (found by the wp8-03 offline rehearsal, 2026-09-17).
    hint = {"log": str(path)}
    try:
        tail = Path(path).read_text(errors="replace")[-6000:]
    except OSError as exc:
        hint["log_error"] = repr(exc)[:160]
        return hint
    for marker in _MARKERS:
        for line in reversed(tail.splitlines()):
            if marker in line:
                hint[marker.strip()] = line.strip()[:240]
                break
    return hint


def _lost(label, state, hint):
    return RuntimeError(
        "surface_lost:%s:%s:daemon=%s"
        % (label, json.dumps(state, sort_keys=True), json.dumps(hint, sort_keys=True))
    )


def require_local(root, label="owner", log_path=None):
    state = local_state(root)
    if state.get("present") is True:
        return state
    raise _lost(label, state, daemon_hint(label, log_path))


def require_remote(remote_root, host, label="target", log_path=None):
    state = remote_state(remote_root, host)
    if state.get("present") is True:
        return state
    raise _lost(label, state, daemon_hint(label, log_path))


def wait_local(root, deadline_s, label="owner", log_path=None):
    end = time.monotonic() + float(deadline_s)
    while True:
        state = local_state(root)
        if state.get("present") is True:
            return state
        if time.monotonic() >= end:
            raise _lost(label + ":absent_after_%ss" % deadline_s, state, daemon_hint(label, log_path))
        time.sleep(2.0)


def wait_remote(remote_root, host, deadline_s, label="target", log_path=None):
    end = time.monotonic() + float(deadline_s)
    while True:
        state = remote_state(remote_root, host)
        if state.get("present") is True:
            return state
        if time.monotonic() >= end:
            raise _lost(label + ":absent_after_%ss" % deadline_s, state, daemon_hint(label, log_path))
        time.sleep(5.0)
