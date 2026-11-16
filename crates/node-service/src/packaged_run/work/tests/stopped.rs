// SPDX-License-Identifier: AGPL-3.0-only
//! Lane CHATPAGE item 3 (ledger rows 106 f and 107 item 5; REPORT-laneCHATS item 5): after the
//! owner's Stop on rv39 and rv40, Mac B's standing card kept `phase: serving`. v0.1.4's teardown
//! announces `stopped` once the route's cancel and the target cleanup had their allowance
//! (demo_chain.py:1381-1390 @ f4e366e9: "`stopped` is an instruction, not just status: target
//! agents tear down their control runtime on receipt", only when `ctx.state['model']` is set, a
//! failure recorded `owner_stop_announce_failed`); v0.2's `PackagedChainPorts::teardown` never
//! announced it, and the owner's idle refresh cannot replace a `serving` announcement
//! (`owner_ports::ensure_exchange` refreshes only over `idle`/`stopped`). Quit stops the run
//! through the same teardown (`RunClose`, `OwnerRun` Stop), so this is also the card after Quit.
//!
//! Model-free: the fixture's two packaged hosts on loopback, the joined Mac's real standing agent
//! (`MemberAgent`) reading the owner's signed announcement over the seed's TLS.
use super::serving_r2::run_state;
use super::*;
use owner_run::owner_chain::ChainPorts as _;
use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};

/// `ctx.state['model']` as the model step leaves it (an inert fixture; nothing is loaded).
pub(super) fn chosen_model() -> ModelStepOutput {
    ModelStepOutput {
        forced_slice: false,
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        manifest_digest: "sha256:00".into(),
        slice_mode: true,
        choice: None,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "constructor-only inert source fixture".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 0,
            },
            manifest_digest: "sha256:00".into(),
            source_bytes: json!(0),
        },
    }
}

/// The joined Mac's card after one standing-loop pass.
fn card(f: &mut Fixture) -> Value {
    f.agent.tick(&f.member);
    f.agent.status().expect("the joined Mac has a card")
}

/// Proves: CHATPAGE item 3 (the joined Mac's card reads `stopped` after the owner's Stop).
#[test]
fn a_joined_macs_card_reads_stopped_after_the_owners_stop() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports.work, "run-stopped", "dep-stopped");
    state.model = Some(chosen_model());
    // The serving step's announcement: what Mac B's card held after Stop and Quit on rv39/rv40.
    owner_run::steps::exchange::announce(&mut ports.reports, &state, "serving", None).unwrap();
    let serving = card(&mut f);
    assert_eq!(serving["phase"], "serving", "{serving}");
    // The owner's Stop: the run's teardown (`chain.teardown(ctx, failed=False)`).
    let mut env = Env { at: Instant::now() };
    assert_eq!(ports.teardown_run(false, &state, &mut env), Ok(()));
    let stopped = card(&mut f);
    assert_eq!(
        (&stopped["phase"], &stopped["state"], &stopped["run_id"]),
        (&json!("stopped"), &json!("stopped"), &json!("run-stopped")),
        "the joined Mac's card after the owner's Stop: {stopped}"
    );
}

/// v0.1.4 announces `stopped` only for a run whose model step ran (`if ctx.state.get('model') is
/// not None`, demo_chain.py:1387): a run that ended before it leaves the announcement as it was.
#[test]
fn a_run_stopped_before_its_model_step_announces_nothing() {
    let mut f = Fixture::new();
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let state = run_state(&mut f, &ports.work, "run-early", "dep-early");
    let before = card(&mut f);
    assert_eq!(before["phase"], "compile", "{before}");
    let mut env = Env { at: Instant::now() };
    assert_eq!(ports.teardown_run(true, &state, &mut env), Ok(()));
    let after = card(&mut f);
    assert_eq!(after["phase"], "compile", "{after}");
}
