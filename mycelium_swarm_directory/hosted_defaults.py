# SPDX-License-Identifier: AGPL-3.0-only
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
#: Apex proxy bounds, matching the directory behind it (32 slots, 10 s): live connections, per-socket-operation
#: timeout, and helper-zip downloads streaming at once (each can be up to 1 GiB over the owner's uplink).
PROXY_CONNECTION_SLOTS = 32
PROXY_SOCKET_TIMEOUT_SECONDS = 10
PROXY_MAX_CONCURRENT_DOWNLOADS = 2
#: Largest request body the proxy forwards to the directory (the directory itself allows 8 KiB).
DIRECTORY_MAX_BODY_BYTES = 16 * 1024

#: Open-mode guard rails enforced by the swarm's entry (per UTC day, whole swarm, all keys together).
SWARM_DAILY_REQUESTS = 200
SWARM_DAILY_TOKENS = 40_000
#: Per-address share of the same day (open mode): one address, however many throwaway keys, cannot take the whole
#: swarm-wide budget.  IPv6 clients count by their /64.  In memory only: a restart forgets it.
IP_DAILY_REQUESTS = 40
IP_DAILY_TOKENS = 8_000
OPEN_GRANTS_PER_IP_PER_DAY = 12
#: Access modes the swarm-wide budget applies to (owner-approved modes are bounded by their own grants).
SWARM_CAP_ACCESS_MODES = ('open',)

#: launchd labels of the prepared (not installed) units.
DIRECTORY_LAUNCHD_LABEL = 'now.mycelium.swarm-directory'
DIRECTORY_WATCHDOG_LAUNCHD_LABEL = 'now.mycelium.swarm-directory-watchdog'
#: Seconds between watchdog probes, and consecutive failures before a restart is requested.
WATCHDOG_INTERVAL_SECONDS = 60
WATCHDOG_FAILURES_BEFORE_RESTART = 3

#: The packaged helper app shown on the page.  Unavailable until a signed package is actually served somewhere:
#: the page then says "not published yet" instead of linking nothing.  Owner decision 8 (un-notarized
#: right-click-Open download) is the ``notarized: False`` here; set ``available``/``url``/``sha256`` to publish.
HELPER_DOWNLOAD = {'available': False, 'url': None, 'sha256': None, 'version': '0.1.0', 'notarized': False}
