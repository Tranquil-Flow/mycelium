<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Offline cached startup — wiring seam for SLICE-1 / L8 (round 7)

What exists (crate `artifacts`, no network code — a test asserts it):

| Step | API | Mutates? | v0.1.4 counterpart |
|---|---|---|---|
| open | `Cache::open(root, cap, index_cap, chunk)` (caps from the L1 registry) | creates private root only | `AssignmentArtifactCache(root, max_bytes)` |
| acquire / repair | `Cache::store(&key, &source) -> StoreOutcome {Populated, Reused, Repaired}` | yes: copy, **quarantine** a damaged/foreign file at the object path, keep pins, **LRU-evict unpinned** entries over the cap | `store()` → `"populated"/"reused"/"repaired"`, `_evict_locked` |
| cap enforcement | `Cache::evict()`, `Cache::unpin_assignment(id)` | yes | `evict()`, `unpin_assignment()` |
| stage | `materialize_assignment(id, &required, dest)` | pins + private staging copy | `materialize_assignment()` (refuses a damaged object: it does not repair) |
| offline resolve | `Cache::resolve_offline(&required) -> Result<Vec<(path, VerifiedArtifact)>, StartupRefusal>` | **no** (read-only, all-or-nothing, names every missing/corrupt/foreign path) | (none: v0.1.4 starts from the cache via `materialize_assignment`) |

Wiring contract for the host (SLICE-1) and the loader (L8):

1. **Offline start** = `open` → `resolve_offline(required)`; on `StartupRefusal` show the per-path causes
   (`artifact_missing`, `artifact_corrupt`, `artifact_foreign`, …) and do not load anything. Never call `store`
   from the offline path: with no source there is nothing to re-acquire, exactly as in v0.1.4.
2. **Re-acquire** (online/import step, owned by the host): call `store(key, source)` per refused path. A damaged
   entry is quarantined (`quarantine/<object>.<ns>`) and re-copied; the outcome says `Repaired`. The same call
   evicts least-recently-used unpinned entries when over the cap (a single object larger than the cap is reported
   `Populated` and is gone again — v0.1.4 behaviour, oracle vector `single-object-larger-than-capacity`).
3. **Stage** with `materialize_assignment` (pins), hand `VerifiedArtifact.file` (held, positioned at 0) to L8;
   L8 must still hash at its load boundary (the cache is not protection against a same-UID attacker).
4. **Retire** with `retire_materialization` (removes staging + pins) or `unpin_assignment` (pins only).

v0.1.4 parity (round 8, integrator rulings; replay `tests/cache_repair.rs` + `fixtures/cache-compat.json`, 32
scenarios/274 steps, plus 7 oracle-written index cases/180 entries in `fixtures/index-corpus.json`):

- ACCEPTED as v0.1.4 does: symlinked ancestors of the root or of a staging destination; any mode of the
  staging parent / intermediate directories / `objects/` / `quarantine/` (root is chmodded 0700); missing parents
  (created); hard-linked source, index and lock file; hand-edited entries whose `relative_path`, `key`, `pins` or
  extra fields differ (only size, access time and pins are read); a second lock holder BLOCKS (unbounded).
- RE-OWNED (mutating paths): a hard-linked object is damage here (v0.1.4 reuses it but never creates one - grep of the whole
  oracle: no `os.link` outside an unrelated release script). `store` returns `Repaired`; `materialize_assignment`
  copies the verified bytes to a fresh private 0400 inode. `verify`/`resolve_offline` (read-only, the offline
  start) SERVE it, as v0.1.4's offline start does (round 9: it was `artifact_unsafe`, a hard failure where
  v0.1.4 works): hash/size/type checks unchanged, no mutation, the other name keeps its bytes.
- STILL REFUSED (states v0.1.4 never writes; recorded in `KNOWN_DIVERGENCES`, V03 rows in `L7-PLANNER.md`): a symlink
  INSIDE the root (`objects/`, `quarantine/`, index, lock file); an entry id that is not `sha256:` + 64 hex (v0.1.4
  would unlink outside `objects/`); a non-numeric `size_bytes` (v0.1.4 fails with TypeError at its next
  status/store/evict); one object under two staged paths (v0.1.4's own `validate_materialization_report` rejects
  the manifest it writes, `unassigned_object_count = -1`).
- Behaviour fixed to match v0.1.4: `materialize_assignment` refreshes the access time of the objects it stages.
