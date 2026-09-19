#!/usr/bin/env python3
"""Detach a long round from this chat session.

macOS has no setsid(1) (checked 2026-09-16), so detachment in this toolchain is
subprocess.Popen(..., start_new_session=True). A physical round outlives a chat session's context,
and a torn-down session process group must not reap the round's daemons.
"""
import subprocess
import sys
import os
from pathlib import Path

script = sys.argv[1]
log = sys.argv[2] if len(sys.argv) > 2 else '/tmp/asm/detached.out'
Path(log).parent.mkdir(parents=True, exist_ok=True)
handle = open(log, 'ab', buffering=0)
process = subprocess.Popen(
    ['bash', script], stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
    start_new_session=True, cwd=os.path.expanduser('~'))
print('DETACHED pid=%d script=%s log=%s' % (process.pid, script, log))
print('monitor: tail -f %s' % log)
