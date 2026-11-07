# Trace v1 → v2 migration

## Contract

`mycelium-sim-v2` records **two distinct runtime read sets**, one for the recorded
mode and one for the corrected control. A set contains exactly the BudgetIds read
by typed runtime accessors, including recursive formula dependencies; each entry
pins value/formula, unit, purpose and full provenance. `entry()` remains const
metadata inspection; runtime evaluation goes through `read_entry()`. The opt-in
`budgets/trace-reads` feature adds thread-local scopes and no policy changes.
Nested captures compose; panic unwinding restores scopes; separate test threads
cannot contaminate each other's sets. The capture is synchronous, not an async
context propagation claim.

Replay validates every pin against current declarations, runs concrete inputs,
and rejects unpinned reads or extra unused pins. Unrelated declarations may be
added/removed without invalidation. Build/lock identity, simulator/PRNG versions,
canonical config/commands/profile inputs (including the full selected saved
profile contents, not only its ID or sample value), ordered outcome and exact failure set
remain mandatory. Corrected mode has its own recorded input/read/outcome pins;
calling `corrected()` is not permission to refresh changed inputs.

## Reproduce the migration (no golden regeneration during tests)

1. Retain `traces/*-v1.json` byte-for-byte. At base
   `a984d632d4733fdaa95d21bfb0a5af98e18a2582`, add only the exporter
   `examples/export_legacy.rs` and run `cargo run -j 2 -p sim --example
   export_legacy -- OUTPUT_DIR`. The exporter checks every full historical
   outcome/failure against v1 and exports the full corrected outcome after its
   contract passes. The committed `fixtures/legacy-outcomes` files were produced
   this way **before** accessor/trace implementation changes, not by v2 tests.
2. Build v2; independently SHA256 the actual `target/debug/sim` and `Cargo.lock`.
   Do not substitute pins read from the input trace for these measurements.
3. For each `F1 F2 F3 F4 F5 F13 F14 F15 F15b F16`, run:
   `sim migrate OLD-v1.json BASE-OUTCOMES.json sha256:EXECUTABLE_SHA LOCK_SHA NEW-v2.json`.
   Output uses create-new: an existing output is an error. The migrator retains
   commands/config/profile/seed, checks all used old values, requires historical
   log/failure equality, records actual full declarations, and then requires both
   complete outcomes to equal the independent base export before writing.
4. Replay every migrated file with the independently measured current identities:
   `sim replay TRACE BUILD LOCK PROFILES historical|corrected`. Historical must
   exit 101 at the incident invariant; corrected must exit 0. Decode, identity,
   read-set or recorded-outcome errors are never credited as historical RED.
5. `trace_migration.rs` compares all ten retained v1 inputs, frozen base exports
   and v2 replay outcomes; normal corpus tests consume v2 with their original
   assertions preserved. See `evidence/l2c-assertion-map.json` for old→new assertion
   locations. Old fixtures remain executable with their source/binary version.

Every Cargo command must obey the project host-priority guard, nice 19 and two
jobs (`CARGO_BUILD_JOBS=2` for fmt). Use owned scratch for TMPDIR. This procedure
needs no Python. The simulator's CLI identity pins are caller-verified, not
executable self-attestation. A new build/lock requires explicit migration or
recording, never silent acceptance of mismatched producer identity.

## Scope

These remain historical/corrected **models**, not executions of historical
products. The read set covers run-time policy evaluation, not budget reads made
by a generator to construct already pinned concrete config/commands. Removing a
generator-only read during minimization correctly removes that dependency.
Full current declaration provenance is introduced explicitly in v2 because v1
stored only values; v1 cannot retroactively attest provenance it never retained.
No model outcome, consent bound, profile observation or policy value is changed.
