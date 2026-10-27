# Consent-aware A13 placement

`mycelium.a13_layer_split.v1` is unchanged: its members, stages and self-digest use the existing field set and canonical digest calculation. Consent limits are planning inputs, not additions to the split document or execution authority. The original memory/microbench split is retained when feasible. Otherwise a deterministic memoized search considers every contiguous nonempty partition in the same member order, preferring the closest cumulative weighted boundary and then the smaller boundary. Candidate and final admission use the same per-stage resource derivation, including whole-snapshot disk overhead versus slice-only mode.

## Capability report extension

`mycelium.a13_capability_link_report.v1` accepts one optional top-level signed field, `caps`. When present it is a nonempty closed object containing any of `storage_bytes`, `max_memory_bytes`, and `max_wall_seconds`; each value must be a positive integer no larger than `2**63 - 1`, and wall seconds must also be in the standing-consent range 60–3600. Unknown nested fields and booleans are rejected. The signature covers this object, and the verified owner inbox preserves it. Reports without the field remain accepted; absent limits are unknown, never an implicit larger consent.

The application producer sends locally persisted standing limits intersected with its storage policy. The owner's own local contributor is checked against the same intersection, including an existing standing memory limit. Generation/expiry checks remain mandatory for the compile inputs. No consent is granted, widened or rewritten by planning. A changed or previously unknown limit can still be refused at member acceptance.

All Macs should use the same application release: older strict report readers do not accept this optional extension from a newer sender.

## Consented worker wall time

At owner compilation, each participant (including the owner's local contributor) gets `min(owner proposal, consented max_wall_seconds, 3600)`. The snapshot is retained as `worker_wall_limits`; the run-wide `work_limits.max_wall_seconds` is the minimum and each `work_nodes` entry carries that member's exact limit. The work announcement carries these same limits for slice and whole-snapshot workers. The standing acceptor uses the announced wall time, rather than unconditionally using its own potentially larger grant. If consent has since become smaller, it refuses with `member_wall_cap_below_share`, not a silently shortened worker that invalidates the owner's schedule. Invalid wall values refuse with `member_wall_limit_invalid`.

A missing wall cap uses **60 seconds**, the lowest allowed standing allowance, with the affected node IDs recorded as `wall_cap_fallback_nodes` and the compile detail `Time limit unknown for N Macs; using 60 seconds per run.` This avoids treating old/incomplete reports as consent to the owner's longer proposal; all existing target-side consent checks still apply. It can cause short runs or a startup ceiling if preparation takes too long. Upgrade every member to report its actual cap rather than relying on this conservative compatibility fallback.

Both owner guards use the smallest worker wall limit, anchored no later than work publication. With the default 120-second rollover margin, 600-second consent rolls at 480 seconds and reaches the hard owner ceiling at 570 seconds. For 60 seconds, the margin is capped at half the allowance: rollover and ceiling are both at 30 seconds, with continuation checked first while serving. The member's independent native cleanup reserve remains unchanged; this is not in-place extension of a retired worker's deadline.
