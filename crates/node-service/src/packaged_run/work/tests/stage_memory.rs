// SPDX-License-Identifier: AGPL-3.0-only
//! Lane A, release blocker B7 (CAP-1, CAP-4, CAP-5; REVIEW-laneCAP-astra.md, ledger rows 90/93).
//!
//! CAP-1: at the demo model's 2,356-token serving context the stage worker's own reservation
//! (`native_selection::stage_reservation`: 3,063,554,560 B for layers [0,19], 2,548,746,752 B for
//! [19,28]) is ABOVE the compile's modelled `max_rss_bytes` (2,703,888,274 / 2,477,956,596 B,
//! `2 x resident + 1 GiB + kv`, x 1.15). The member used to discard the share it admitted and run
//! its worker at its whole consent, which hid the gap. The fix plans the worker's own reservation
//! in the compile AND enforces the admitted share on the member, together.
//!
//! The figures below are written out, not recomputed by the helpers under test (CAP-5): the native
//! reservations and the modelled rows come from the static review's independent integer
//! arithmetic over the source formulas (REVIEW-laneCAP-astra.md, CAP-1 table).
use super::*;

/// Native minimum logical reservation at 2,356 tokens (review table, independent arithmetic).
const RESERVED_0_19: u64 = 3_063_554_560;
const RESERVED_19_28: u64 = 2_548_746_752;
/// The compile's MODELLED `max_rss_bytes` at 2,356 tokens (review table): below the reservation.
const MODELLED_0_19: u64 = 2_703_888_274;
const MODELLED_19_28: u64 = 2_477_956_596;
const CONTEXT: u64 = 2_356;
const JOINED_CAP: u64 = 8_589_934_592;

fn demo_request(
    owner: [usize; 2],
    joined: [usize; 2],
    context: u64,
) -> planner::chain_compile::BuildRequest {
    planner::chain_compile::BuildRequest {
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        deployment_id: "d".into(),
        route_label: "a13",
        stage_sharded: false,
        compilation_cache_key: "k".into(),
        node_ids: vec!["owner".into(), "joined".into()],
        managed_layer_ranges: vec![owner, joined],
        layer_split: json!({}),
        serving_context_tokens: context,
    }
}
/// A run state as the compile step leaves it: the owner's own node is "owner" (its consent is its
/// contributor's grant; the compile's records carry no caps for it), the joined Mac's caps come
/// from its record, and each stage's announced share is the compile's resource row.
fn demo_state(joined_cap: Option<u64>, shares: Option<(u64, u64)>) -> RunState {
    let mut state = RunState::new("run-cap", "dep-cap", owner_run::Reason::Start);
    state.participants = vec!["owner".into(), "joined".into()];
    state.local_node = Some("owner".into());
    let caps = joined_cap.map_or(json!({}), |c| json!({"max_memory_bytes": c}));
    state.member_records = Some(json!({"owner": {"memory_bytes": 17_179_869_184u64},
        "joined": {"memory_bytes": 17_179_869_184u64, "caps": caps}}));
    if let Some((owner, joined)) = shares {
        state.compile_state = Some(json!({"resource_plan": {"members": [
            {"node_id": "owner", "start": 0, "end": 19, "max_rss_bytes": owner},
            {"node_id": "joined", "start": 19, "end": 28, "max_rss_bytes": joined}]}}));
    }
    state
}
fn refusal(result: Result<(), StepError>) -> Option<(String, String)> {
    match result {
        Ok(()) => None,
        Err(StepError::Refused(e)) => Some((e.code, e.message.unwrap_or_default())),
        Err(other) => panic!("{other:?}"),
    }
}

/// CAP-5: the stage worker's own reservation at the product context, written out.
#[test]
fn the_stage_workers_reservation_at_the_serving_context_is_pinned_independently() {
    use crate::native_selection::{Profile, stage_reservation};
    let at = |range: std::ops::Range<u32>| {
        stage_reservation(
            "Qwen/Qwen3-0.6B",
            Profile::PackedInt8,
            range,
            28,
            CONTEXT as u32,
        )
        .unwrap()
    };
    assert_eq!(at(0..19), RESERVED_0_19);
    assert_eq!(at(19..28), RESERVED_19_28);
    const { assert!(RESERVED_0_19 > MODELLED_0_19 && RESERVED_19_28 > MODELLED_19_28) };
}

/// CAP-1 (compile half): the packaged composition gives the compile the stage worker's own
/// reservation, so the share it announces is the figure the member's worker needs.
#[test]
fn the_packaged_composition_plans_shares_with_the_stage_workers_reservation() {
    let f = Fixture::new();
    let composed = Composed::new(&f.host, None, None);
    let hook = lock(&composed.compiler.0)
        .stage_reservation()
        .expect("the packaged compile plans with the stage worker's own reservation");
    assert_eq!(
        hook("Qwen/Qwen3-0.6B", [0, 19], 28, CONTEXT),
        Some(RESERVED_0_19)
    );
    assert_eq!(
        hook("Qwen/Qwen3-0.6B", [19, 28], 28, CONTEXT),
        Some(RESERVED_19_28)
    );
    assert_eq!(
        hook("Qwen/Qwen2.5-0.5B-Instruct", [0, 12], 24, CONTEXT),
        None
    );
}

