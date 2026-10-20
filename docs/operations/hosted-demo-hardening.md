<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Hosted swarm demo: hardening notes and turn-on checklist

Source review: R3 (public demo privacy / abuse review, 2026-10-20). This lane implements M1-M6 and M8 and the probe for M7.

## What changed

| Item | Behaviour |
|---|---|
| M1 | With `--directory-upstream` set, the apex proxy answers 404 for everything outside `/swarms/*` and the `/` redirect, with no upstream connection. `--enable-product-routes` restores the old product surface (and its `X-Mycelium-Authenticated-User` stamp) explicitly. Without a directory mount nothing changes. |
| M2 | While a swarm is published, `/chat/api/send` through the relay needs the same checks as `/swarm-entry/v1/send`: owner kill switch off, swarm open, swarm-wide daily budget, per-address daily budget. Refused: `entry_killed_by_owner`, `entry_chat_requires_entry`, `entry_swarm_daily_cap`, `entry_ip_daily_cap`. The loopback chat page and an unpublished owner are unchanged. |
| M3 | Footer, directory privacy note, demo label, relay status string and the owner tunnel prompt name Cloudflare (TLS termination: IP, timing, plaintext prompts and replies) and the direct-to-entry flow. Not done: the first-prompt interstitial in `swarms.js`. |
| M4 | The apex proxy has `connection_slots=32`, `socket_timeout_seconds=10` (header, body and write) and `max_concurrent_downloads=2` (`503 download_busy`). Flags: `--connection-slots`, `--socket-timeout-seconds`, `--max-concurrent-downloads`. |
| M5 | Start-up now also removes `.checkpoint-*.tmp` files (a crash between `mkstemp` and `os.replace`; they hold the encoded prompt). Already present and pinned by tests: orphaned `*.checkpoint.json` are journaled by digest and discarded when the gateway is constructed, and shutdown discards through the worker's terminal path. The checkpoint stays enabled because A6 recovery freezes it; A6 never resumes one after a restart. |
| M6 | `DailyBudget` per normalised address (IPv4 exact, IPv6 by /64, v4-mapped as v4): 40 requests / 8 000 tokens per UTC day, in memory only, in front of the swarm-wide 200 / 40 000. 12 open grants per address per day. `state` shows the caller's `ip_budget`. A restart forgets the counts. |
| M8 | Directory write budget is charged only after the signature is verified (and the heartbeat signer authorised). Heartbeats have their own budget (`heartbeats_per_minute=1200`). Signed queries never spend either. |

## M7: turn-on checklist (operator, live)

Run only when the demo is being restored; nothing here was run against the network by the lane.

1. `python -m mycelium_swarm_directory.surface_probe --base https://mycelium.now` exits 0 (product routes 404, no `Set-Cookie` on `/swarms/` and its config).
2. cloudflared: log level `warn`, no request-path logging, log file rotated or dropped; credentials file mode 0600; ingress is only `mycelium.now -> http://127.0.0.1:4350` with a catch-all `http_status:404`.
3. The proxy unit does not pass `--enable-product-routes`.
4. Nothing sensitive (state roots, checkpoint directories) under any served path.
5. Check whether Cloudflare caches `application/zip` for `/swarms/download/*`.

## Remaining from R3 (not in this lane)

Interstitial before the first prompt to an unfamiliar swarm (M3), per-address new-record cap and operator delete in the directory (M8), M9 (only if the product routes are ever re-enabled), and section B.
