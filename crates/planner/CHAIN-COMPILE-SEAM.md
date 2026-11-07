<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# SE-L7-CHAIN-COMPILE: the planner API behind owner-run's `compile` step

Oracle: v0.1.4 @ `f4e366e98a1f705dfaf182897044500d290dbd7f`, `demo_chain.py:792-850` (`_step_compile`),
`demo_chain.py:169-221` (`member_capability_records`, `work_limits`), `chain_compile.py:136-300` (`start_compile`),
`model_resources.py`, `layer_split.py`, `compile_cache.py:17-30`. Port: `planner::chain_compile` (sans-IO, no clock, no thread).
SLICE-1 owns `crates/owner-run`; this crate is called through a port that `steps::compile` implements.

## Calls, in the order `_step_compile` makes them

```rust
use planner::chain_compile::*;

// 1. demo_chain.py:795-797. `nodes` = sorted keys of the observation facts.
let records: serde_json::Value = member_capability_records(&nodes, local_record.as_ref(), &joined_reports, now);
//    joined_reports = app.member_capability_reports() (node -> signed-report content, host-verified)
//    LocalRecord { node_id, memory_bytes, available_storage_bytes: Option<&Value>, generation,
//                  grant: Option<GrantCaps>, policy_storage_bytes: Option<u64> }   // owner's own Mac, fresh observation
// 2. announce('compile'); chain_compile.start_compile up to the thread start:
let prepared: Result<PreparedCompile, Refusal> = prepare(&PrepareInput { /* see rustdoc */ });
//    Err(Refusal { code, message }) -> OwnerRunError(code, message)       (message = the oracle's human text or None)
//    Ok -> host: context.retire_bootstrap(); progress(compile_progress(nodes.len()));
//    run the route builder on a worker thread with `prepared.request: BuildRequest`
// 3. the thread's tail, per outcome of the host's builder + workflow:
let state: Value = match built { Ok(out /* BuildOutput */) => prepared.finish(&out), Err(_) => failed_state() };
//    `state` is `app._chain_compile_state`; the step's wait polls it: 'failed' -> OwnerRunError(error_code),
//    never 'compiled' within the Compile budget -> chain_compile_timeout (owner-run's wait; nothing in the planner)
// 4. demo_chain.py:812-850
let adm: Result<Admission, Refusal> = admit(&state, &records, &nodes, local, ceiling.as_ref(),
                                            &|p, c| owner_run::ceiling::proposal_within(p, c));
//    Err: execution_proposal_outside_ceiling | member_wall_limit_invalid (ctx.state['proposal'] is already set then)
//    Ok(Admission): ctx.state.{proposal, worker_wall_limits, wall_cap_fallback_nodes, compile_state, layers},
//                   ctx.result.{compilation_cache_hit, build_seconds}, progress(adm.progress) if Some
```

## Adapter the owner-run port needs (SLICE-1 writes it; nothing in `crates/owner-run` is edited by L7)

```rust
pub trait CompilePort {
    /// Everything start_compile reads from the app: facts, observation hosts, context members and the coordinator's
    /// current record per member (lease_expires_at), prepared source (model contract, manifest, digest, source_bytes),
    /// keys {gossip, load_proof}, deployment id, epoch, joined reports, the owner's device/grant/policy, wall clock.
    fn compile_inputs(&mut self) -> CompileInputs;
    /// host: after prepare() is Ok (context.retire_bootstrap()).
    fn retire_bootstrap(&mut self) -> Result<(), PortError>;
    /// host: the route builder (L8, scripts/build_qwen_live_route.build) + prepare_serving_workflow (L9)
    /// on a worker thread; returns operator_plan_digest, physical_host_ids, cache_hit, build_seconds and, in slice
    /// mode, the slice plan digests.
    fn build(&mut self, request: &BuildRequest) -> Result<BuildOutput, PortError>;
}
```

## What is recorded (FX-L7-chain-compile-r11, 907 cases, `tests/chain_compile.rs`)

Executed v0.1.4 `AppDemoChain._step_compile` (real `start_compile` and its compile thread) on a stub app; stubs only for
the builder, the workflow, `model_layer_profile` (registered shape = the safetensors-header profile, pinned by
`model-headers.json`), the clock, coordinator, consent/policy file reads. Compared per case: capability records,
refusal code + message, builder request (ranges, cache key, split document), `_chain_compile_state`, admission
outputs, progress lines, retire/builder call counts.

## Differences (V03 rows)

- V03-L7-10: an unregistered model contract or a non-`float32` runtime dtype is refused (`model_resources_unregistered`,
  `model_resources_dtype_unsupported`); v0.1.4 registers only models with a resource shape and only float32 contracts.
- Not ported here (host/other lanes): the run files under `chain-runs/<run>`, `compiler_run_reused`, the reaping of a
  retired route, the builder and workflow, `work_limits` max_rss/max_disk (used by the `slices` step), `plan_member_split`
  from safetensors headers (`planner::profile::from_headers` exists).
- Python ints above u64 and non-finite JSON numbers cannot be a `serde_json::Value` (V03-L7-8).
