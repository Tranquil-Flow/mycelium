# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every default of the hosted mycelium.now swarm demo, in one place.

These are the recommended defaults of the hosted-swarm-demo plan.  Each one is
only a default: the proxy flags, ``EntryLimits`` and the directory CLI all
override it, and nothing here is read from the environment.  Owner decisions
(open-mode limits, hosting resilience) change these numbers, not the code.
"""
from __future__ import annotations

#: Path the directory page and its API are mounted at on the apex origin.
DIRECTORY_MOUNT_PREFIX = '/swarms'
#: Loopback port of the always-on directory unit (``python -m mycelium_swarm_directory``).
DIRECTORY_PORT = 4360
#: Public origin the directory advertises (links, QR codes, config).
DIRECTORY_PUBLIC_ORIGIN = 'https://mycelium.now'
#: Loopback port of ``mycelium_demo.public_origin_proxy`` (the tunnel's apex origin).
PROXY_PORT = 4350
#: Largest request body the proxy forwards to the directory (the directory itself allows 8 KiB).
DIRECTORY_MAX_BODY_BYTES = 16 * 1024

#: Open-mode guard rails enforced by the swarm's entry (per UTC day, whole swarm, all keys together).
SWARM_DAILY_REQUESTS = 200
SWARM_DAILY_TOKENS = 40_000

#: launchd labels of the prepared (not installed) units.
DIRECTORY_LAUNCHD_LABEL = 'now.mycelium.swarm-directory'
DIRECTORY_WATCHDOG_LAUNCHD_LABEL = 'now.mycelium.swarm-directory-watchdog'
#: Seconds between watchdog probes, and consecutive failures before a restart is requested.
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_FAILURES_BEFORE_RESTART = 3
