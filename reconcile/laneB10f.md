# Lane B10f — REVIEW-laneB10 backlog: Stop at the compile commit point, cancellable scans

Branch lane/b10f-stop from m4/main b76d44b. Closes REVIEW-laneB10 F1, F2 and the uncancellable-path list.

Ruling 10(b) additive changes; no wire, signed, stored or stage-contract byte changed; route_ready stays false.
- F1 `owner_run::steps::compile::run_compile_step`: a Stop check directly before `retire_bootstrap`
  and directly before `start_build` (nothing between check and side effect). A Stop observed before
  a side effect prevents it; the step ends `Stopped`, as a Stop during the build wait does. The 907
  recorded oracle cases (no Stop) are unchanged. The Stop event's lock is deliberately not held across
  the host calls: `Runner::stop` sets the event while holding the run lock, so holding it over host IO
  would make status wait on `retire_bootstrap` and invert lock order with host locks.
- `api::native_compiler::Config::from_environment_until` (crate-private): the worker-binary pin at
  build start hashes with the build's cancel flag (`compiler_stopped`). `from_environment` (used by
  the joined-shard native start) keeps the plain read.
- headless `install_compiled_handoff`: `revalidate_until` consults the host state (closed, target,
  fenced) once per chunk; a stopped host refuses with the existing `compiled_handoff_not_installable`
  before hashing further. Test-only caller today; change kept local to that function.
- headless `Compiler::signal_stop` (teardown's first step, split out); F2's model test uses it to
  read the worker's own verdict before teardown.

Left as is, with reasons:
- `/api/serving/compile` (`compiler.rs` `public_compile` -> `acquire()`): the preflight runs under
  the compiler mutex and its only Stop owner is the chain teardown, which needs that same mutex.
  Making it observe Stop needs either a lock-free stop token set by `HeadlessChainPorts::teardown`
  before it locks (headless.rs, outside install_compiled_handoff, owned by lane B12f this round) or
  moving the manifest re-hash out of the mutex (a custody change). Neither can get a genuine RED
  here: no in-process fixture reaches a prepared acquisition plus an admitted two-host observation
  (same gap as B10). Worst case today: Stop or quit waits for one manifest re-hash after the S1
  snapshot verify. Suggested follow-up: stop token on `SharedCompiler`, hash outside the mutex with
  the compiler's cancel clone, fixture from a prepared local Qwen3-0.6B acquisition.
- `api/src/acquisition.rs:696` `prepared_source` -> `verify_snapshot` -> `digest_file:477`: S1
  RESERVED, not edited; already in reconcile/laneB10.md (suggested `prepared_source_until`).
