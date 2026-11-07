# FX-L1b-r23-seed-domain

External oracle: `f4e366e98a1f705dfaf182897044500d290dbd7f`.
Candidate: exactly `mycelium_seed/coordinator.py:690:20:keyword:invite_expires_at`.
Contract: existing RU-014/RU-051 `source-unbounded`, source-specific `Value::Blocked`
ID `PySourceUnboundedR23SeedInviteExpiryL690C20`. This is one review, **zero numeric ceiling credit**.

## Source argument

`verify_invite` canonical-JSON-decodes builtin values; it requires finite numeric
issued/expiry values, expiry strictly after issued, the invite protocol and valid
signature, then rejects only `now > expiry`. It neither caps the absolute expiry
nor rejects all future-issued invites. The positive-integer TTL convenience minter
also has no maximum. `accept_join` verifies this invite separately from the signed
membership join request and separately mints the member lease. Its original
`float(invite["expires_at"])` flows unchanged to `commit_join`/`consumed_invites`.
The replay consumer reads the nonce; registry pruning uses strict `expiry < now`.
No membership TTL, HTTP-envelope TTL, float representation maximum, or observed
example is substituted for a source magnitude ceiling.

The exact proof rows cover all scoped `invite_expires_at`/`consumed_invites`
occurrences plus token operands, invite payload reads and registry consumers.
Whole-source and line pins are independently re-read by Rust tests. The separate
r23 admission allowlist is one exact key; its prior-gap fixture pins all 76 prior
pending keys and tests the exact difference, not a prefix/subset allowance.

## Genuine external controls

The approved `/opt/homebrew/bin/python3.14 -I -B` ran the pinned oracle with an
allowlisted environment, scratch HOME/TMPDIR and disposable signing identities and
SQLite fixtures. No live configuration, service or network was initialized.
The oracle remained at the pin with empty Git status. Python is not imported or
executed by product, Rust tests, CI or release paths.

External evidence directory:
`L1B-ABC-CHECKPOINT-20261027-evidence/round23/` under the v02 run root.
Final paired raw captures: `seed-controls-5.json` and `seed-controls-6.json`,
byte-identical SHA-256 `c80fa67e104484b48eca8d344576652b5b577c5c14d7d40e9a8c9b27b83a0654`.
Probe `seed_domain_probe.py`: SHA-256
`b87c204c9e2cefec62e87695052e19825489d25fe6bd4ca6bd36eb32d6f1a167`.
The adjacent TSV is a direct projection of these actual captures: 17 cases,
8 accepted and 9 source refusals. Earlier exploratory captures are superseded.

A full join at clock 1000 with TTL 1000000000000 retains invite expiry
1000000001000 while its member lease is 1300. An independently signed invite with
issued 1000000000000 and expiry 2000000000000 is likewise accepted and persisted.
Both replay exactly. Pruning removes neither at current time nor exactly at expiry,
but removes at expiry + 1. Expiry equal to current time is accepted; expired,
wrong-protocol and wrong-key invites are refused with their original source codes.

## Deliberately not promoted

The other five exact coordinator keys remain pending. Real persistence/constructor/
placement controls accept finite leases +1e100 and -1e100 but refuse infinity and
text on restore. A separate bounded-deadline helper refuses previous_deadline 1e100
at issued 1000; it does not thereby bound all stored/member inputs. Those observations
do not complete their writer/decoder/consumer proofs, and no blanket carrier or
source-unbounded verdict is inferred. The raw pre-verification heartbeat expiry
also requires its own accepted-domain and re-read proof.

Local compiled mutation controls are static registry/proof checks, not runtime
behavior or global fault-catalogue credit. Parent admission remains separate from
lane qualification. Any new product ceiling is a V03 decision.