/// CAP-1 (slices half): the check holds each stage to the limit its member will ENFORCE, its
/// consent tightened by the share the compile announced for it. A share below the stage's own
/// reservation (the unreconciled modelled plan) is refused here by name, before any push; with
/// the reconciled shares the same stages pass.
#[test]
fn the_slices_check_holds_each_stage_to_the_share_its_member_enforces() {
    let f = Fixture::product_contributor();
    let contributor = owner_ports::contributor_dir(&f.root.join("owner"));
    let request = demo_request([0, 19], [19, 28], CONTEXT);
    let tokens = CONTEXT as usize;
    let unreconciled = demo_state(Some(JOINED_CAP), Some((MODELLED_0_19, MODELLED_19_28)));
    let (code, message) = refusal(check_run_stage_memory(
        &request,
        &unreconciled,
        &contributor,
        tokens,
    ))
    .expect("a share below the worker's reservation is refused at slices");
    assert_eq!(code, "stage_memory_consent_below_context");
    assert!(
        message.contains("Mac owner") && message.contains(&MODELLED_0_19.to_string()),
        "names the Mac and the limit it would enforce: {message}"
    );
    let reconciled = demo_state(Some(JOINED_CAP), Some((RESERVED_0_19, RESERVED_19_28)));
    assert_eq!(
        refusal(check_run_stage_memory(
            &request,
            &reconciled,
            &contributor,
            tokens
        )),
        None
    );
}

/// CAP-4: the PRODUCTION consent sources, not a closure written by the test. The compile's
/// records carry no caps for the owner's own node: its limit is its contributor's grant exactly as
/// the contributor step leaves it (4 GB). At the oracle's 4,096 tokens its 19 layers need 6.79 GB,
/// so the run is refused naming the owner's Mac; a joined Mac whose signed caps are short is
/// refused naming it.
#[test]
fn the_slices_check_reads_the_contributors_grant_and_the_joined_macs_caps() {
    let f = Fixture::product_contributor();
    let contributor = owner_ports::contributor_dir(&f.root.join("owner"));
    let tokens = 4096;
    let owner_short = demo_state(Some(JOINED_CAP), None);
    let (code, message) = refusal(check_run_stage_memory(
        &demo_request([0, 19], [19, 28], 4096),
        &owner_short,
        &contributor,
        tokens,
    ))
    .expect("the contributor's 4 GB cannot hold 19 layers at 4,096");
    assert_eq!(code, "stage_memory_consent_below_context");
    assert!(
        message.contains("Mac owner") && message.contains("4000000000"),
        "{message}"
    );
    let joined_short = demo_state(Some(2_000_000_000), None);
    let (code, message) = refusal(check_run_stage_memory(
        &demo_request([0, 19], [19, 28], CONTEXT),
        &joined_short,
        &contributor,
        CONTEXT as usize,
    ))
    .expect("a joined Mac whose caps cannot hold its stage");
    assert_eq!(code, "stage_memory_consent_below_context");
    assert!(
        message.contains("Mac joined") && message.contains("2000000000"),
        "{message}"
    );
    // Both consents large enough: no refusal.
    assert_eq!(
        refusal(check_run_stage_memory(
            &demo_request([0, 19], [19, 28], CONTEXT),
            &demo_state(Some(JOINED_CAP), None),
            &contributor,
            CONTEXT as usize,
        )),
        None
    );
}

/// Lane A patch 2, A-04 (REVIEW-laneA-release-astra.md): the negative boundary through the
/// production slices preparation, `Work::prepare`, not the helper. Only the compile's completion
/// is supplied ([`COMPLETED_REQUEST`]: its native challenge needs the real model); the consent
/// sources are the product's (the owner's contributor grant as the contributor step leaves it, a
/// joined Mac's signed caps in its record). An insufficient grant is refused by name, naming the
/// Mac, before anything downstream: no owner work composed, no shard document read, no push, no
/// prepare. With both consents sufficient the same preparation passes the memory check and stops
/// only at the seam's end (`test_completed_request_only`), so the refusal above is the check's.
#[test]
fn the_slices_preparation_refuses_an_insufficient_grant_by_name_before_any_push() {
    let f = Fixture::product_contributor();
    let composed = Composed::new(&f.host, None, None);
    let prepare = |request: planner::chain_compile::BuildRequest, state: &RunState| {
        COMPLETED_REQUEST.with(|c| *c.borrow_mut() = Some(request));
        let result = composed.work.prepare(
            state,
            &mut Env { at: Instant::now() },
            &composed.compiler,
            None,
        );
        COMPLETED_REQUEST.with(|c| c.borrow_mut().take());
        refusal(result)
    };
    let run = |joined_cap: u64| {
        let mut state = demo_state(Some(joined_cap), None);
        state.model = Some(super::stopped::chosen_model());
        state
    };
    let request = |context: u64| {
        let mut request = demo_request([0, 19], [19, 28], context);
        request.deployment_id = "dep-cap".into();
        request
    };
    let downstream = || lock(&composed.work.resident).work.is_some();
    // The owner's contributor grant (4 GB) cannot hold layers [0,19) at 4,096 tokens (6.79 GB).
    let (code, message) = prepare(request(4096), &run(JOINED_CAP))
        .expect("the contributor's grant is refused at the slices preparation");
    assert_eq!(code, "stage_memory_consent_below_context", "{message}");
    assert!(
        message.contains("Mac owner") && message.contains("4000000000"),
        "{message}"
    );
    assert!(!downstream(), "nothing composed after the refusal");
    // A joined Mac whose signed caps cannot hold its stage at the serving context.
    let (code, message) = prepare(request(CONTEXT), &run(2_000_000_000))
        .expect("the joined Mac's caps are refused at the slices preparation");
    assert_eq!(code, "stage_memory_consent_below_context", "{message}");
    assert!(
        message.contains("Mac joined") && message.contains("2000000000"),
        "{message}"
    );
    assert!(!downstream(), "nothing composed after the refusal");
    // Control: both consents sufficient, the check passes and the preparation goes on to the
    // compile's artifact (the seam's end).
    let (code, _) = prepare(request(CONTEXT), &run(JOINED_CAP)).expect("the seam's end");
    assert_eq!(code, "test_completed_request_only");
    assert!(!downstream());
}
