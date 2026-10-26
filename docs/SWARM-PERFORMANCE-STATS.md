<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Optional public swarm performance statistics

`POST /swarms/api/v1/stats` accepts `mycelium.swarm.stats.v1`, separately signed with the existing canonical Ed25519 format. The exact envelope is `protocol, swarm_id, signer_key, at, values, sig`. `stats_docs.py` defines the closed numeric payload and all bounds. Integers only: durations in milliseconds; throughput in milli-tokens/second. The mode is a closed enum, not a free-text field. Statistics contain no prompts, outputs, IP addresses, hostnames, node IDs or node keys. The envelope identifies the already-public swarm signer.

## Compatibility and retention

Heartbeat v1 is unchanged. A v0.1.3 app continues to heartbeat normally. A new app posts statistics **after** its ordinary heartbeat; an old directory's refusal, collection error or telemetry timeout does not fail publication or liveness. Future telemetry formats need a separate versioned document, not an unchecked extension field. Both code lines use identical `stats_docs.py`; neither changes `docs.py`.

The directory verifies the signature, current owner/entry authorization, timestamp skew and increasing `at`. There is one bounded latest document per existing swarm, at most 8 KiB, within existing swarm and write limits. Model or entry changes clear the displayed report, while retaining the replay high-water mark. Deletion removes statistics with the listing. List and detail responses expose `stats: {values, updated_at, age_seconds, stale}` or null. Stats become stale after 90 seconds or whenever the listing is not serving. The page does not use stale numbers for live comparisons. A signature authenticates the reporter, **not** the accuracy of its claim.

## Measurements and windows

- App-chat first-token time: admission to first registry token event, including local owner delivery overhead. p50/p90 use nearest rank.
- Decode speed: `(token_events - 1) / (last_token_time - first_token_time)`. One-token answers have no decode-speed sample. This is not browser-observed speed or a hardware-only benchmark.
- End-to-end: app admission through terminal completion, in seconds in the UI. Only successfully completed requests contribute; failed and cancelled requests do not.
- Request metrics: last 50 completed requests, no older than one hour, on the current owner run. Each metric carries its own sample count; missing is null/n=0, not a fabricated zero. A run change clears percentiles; a late old-run completion cannot enter the new run's percentiles.
- Last-24-hour request count: bounded completion timestamps across app-chat routes. Coverage says how much of the day was observed. All aggregates reset on app restart; they are not durable usage accounting and may overlap across listings.
- Serving since/uptime: observed by the publishing app from its first serving heartbeat, reset on observed non-serving status or run identity change. Not a continuous-availability guarantee between heartbeats.
- Layer ranges: current owner-run participants, sorted, numbered anonymously, start-inclusive/end-exclusive. Unsupported/incomplete topologies are unavailable, not truncated. At most 32 stages.
- Mode: the actual constructed live route's token-path owner (`token_path_enabled`), not the current environment preference. Missing/unreachable/simulated sources say unknown.
- Stage compute/token: existing validated A10 publications from `/__mycelium/live-status`, only unbatched decode compute intervals mapped to the matching peer layer placement. Up to 512 intervals per stage within one hour; duplicate intervals counted once. Durations use differences on **one Mac's monotonic clock**. Publication wall anchors only age samples. Missing publication, prefill, batched and old intervals are excluded.

## Hop time: measured RTT with explicit scope

The app first projects existing, closed-validated iroh activation-plane `transport_path_observations` onto actual consecutive stage placements. Only fresh, resolved observations proving connection reuse are accepted. The number is the latest reported `warm_rtt_ms` estimate between those endpoints, not one-way activation handoff time. Node IDs, endpoint IDs, relay identities and addresses remain local.

When an actual adjacent activation observation is unavailable, use the existing route idle health probe over the owner-to-member control session. `MeasuredRttTable` already backs runtime RTT tracking; a separate bounded table stores only **verified successful idle health probes**, so model setup, request execution and rejected responses cannot masquerade as probes. Timing brackets command send through response on the owner's monotonic clock. There are no extra network calls: publication only reads the existing samples. This is labeled **Network round trip (probe)** and explicitly includes mailbox polling plus health-command execution, an upper bound on pure network RTT. It is not misrepresented as a consecutive-stage or one-way measurement.

The closed hop shape is `from_stage, to_stage, kind, rtt_ms, samples, age_seconds`. `kind` is `activation_rtt` (adjacent indices) or `owner_control_rtt_probe` (`from_stage=null`, displayed as Owner control). A destination has at most one report, with actual activation RTT preferred. RTT is the latest estimate, **not p50/p90**; probe sample count is the retained last 64, transport count comes from the observation. Values older than 90 seconds are excluded; the page also ages each sample between refreshes. Owner-local control probes are excluded by the publisher. A one-Mac route or a route without a recent valid sample says **Not measured**.

Fake-clock production-route tests verify nonempty hops after an authenticated health probe, rejection of failed verification, TTL expiry, and no extra sends while publishing. Sidecar-fixture tests check adjacent transport projection, freshness and wrong-node exclusion. These are tests of the measurement path, not a new two-Mac physical latency benchmark. Sandbox screenshots use an explicitly synthetic listing to exercise the UI.
