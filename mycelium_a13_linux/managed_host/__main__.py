# SPDX-License-Identifier: AGPL-3.0-only
"""Linux managed host agent CLI (the target side of an A13 managed route).

The same commands and consent gates as the Windows host CLI
(``mycelium_a13_windows.managed_host``), with the Linux application::

  mycelium -m mycelium_a13_linux.managed_host --root ~/.local/state/mycelium/host status
  ... pair-request --policy policy.json --out request.json   # give request.json to the owner
  ... accept-handoff handoff.json                          # the owner's approval
  ... report-capability
  ... descriptor --run-id R --deployment-id D --manifest-digest sha256:... --acknowledge --out card.json
  ... preview --policy policy.json --limits limits.json --out preview.json
  ... run --policy policy.json --limits limits.json --preview preview.json --acknowledge

``run`` accepts exactly the previewed work and keeps serving until the owner
requests cleanup, Ctrl+C, or SIGTERM from the service runner (which drains and
keeps staging).  Unattended operation is ``mycelium_a13_linux.service_install``.
"""

from __future__ import annotations

import signal
import sys

from mycelium_a13_windows.managed_host.__main__ import main as _host_main

from .app import LinuxTargetApplication, linux_installation_descriptor


def _drain_on_sigterm(_signum, _frame):
    # The POSIX service runner (and systemd) stop the host with SIGTERM: drain like Ctrl+C.
    raise KeyboardInterrupt


def main(argv=None) -> int:
    previous = signal.signal(signal.SIGTERM, _drain_on_sigterm)
    try:
        return _host_main(argv, app_factory=LinuxTargetApplication, descriptor=linux_installation_descriptor,
                          description=__doc__)
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
