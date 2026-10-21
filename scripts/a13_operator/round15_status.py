# SPDX-License-Identifier: AGPL-3.0-only
"""Wait for each packaged app's private control surface, then read /api/status.

Readiness signal is native-control.json (written only once the app has opened its
local authenticated surface). Cold target start can spend time on its obsolete
relay before opening that surface, so a missing file early is not a failure.
"""
import json
import subprocess
import sys
import time
from pathlib import Path

from round14_rpc import OWNER, REMOTE, REMOTE_HOST, owner, target


def _remote_control_present():
    cmd = ['ssh', '-T', '-o', 'BatchMode=yes', REMOTE_HOST,
           'test -f ' + json.dumps(REMOTE + '/target/native-control.json')]
    return subprocess.run(cmd, capture_output=True).returncode == 0


def wait_local(deadline_s):
    p = Path(OWNER) / 'native-control.json'
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if p.exists():
            return True
        time.sleep(2)
    return False


def wait_remote(deadline_s):
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if _remote_control_present():
                return True
        except Exception:
            pass
        time.sleep(5)
    return False


def main():
    deadline = float(sys.argv[1]) if len(sys.argv) > 1 else 420.0
    results = {}
    for label, waiter, fn in (('OWNER', wait_local, owner), ('TARGET', wait_remote, target)):
        ok = waiter(deadline)
        print(label, 'control_surface=' + ('present' if ok else 'ABSENT'), flush=True)
        if not ok:
            results[label] = None
            continue
        last = None
        for _ in range(10):
            try:
                # /api/status is a GET route; passing a body would make it a POST.
                st = fn('/api/status')
                results[label] = st
                print(label, 'STATUS', json.dumps(st), flush=True)
                break
            except Exception as exc:
                last = repr(exc)[:400]
                time.sleep(6)
        else:
            results[label] = None
            print(label, 'STATUS_UNAVAILABLE', last, flush=True)
    Path('/tmp/round15-status.json').write_text(json.dumps(results))


if __name__ == '__main__':
    main()
