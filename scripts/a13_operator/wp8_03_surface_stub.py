# SPDX-License-Identifier: AGPL-3.0-only
"""A stub app surface: the app's control-file contract, for the wp8-03 rehearsal only.

Why a stub is enough to make this rehearsal REAL rather than simulated. The operator's liveness
check depends on exactly one observable: `mycelium_a13_macos/app.py` writes
`native-control.json` at startup (:294) and UNLINKS it on shutdown (:771). A genuine SIGTERM to
this process therefore produces the same on-disk event as a genuine SIGTERM to the app -- the
control file disappears, and the daemon's log carries its last markers.

It deliberately does NOT serve the app's API: the property under rehearsal is the surface
lifecycle and the operator's reaction to it, not the API. Keeping it to the contract means the
rehearsal needs no model, no laptop and no packaged build.
"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

CONTROL = 'native-control.json'


def main(argv=None):
    parser = argparse.ArgumentParser(description='wp8-03 surface stub')
    parser.add_argument('--root', required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--port', type=int, default=54321)
    args = parser.parse_args(argv)

    root = Path(args.root)
    log = Path(args.log)
    control = root / CONTROL

    def note(marker):
        with open(log, 'a') as stream:
            stream.write(marker + '\n')
            stream.flush()

    # Markers chosen from round14_surface._MARKERS so the operator's evidence extraction has
    # something real to find.
    note('launcher_started')
    control.write_text(json.dumps({'protocol': 'mycelium.a13.surface_stub.v1',
                                   'port': args.port}, sort_keys=True))
    os.chmod(control, 0o600)
    note('LAUNCH stub surface written')

    stopping = {'now': False}

    def _stop(_signum, _frame):
        stopping['now'] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    while not stopping['now']:
        time.sleep(0.05)

    # The app unlinks its control file on shutdown; mirroring that is the whole point.
    try:
        control.unlink()
    except OSError:
        pass
    note('packaged_app_closed')
    note('OWNER_DAEMON_EXIT code=0')
    return 0


if __name__ == '__main__':
    sys.exit(main())
