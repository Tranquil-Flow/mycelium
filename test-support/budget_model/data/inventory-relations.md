<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Reviewed static relations and unresolved hazards

All citations below refer to Python commit
`f5bfb70981569c79cb873afe8e0c5be20342db7e`. These are code/configured
relations, not measured p99s or proof of device close behavior.

## Cleanup partition and F16

`mycelium_deadlines.py:188-234,438-469` currently declares cancellation plus
cleanup at 8,000 ms, with a 3,500 ms fallback reserve. Thus route fanout is
4,500 ms and the peer-advertised bound is `4500 - 200 - 100 = 4200 ms`.
Command interruption cleanup is the same 8,000 ms envelope. Rust derived
formulas preserve those relations; tests use the current declarations instead
of the stale 2-second / 0.5-second / 1.5-second explanatory docstrings.
This only partitions a budget; it does not demonstrate that the peer meets it.

The existing F16 deterministic close-wait model remains
`0.5 + 4 + 8 + 1 + 2 * (2 * 930 + 30) = 3793.5 seconds`, while the demo-chain
previous-close margin is 120 seconds. The modeled sum exceeds that margin by
3,673.5 seconds. Existing rejection tests are retained. A sum of configured
waits is not a guaranteed or measured close bound; unknown close stays blocked.
Rollover must reserve `stop + start + safety` (not just the advertised close
number), and worker work cannot consume its cleanup reserve. See the retained
L1 coherence/model tests for those strict inequalities.

## Relay call-path distinctions

`mycelium_a13_macos/public_relay.py:298-306,324,339,347,395-403`:

* The lower-level per-window constructor accepts 30–3,600 seconds; the app quick
  surface's maximum is separately 1,800 seconds. These are separate call-path
  constraints, not equal-valued IDs to collapse or a proposed behavior change.
* A quick relay has a 86,400-second total cap; the external relay policy permits
  up to 604,800 seconds unless its explicit `long_lived` policy is selected.
* Renewal lead is `min(60 seconds, window / 3)`, with a renewal opportunity only
  while serving. Do not confuse this with the directory membership lease.
* The `cloudflared --grace-period 5s` child option differs from parent stop/join
  waits. Reader requests 65,537 bytes to detect an accepted-line ceiling of
  65,536 bytes. The log deque is a different retained-item ceiling.

`mycelium_a13_macos/public_https.py:86-87,111-123`: the DNS retry deadline is
`now + min(max(1 second, caller_timeout * 0.5), 5 seconds)`. Each fetch still
gets the full caller timeout. Consequently this retry cap is **not** a total
end-to-end hard bound; the in-progress fetch can outlast the retry deadline,
and the preceding ordinary resolver call has no equivalent explicit bound.
The response reader uses ceiling+1 to enforce a 16,384-byte accepted payload.
These are review findings, not authorized fixes to Python or changed Rust
product behavior.

## Freshness versus membership renewal

`mycelium_deadlines.py:313-319`: qualification freshness is 3,600 seconds and
requalification interval 3,300 seconds. The interval is below the TTL but
leaves only 300 seconds of configured headroom; it is **not** `<= TTL/3`.
That is a different policy from a membership lease. Applying the lease rule to
all TTLs would silently change the strict port. Existing `RenewInterval` versus
`MembershipLease` checks retain the membership-specific one-third rule.

`mycelium_node/reconnect.py:58-94`: heartbeat jitter is interval multiplied by
`1 +/- jitter_fraction`; with the declared 30 seconds and 0.15, the range is
25.5–34.5 seconds. Reconnect delay is capped by maximum backoff and by the
remaining lease; the exponent is separately capped. A default declaration is
not permission to renew or execute work beyond member consent.

## Retention distinctions

`mycelium_a13_macos/managed_offers.py:52-61,149`: preposition file count,
serialized document bytes, per-file and aggregate bytes, and the 256-entry
superseded-offer history all constrain different stores. Stage history is not
a replay-cache alias merely because another limit equals 256. Manifest expiry
and owner/member approval still bind artifact transport.

`mycelium_a13_macos/mailbox_retention.py:11-18,70-96`: authentication replay
retention derives from issued-at, accepted TTL and skew; the permanent replay
history is a bounded bitmap with deliberately conservative false positives.
Do not conflate history capacity, signed TTL, body bytes, and retained reply
count. The current snapshot records declarations and keeps unresolved dynamic
expiry expressions in the pending queue rather than assigning them guessed
constants.

`mycelium_swarm_directory/ratelimit.py:10-38,50-74`: per-minute directory limits
use a 60-second **sliding** window with an 8,192-key retention cap. The daily
budget has a separate 8,192-key cap and evicts the oldest key. Source comments
explicitly say restart forgets daily counters and the swarm-wide budget is the
backstop. A retained-key cap alone is not a persistent per-subject spend cap.

## Meaning of BLOCKED

The new source-backed values do not supply measurements. Unknown operational
phases are explicitly unclassified, and even a p99-shaped generic profile must
not qualify them. A negative-control test uses deliberately invented metadata
only to assert refusal; it is not evidence. The full inventory also remains
BLOCKED because source review/discovery closure is unfinished. Neither a
passing consistency suite nor these static equations discharges that gate.
