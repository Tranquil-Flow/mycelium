# L6 application lifecycle seam (SLICE-1 / S3–S5, S13)

`node::Core` is the durable reservation authority. Do not add a second lifecycle
clock or infer cleanup from a retired request generation. All methods are synchronous
bounded state transitions; run process/network/device effects outside SQLite locks.

## Wall-timed continuation

1. Open with `Core::open_with_policy` and the L1-backed SQLite wait; authenticate
   join/consent/lease inputs with L5. Plan via the selected L7 adapter, then execute
   `Core::start` effects and report actual worker start origins using `started`.
2. Drive `Core::tick(Now)` from the application's existing owner timer. `Now.mono`
   must keep one clock domain and never go backwards; `Now.wall` is wall time used
   for signed expiry translation. Tests inject both clocks, not reduced budgets.
   Handle `Renew` normally; a lease renewal never enlarges worker authority.
3. `state().rollover_at` is L1's earliest worker deadline minus effective rollover
   lead. At that instant `tick` emits `Stop` effects and durably captures the exact
   member identities in `state().rollover_members`. It does **not** start a new run.
4. Route each Stop to its exact ticket-bound `Supervisor`. Repeatedly obtain
   `observe_cleanup`; call `Core::cleaned` with the real environmental observation.
   All reservations remain until process **and** every inventoried resource owner
   prove absence. Use `CleanupDomains` for staging/device owners; a signed remote
   ACK alone is never sufficient. Observe on a background driver, not the UI thread.
5. Once the core is idle with no reservations, collect **one** complete authenticated
   set of remote descriptors/leases (exclude the owner, whose local authority stays
   local). Call `continue_rollover(&[RolloverReport], planner, now)` exactly once for
   that collection. A matching set performs fresh report + plan + start in one
   persisted revision and returns normal Start effects. Execute them as in step 1.
   The caller owns descriptor collection/transport; this method never polls peers.

A lost, duplicate, replaced or missing member descriptor yields
`rollover_members_changed`, persists the diagnostic, consumes the continuation,
and returns no Start effects. **No retry in v0.2**: BACKLOG item 1 / V03-PLAN lane 3
owns changing this known v0.1.4 behavior. Provenance: `demo_chain.py:1117–1141` and
`owner_run.py:351–377 @ f4e366e9`. Other planning/transport failure retry policy is
still the application owner's responsibility, not a newly added policy here.
An explicit Stop or reopening the owner revokes automatic continuation. A restart
must first recover the exact old ownership and finish cleanup, then explicitly plan.

## Stop with cleanup proof

`let stop = core.request_stop(now)?` commits cancellation, revokes continuation and
returns `StopRequest { fence, effects }`. Dispatch every Stop, including all cleanup
domains even if one fails. Poll environmental observations and pass them to
`cleaned`. `core.finish_stop(&stop.fence)` returns `StopProof` only after the same
owner/epoch/run is idle, unplanned and has no reserved workers. Before that it
returns `cleanup_unproven`; an old fence cannot settle a successor.

The proof has private fields and public `epoch()` / `run()` accessors. It is a local
Core proof, **not** `router::cleanup::CleanupProof`: SLICE-1 must additionally join
router request-terminal/transport shutdown proof, actual L8 stage/device ownership,
and any application staging domains before advertising complete application Stop.
No node-service file is changed by this seam. The real-child composition test uses
two inert native children per cohort, fake clock rollover, cleanup, a new cohort,
and explicit Stop. It is not Metal, QUIC, HTTP or installed-app qualification.

## Persistent ownership across owner death

`guardian::PersistentChild::spawn(guardian_executable, command, resource_root,
state_database, unique_journal_name, ticket)` starts the same-build `node-guardian`
helper. It retains a real unreaped `GroupChild` in a separate process group. The
application's exclusive pipe write end is the lifetime capability: explicit Stop,
Drop or application death closes it, so the guardian stops its owned group, reaps,
proves absence and durably publishes completion. No saved PID is signalled. The
launch configuration goes through the private pipe; argv/environment are not
stored in the journal. The helper is a Rust binary, not an interpreter or shell.

After owner death, reopen Core (which keeps old reservations cleanup-unproven),
then use `guardian::recover(database, journal_name, &old_ticket)` and pass its
`observe_cleanup()` result to `Core::cleaned`. Recovery only reads durable exact-
ticket observations; while the original owner still holds the pipe, it cannot
revoke that owner's capability. It never turns a journal PID into kill authority.
A completed journal also refuses a recreated/replaced resource path. A failed or
Prepared/uncertain journal never releases a reservation. Unique journal names
cannot be reused, and an old proof cannot settle a new Core epoch/run.

