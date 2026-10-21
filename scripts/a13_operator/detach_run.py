#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Detach a long round from this chat session.

macOS has no setsid(1) (checked 2026-09-16), so detachment in this toolchain is
subprocess.Popen(..., start_new_session=True). A physical round outlives a chat session's context,
and a torn-down session process group must not reap the round's daemons.
"""
import subprocess
import sys
import os
from pathlib import Path

# Resolve against the CALLER's cwd, which is well defined; the round itself is launched with cwd=~,
# so an unresolved relative path would die there as "No such file or directory" -- after the log file had
# already been created, which is how this surfaced on the tool's first real use.
script = str(Path(sys.argv[1]).expanduser().resolve())
if not Path(script).exists():
    raise SystemExit('no such script: %s' % script)
log = sys.argv[2] if len(sys.argv) > 2 else '/tmp/asm/detached.out'
allow_append = '--allow-append' in sys.argv
Path(log).parent.mkdir(parents=True, exist_ok=True)
# A round log is evidence and is never overwritten or appended to: interleaved launches make the log
# ambiguous about which launch produced which line.  Pass --allow-append deliberately to opt out.
if Path(log).exists() and Path(log).stat().st_size > 0 and not allow_append:
    print('REFUSING: %s already has content (%d bytes); pass --allow-append to opt out'
          % (log, Path(log).stat().st_size))
    raise SystemExit(4)
handle = open(log, 'ab', buffering=0)
process = subprocess.Popen(
    ['bash', script], stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT,
    start_new_session=True, cwd=os.path.expanduser('~'))
print('DETACHED pid=%d script=%s log=%s' % (process.pid, script, log))
print('monitor: tail -f %s' % log)
