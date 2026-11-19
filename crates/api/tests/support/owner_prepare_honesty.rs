// SPDX-License-Identifier: AGPL-3.0-only
//! Lane PP diagnostic on unchanged rv46 production code, NOT a claimed RED regression.
//! Native prepare is held at a deterministic barrier, then answers the terminal control error.
//! The real publish step must refuse before targets, activation or serving can complete.
use super::*;
use owner_run::{
    machine::{STEPS, StepState},
    steps::publish::run_publish_step,
};

#[test]
fn a_missing_then_failed_prepare_cannot_complete_publish_or_qualify_the_run() {
    let mut s = swarm(2);
    s.members[1]
        .member
        .script
        .insert("prepare_native", Act::Hold);
    let mut scripts = scripts_emitting(&SCRIPTED);
    scripts[1].prepare_refused = Some("control_call_failed:control_connect_timeout");
    let (mut work, members, _) = challenged(&mut s, Some(challenge(&SCRIPTED)), scripts);
    let _release: Vec<Release> = members.iter().map(|m| Release(m.2.clone())).collect();
    let mut state = serving_run("s4a-run");
    state.proposal = Some(json!({"max_wall_seconds": seconds(B::MemberChosenWall)}));

    // Earlier steps are outside this diagnostic. Feed only their modeled successes; the
    // publish outcome below comes from the real step and the real OwnerWork worker.
    let mut run = OwnerRun::new(Box::new(|| RunIdentity {
        run_id: "s4a-run".into(),
        deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
    }));
    run.start(
        "start",
        None,
        Clock {
            mono: 0.0,
            wall: 0.0,
        },
    )
    .unwrap();
    for (step, _) in STEPS {
        if step == "publish" {
            break;
        }
        run.step_finished(StepOutcome::Ok);
    }
    let publish = std::thread::spawn(move || {
        let mut env = RunEnv::new(Arc::new(AtomicBool::new(false)));
        let outcome = run_publish_step(
            &mut env,
            &mut NoReports,
            &mut work,
            &mut state,
            seconds(B::OwnerPoll),
        );
        (work, state, outcome)
    });
    assert!(
        members[1]
            .2
            .holding("prepare_native", span(B::WorkerStartup))
    );
    assert!(
        !publish.is_finished(),
        "missing result must keep publish pending"
    );
    assert_unready(&run);
    members[1].2.release();
    let (mut work, state, outcome) = publish.join().unwrap();
    let Err(StepError::Refused(error)) = outcome else {
        panic!("terminal prepare refusal must refuse publish: {outcome:?}");
    };
    assert_eq!(error.code, "managed_workflow_failed");
    assert_eq!(
        work.failure_cause().as_deref(),
        Some("control_call_failed:control_connect_timeout")
    );
    run.step_finished(StepOutcome::Refused(error));
    assert_unready(&run);
    assert_eq!(run.snapshot().failed_step, Some("publish"));
    assert_eq!(work.state()["startup"], Value::Null);
    assert!(work.gateway().is_none());
    assert!(ActivationPort::activate(&mut work, state.proposal.as_ref().unwrap()).is_err());
    work.teardown().unwrap();
    for member in &members {
        assert!(
            !member
                .0
                .lock()
                .unwrap()
                .commands
                .iter()
                .any(|c| c == "serve")
        );
    }
}

fn assert_unready(run: &OwnerRun) {
    let status = run.snapshot();
    assert!(!status.route_ready, "{status:?}");
    for ((step, _), row) in STEPS.iter().zip(&status.steps) {
        if matches!(*step, "targets" | "activate" | "serving") {
            assert_eq!(row.state, StepState::Pending, "{step}: {status:?}");
        }
    }
}