**Boundaries:** launch/Drop may wait; execute them on the background lifecycle
owner. This adapter accepts only a trusted group-contained executable, not
arbitrary fork/setsid escapes. `WorkerCommand` explicitly supplies only an absolute
program, argv and environment overrides/removals; guardian owns cwd and stdio.
There are no silently discarded `std::process::Command` options. The packaged
helper path/build identity
must be bound by SLICE-1/L11; this lane does not install or qualify a package.
Guardian failure/death itself is not automatically recoverable; it fails closed.
The worker's independent wall/cancel protocol and device resource owner remain
mandatory. There is no claim that a process-group signal proves external Metal,
remote process, staging or unregistered resource cleanup. Compose those owners
through `CleanupDomains` before issuing the application-level Stop result.

Tests kill the original owner process, observe automatic worker/group cleanup,
reopen real Core state, retire its retained reservation with the recovered proof,
admit a successor and reject the old proof. An unrelated live child remains alive;
a real descendant and foreign resource residue are covered separately. The
historical `recovery::RecoveredGroup` is intentionally unchanged: old numeric-only
journals still cannot acquire signal authority. New launches must use the guardian
to gain post-owner-death termination, rather than silently upgrading old records.

## Compatibility

Public methods are additive. `State` adds a serde-defaulted `rollover_members`
field; old persisted JSON reopens, but downstream Rust struct-literal constructors
must add that field. No wire format, consent value, deadline or lockfile changed.

## Child descriptors: exactly stdio (`fds.rs`)

Every spawn in this crate (`NativeChild`, `GroupChild`, the `node-guardian` launcher; the
guardian's worker goes through `GroupChild`) first calls `node::process::confine_descriptors`.
A worker or guardian receives **only descriptors 0, 1, 2**; the guardian's fd 0 is the
exclusive lifetime pipe. macOS cannot create sockets/pipes close-on-exec atomically, so any
listener, accepted connection or lifetime pipe that a library or another thread has not yet
marked would otherwise follow a child past rollover/restart (`AddrInUse`, no FIN, no EOF).
v0.1.4's Python `close_fds=True` never allowed this.

- Do **not** bind or accept sockets and then spawn through your own `Command`. Route every
  long-lived child through these adapters, or call `confine_descriptors(&mut command)?` as
  the **last** registration on your `Command` (it must run after any caller `pre_exec`).
- It is the crate's single `unsafe` boundary: fork-child `fcntl` marking (async-signal-safe,
  no allocation), fail-closed when the soft `RLIMIT_NOFILE` is unavailable/infinite.
- Residual: a descriptor another thread creates **and closes** between `fork` and `exec` is
  still held by that not-yet-exec'd child for that interval; no in-process mechanism removes
  it (`POSIX_SPAWN_CLOEXEC_DEFAULT` has the same interval and cannot carry `Command` intent).
  A caller that rebinds a just-closed address must still order it after in-flight spawns.

## Liveness: no effect or wait under the Core lock

`lifecycle.rs`, `continuation.rs` and `types.rs` hold no locks, threads, sockets, waits or
adapters (`tests/liveness.rs` scans them). Compose as: `Arc<Mutex<Core>>`; observe cleanup
**outside** the lock (`Supervisor::observe_cleanup`); lock only for `cleaned`, `tick`,
`report`, `consent`, `state().clone()`. A hung observation is bounded by the stop deadline
(`tick` → `CleanupUnproven`/`worker_stop_timeout`, reservation kept); a late exact-ticket
proof still settles it, and a stale/other ticket or residue cannot.
**Planners run outside the lock (round 7, `tests/planner_purity.rs`).** `Core::plan` and
`continue_rollover` remain as conveniences, but they are now exactly request -> planner ->
apply, so the planner still runs under the *caller's* lock. A planner that can block, does
I/O, or reads the Core through the shared lock must use the split API instead:
`let req = core.lock().plan_request(now)?;` (`&self`, immutable `PlanningRequest`), run
`planner.plan(req.members())` with the lock released, then `core.lock().apply_plan(req, plan, now)?`
(rollover: `rollover_request(&reports, now)` / `finish_rollover(req, plan, now)`). `apply` refuses
(`planning_request_stale`) unless the live state and revision are exactly those of the request, re-checks
every lease at the apply instant and re-validates the plan against the requested members. Rust cannot forbid a
trait implementation from blocking, and no registry budget bounds planner time, so purity of the
INLINE path is a documented contract plus a source test (exactly one `planner.plan(` call site per entry
point), not a runtime guard; a detection timeout would need a new L1 budget id (seam request).
