<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# L7 offline artifact cache

Unix-only cache with explicit caller/L1-owned capacity, index and buffer limits. Object keys
match the pinned Python cache identity. A cache quota is not an execution grant. There is no
network fallback and no model download.

**v0.1.4 parity.** v0.2 upgrades v0.1.4 installs in place, so the cache opens every on-disk
state `AssignmentArtifactCache` (f4e366e9) writes, and `store` repairs and evicts as it does:

- Root: a symlink ABOVE the root works (the root is canonicalised once at `open`); the root
  itself may not be a symlink. Missing parents are created (default mode), the root is
  chmodded to 0700, and `objects/` / `quarantine/` keep whatever mode they have.
- Staging: the destination's missing parents are created; the parent and every intermediate
  directory of a nested staged path may have any mode (v0.1.4 leaves the default mode, i.e.
  the user's umask).
- Hard links: a source, the index or the lock file with several names is accepted (v0.1.4 never
  creates one). A hard-linked OBJECT is served by the read-only offline
  startup (`verify`/`resolve_offline`: hash, size and type checks unchanged, nothing mutated), as
  v0.1.4's offline start stages it; the mutating paths converge it: `store` re-acquires it
  (`Repaired`), `materialize_assignment` copies its verified bytes to a fresh private inode.
- Lock: a second holder blocks until the first operation ends, unbounded, as `flock(LOCK_EX)`
  does in v0.1.4 (V03 characterization `tests/lock_contention.rs`).
- Index entries are read as v0.1.4 reads them (size, access time, pins; everything else carried
  through verbatim). The id of each entry must be `sha256:` + 64 lowercase hex.
- Still refused (states v0.1.4 never writes): a symlink inside the root, a malformed entry id
  (v0.1.4 would unlink outside `objects/`), a non-numeric entry size, one object under two
  staged paths (v0.1.4's own report validator rejects the manifest it writes). See
  `OFFLINE-STARTUP-SEAM.md`.

Publication holds the blocking lock, verifies length and SHA-256, checks file identity
before/after reading, writes/fsyncs a private temporary file, and atomically publishes bytes
and then the index. An interruption between those commits leaves an unindexed object, which is
never served and is quarantined by the next `store` of its key. All I/O, unlock and
temporary-cleanup failures remain explicit.

A verified object is returned as an open read-only file positioned at zero. This avoids a
required pathname re-open; it does **not** isolate a hostile process with the same Unix
identity. L8 still verifies at load boundaries; its current path-only interface needs a
held-file adapter.

## Assignment staging

`materialize_assignment` verifies each required content identity, copies only that
closure into an owned private temporary tree, verifies copied SHA-256/length,
writes the Python-compatible `assignment-materialization.v1` manifest, and
persists assignment pins before atomic directory publication. Missing/foreign
objects and an existing destination refuse without publishing a partial stage.
The caller owns a private, single-writer destination parent and one staging
lifecycle per assignment ID, and must already have admitted the staging footprint.
No download, implicit reservation, route readiness or model load is performed.

`verify_materialization` validates the exact manifest and file inventory, cached
identities/pins and staged digests, returning held read-only files. `status` matches
the recorded cache-status shape. Close/reopen and shared-object pins are exercised.
`retire_materialization` removes only a fully verified assignment tree, then unpins
its assignment. The caller must first stop all consumers; this helper cannot prove
worker/Metal cleanup or release L6 capacity. Foreign descendants, corruption and
incomplete removal remain errors. Pins may survive interrupted publication or
retirement: automatic crash recovery and the L8/SLICE-1 production held-file consumer are
**not qualified** here.

## Explicit real-byte qualification

`cached_model` is an explicit ignored-by-default qualification test, requiring the
already-cached pinned Qwen2 checkpoint. It verifies real model bytes through
store/reopen/held-file use without modifying the source or downloading anything.
It does not start an inference backend, load tokenizer assets or qualify an app's
offline startup. Ordinary tests use small deterministic local byte fixtures.
