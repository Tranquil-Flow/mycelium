# Lane B10 — Stop reaches compile preflight and parent-side handoff hashing

Branch lane/b10-cancel from m4/main 8e76e12. Code commit 3496323 (gated), then this docs commit.

Closes REVIEW-laneB6 F1 (parent-side handoff shard hashing not cancellable) and the manifest
half of REVIEW-laneB3 B3-3 (source preflight hashed while Stop could not be observed).

Ruling 10(b) additive API, no existing behaviour or bytes changed:
- `owner_run::steps::compile::CompilePort::compile_inputs_until(stopped)`; default calls the
  old `compile_inputs`, so every other implementor keeps its behaviour. The compile step now
  calls it with the runner's own Stop; an error returned while Stop holds ends the step
  `Stopped` (the same terminal as a Stop during the build wait; the 907 recorded oracle cases
  have no Stop and are unchanged: oracle_compile_step 7/7).
- `api::compiled_handoff::CompiledHandoff::{open_until, revalidate_until}`; `open`/`revalidate`
  are the same reader with no Stop. New refusal code `compiled_handoff_stopped` (internal).
- `native_compiler::build` maps it, and its own checked source/binary pins, to the existing
  `compiler_stopped`. No signed plan, checkpoint, proof or stage-contract byte changed.

Evis reconcile items:
- `api/src/acquisition.rs` (S1 download module) NOT edited: `prepared_source` still verifies
  the whole snapshot (`verify_snapshot` -> `digest_file`, acquisition.rs:696/477) without a
  Stop check. A Stop during that scan waits for it; the manifest re-hash right after now
  returns at its first chunk. Suggested follow-up on Evis: an additive `prepared_source_until`
  whose cancellation does not mark the acquisition failed.
- `/api/serving/compile` (public preflight route) still calls the unchecked `acquire` while
  holding the compiler mutex; there is no Stop owner for that route.
