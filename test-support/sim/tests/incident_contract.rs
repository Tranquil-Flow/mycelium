// SPDX-License-Identifier: AGPL-3.0-only
use sim::{model::*, trace::*};
fn configuration(incident: Incident, mode: Mode, multiplier: u64) -> Config {
    Config::standard(incident, mode, multiplier).unwrap()
}
fn inputs() -> Vec<Input> {
    vec![Input {
        at_ms: 0,
        task: 1,
        command: Command::Begin,
    }]
}
fn progress_contract(incident: Incident) {
    let mode = if std::env::var("SIM_MODEL").as_deref() == Ok("historical") {
        Mode::Historical
    } else {
        Mode::Corrected
    };
    let c = configuration(incident, mode, 1);
    let o = run(&c, &inputs()).unwrap();
    let v = violations(&c, &o);
    assert!(
        v.is_empty(),
        "MODEL {incident:?} violates section-7 contract: {v:?}; reached={:?}",
        o.transitions
    );
    assert_eq!(o.rollovers, 2);
    assert_eq!(o.served, 3);
    assert_eq!(o.successful_chats, 3);
    assert_eq!(o.interrupted_chats, 2);
    assert_eq!(o.cleanup_proofs, 2);
}
#[test]
fn section7_f4_authorized_progress_contract() {
    progress_contract(Incident::F4);
}
#[test]
fn section7_f15_authorized_progress_contract() {
    progress_contract(Incident::F15);
}
#[test]
fn section7_f16_authorized_progress_contract() {
    progress_contract(Incident::F16);
}
#[test]
fn historical_faults_reach_the_distinguishing_transition() {
    for (incident, expected) in [
        (Incident::F4, "forbidden_replacement_attempt"),
        (Incident::F15, "expiry_driven_service_loss"),
        (Incident::F16, "stop_error_swallowed"),
    ] {
        let c = configuration(incident, Mode::Historical, 1);
        let o = run(&c, &inputs()).unwrap();
        assert!(o.transitions.contains_key("stop"));
        assert!(o.transitions.contains_key("close_completion"));
        assert!(o.successful_chats > 0);
        assert!(violations(&c, &o).contains(&expected.into()));
    }
}
#[test]
fn corrected_models_cross_two_rollovers_at_synthetic_close_loads() {
    for incident in [Incident::F4, Incident::F15, Incident::F16] {
        for multiplier in [1, 2, 3] {
            let c = configuration(incident, Mode::Corrected, multiplier);
            let o = run(&c, &inputs()).unwrap();
            assert!(
                violations(&c, &o).is_empty(),
                "{incident:?} {multiplier}x: {o:?}"
            );
            assert_eq!(o.interrupted_chats, 2);
            assert_eq!(
                o.workers.iter().map(|w| w.generation).collect::<Vec<_>>(),
                [1, 2, 3]
            );
            for w in &o.workers {
                let cap = if incident == Incident::F4 {
                    1_800_000
                } else {
                    600_000
                };
                assert_eq!(w.member_expiry_ms - w.member_origin_ms, cap);
                assert_eq!(w.owner_expiry_ms - w.owner_origin_ms, 1_800_000);
                if let Some(t) = w.closed_at_ms {
                    assert!(t <= w.member_expiry_ms.min(w.owner_expiry_ms) - 10_000);
                }
            }
        }
    }
}
#[test]
fn f15_uses_absolute_staggered_member_deadline_not_owner() {
    let o = run(&configuration(Incident::F15, Mode::Corrected, 1), &inputs()).unwrap();
    assert_eq!(o.deadlines[0], (600_000, 1_830_000, 480_000));
    // Independent arithmetic: old member began at zero, owner at 30s; do not
    // reset either origin when the next phase begins or when a wall clock jumps.
    let mut i = inputs();
    i.push(Input {
        at_ms: 300_000,
        task: 0,
        command: Command::WallJump { delta_ms: -900_000 },
    });
    assert_eq!(
        run(&configuration(Incident::F15, Mode::Corrected, 1), &i)
            .unwrap()
            .deadlines,
        o.deadlines
    );
}
#[test]
fn refusal_and_missing_receipts_cannot_fake_progress() {
    for mode in [Mode::AlwaysRefuse, Mode::MissingProof] {
        let c = configuration(Incident::F16, mode, 1);
        let o = run(&c, &inputs()).unwrap();
        assert!(!violations(&c, &o).is_empty());
        assert_eq!(o.rollovers, 0);
        if mode == Mode::MissingProof {
            assert_eq!(o.cleanup_proofs, 0);
            assert!(o.diagnostics.iter().any(|d| d.contains("MissingReceipt")));
        }
    }
}
#[test]
fn concrete_minimization_replays_without_generator_and_rejects_identity_drift() {
    let t = Trace::generate(
        "test-build".into(),
        "a".repeat(64),
        17,
        configuration(Incident::F16, Mode::Historical, 1),
    )
    .unwrap();
    let minimal = t.minimize().unwrap();
    assert_eq!(minimal.commands, inputs());
    let mut persisted: Trace =
        serde_json::from_str(&serde_json::to_string(&minimal).unwrap()).unwrap();
    persisted.seed = 999; // generator changes cannot affect the concrete inputs
    assert_eq!(persisted.replay().unwrap(), minimal.replay().unwrap());
    assert!(
        persisted
            .require_identity("other", &"a".repeat(64))
            .is_err()
    );
    persisted.budget_snapshot.clear();
    assert!(persisted.replay().is_err());
}
#[test]
fn obligations_are_real_l0_ids() {
    let (registry, _) = obligations::checked_in().unwrap();
    for incident in ["F4", "F15", "F16"] {
        assert!(registry.escapes.iter().any(|r| r.id == incident));
    }
    for path in [
        "mycelium_a13_macos/worker.py",
        "mycelium_a13_macos/demo_chain.py",
        "mycelium_a13_macos/chain_compile.py",
    ] {
        assert!(
            registry
                .obligations
                .iter()
                .any(|r| r.id == format!("O:path:{path}"))
        );
    }
}
