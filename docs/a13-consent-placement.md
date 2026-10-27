# Consent-aware A13 placement

`mycelium.a13_layer_split.v1` is unchanged: its members, stages and self-digest use the existing field set and canonical digest calculation. Consent limits are planning inputs, not additions to the split document or execution authority. The original memory/microbench split is retained when feasible. Otherwise a deterministic memoized search considers every contiguous nonempty partition in the same member order, preferring the closest cumulative weighted boundary and then the smaller boundary. Candidate and final admission use the same per-stage resource derivation, including whole-snapshot disk overhead versus slice-only mode.

## Capability report extension

`mycelium.a13_capability_link_report.v1` accepts one optional top-level signed field, `caps`. When present it is a nonempty closed object containing `storage_bytes`, `max_memory_bytes`, or both; each value must be a positive integer no larger than `2**63 - 1`. Unknown nested fields and booleans are rejected. The signature covers this object, and the verified owner inbox preserves it. Reports without the field remain accepted; absent limits are unknown, never an implicit larger consent.

The application producer sends locally persisted standing limits intersected with its storage policy. The owner's own local contributor is checked against the same intersection, including an existing standing memory limit. Generation/expiry checks remain mandatory for the compile inputs. No consent is granted, widened or rewritten by planning. A changed or previously unknown limit can still be refused at member acceptance.

All Macs should use the same application release: older strict report readers do not accept this optional extension from a newer sender.
