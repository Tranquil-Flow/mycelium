"""Prove a run used no ssh -- or enumerate exactly where it did.

Criterion 3 of PLAN.md section 3 requires an acceptance run in which the targets are
driven only through their own product UI/API. The chain is ssh-driven today
(`round14_rpc.py`: "One remote RPC over the canonical ssh/scp pattern"), so "no ssh"
has to be MEASURED rather than asserted -- the same discipline that made the worker
guard report its overshoot (e389a84) and the seed window report its numbers (07408a3).

Mechanism: a PATH shim. `install()` writes a tiny `ssh` that appends every invocation
to a log and then execs the real binary. Put the shim directory first on PATH for the
whole run; `report()` counts afterwards. A run that never shells out to ssh leaves the
log missing or empty.

Scope: this observes the operator's own process environment. It cannot see a child
that calls `/usr/bin/ssh` by absolute path or clears its log; it is an audit of the
orchestration we control, not a jail.
"""
import os
import stat
from pathlib import Path

SHIM_DIR = Path('/tmp/asm/ssh-shim')
DEFAULT_LOG = '/tmp/asm/ssh-calls.log'
REAL_SSH_ENV = 'A13_SSH_REAL'

_SHIM_BODY = """#!/bin/sh
# Counted by scripts/a13_operator/ssh_audit.py: log the call, then behave as ssh.
log="${A13_SSH_LOG:-%s}"
mkdir -p "$(dirname "$log")" 2>/dev/null
printf '%%s %%s\\n' "$(date -u +%%Y-%%m-%%dT%%H:%%M:%%SZ)" "$*" >> "$log" 2>/dev/null
exec "${%s:-/usr/bin/ssh}" "$@"
""" % (DEFAULT_LOG, REAL_SSH_ENV)


def install(shim_dir=SHIM_DIR, log=DEFAULT_LOG):
    """Write the shim; return the env overrides the audited run must inherit."""
    shim_dir = Path(shim_dir)
    shim_dir.mkdir(parents=True, exist_ok=True)
    shim = shim_dir / 'ssh'
    shim.write_text(_SHIM_BODY)
    shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return {'PATH': '%s:%s' % (shim_dir, os.environ.get('PATH', '')),
            'A13_SSH_LOG': str(log)}


def calls(log=DEFAULT_LOG):
    """Every recorded ssh invocation, oldest first. Absent log == no calls."""
    path = Path(log)
    if not path.exists():
        return []
    return [line for line in path.read_text().splitlines() if line.strip()]


def report(log=DEFAULT_LOG):
    """The audit verdict for a finished run."""
    found = calls(log)
    return {
        'ssh_calls': len(found),
        'log': str(log),
        'calls': found,
        'verdict': 'NO_SSH' if not found else 'SSH_USED',
    }


def reset(log=DEFAULT_LOG):
    """Clear the log so a run starts from a known state."""
    path = Path(log)
    if path.exists():
        path.unlink()
    return str(path)
