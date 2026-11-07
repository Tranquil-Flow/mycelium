// SPDX-License-Identifier: AGPL-3.0-only
//! Round 7 qualification of v0.1.4's prompt worker-death detection (`ManagedWorker._watch`,
//! 0.25 s, `worker_native_exited`) on the real native path: a real release owner, member and
//! owner-leaf over signed Iroh NodeControl/artifact/Router; the exact worker pid is killed
//! mid-job and the job must fail promptly with v0.1.4's code and verified cleanup.
//! Explicit isolated run: `--release --features metal` with the same three environment
//! variables as `native.rs` (the evidence directory must be fresh).
#![cfg(feature = "metal")]
#[path = "support/death.rs"]
mod death;
use budgets::BudgetId as B;
use death::{Case, Kill, Owner, Role, assert_prompt_and_clean, duration, inject};
use serde_json::Value;
use std::{
    os::unix::process::CommandExt,
    path::PathBuf,
    process::{Command, Stdio},
    time::Instant,
};

/// RU-020: what a killed native worker leaves behind, read from the owner's evidence only.
fn worker_kill_evidence(
    native: &std::path::Path,
    case: &Case,
    outcome: &death::Outcome,
    scratch: &std::path::Path,
) {
    let settled: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/failed-settle.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(settled["terminal"], "failed", "{settled}");
    assert_eq!(settled["error_code"], "worker_native_exited");
    assert_eq!(settled["signed_cleanup_quorum"], true);
    assert_eq!(
        settled["receipts"],
        serde_json::json!(["b-member", "a-owner"])
    );
    assert_eq!(settled["agents"]["survived_to_close"], true);
    assert_eq!(settled["died"]["pid"], outcome.killed);
    assert_eq!(
        settled["died"]["role"],
        if case.role == Role::Member {
            "member"
        } else {
            "owner_leaf"
        }
    );
    assert!(settled["died"]["signal"] == 9, "{settled}");
    let stats = if case.role == Role::Member {
        &settled["member_stats"]
    } else {
        &settled["owner_leaf_stats"]
    };
    assert_eq!(stats["worker_exit"]["reaped"], true, "{stats}");
    assert_eq!(stats["worker_exit"]["pid"], outcome.killed);
    // The surviving peer closed its own stage normally.
    let other = if case.role == Role::Member {
        &settled["owner_leaf_stats"]
    } else {
        &settled["member_stats"]
    };
    assert_eq!(other["context_closed"], true, "{other}");
    // The dead worker's agent removed the member's staged copy itself.
    if case.role == Role::Member {
        assert_eq!(stats["artifact"]["staged_file_removed"], true, "{stats}");
    }
    let diagnostic: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/native-exit.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(diagnostic["error_code"], "worker_native_exited");
    assert_eq!(diagnostic["node_id"], case.role.node());
    assert_eq!(diagnostic["pid"], outcome.killed);
    assert!(!native.join("generation-1/result.json").exists());
    // A signed quorum replaced the local-reap fallback.
    assert!(!native.join("generation-1/local-reap.json").exists());
    // Next Start: generation 2 completed and equals the recorded oracle.
    let summary: Value =
        serde_json::from_slice(&std::fs::read(native.join("summary.json")).unwrap()).unwrap();
    assert_eq!(summary["runs"][0]["terminal"], "failed", "{summary}");
    assert_eq!(summary["runs"][1]["terminal"], "completed", "{summary}");
    assert_eq!(summary["runs"][1]["generation"], 2);
    assert_eq!(summary["runs"][1]["oracle_equal"], true);
    assert!(native.join("generation-2/result.json").exists());
    assert!(
        !std::fs::read_dir(scratch).unwrap().any(|e| e
            .unwrap()
            .file_name()
            .to_string_lossy()
            .starts_with("slice-private-")),
        "case {}: private state survived a passing qualification",
        case.name
    );
}

fn run(case: &Case) {
    let checkpoint =
        std::env::var("MYCELIUM_SLICE_CHECKPOINT").expect("explicit checkpoint required");
    let tokenizer = std::env::var("MYCELIUM_SLICE_TOKENIZER").expect("explicit tokenizer required");
    let evidence = PathBuf::from(
        std::env::var("MYCELIUM_SLICE_EVIDENCE").expect("fresh evidence directory required"),
    )
    .join(case.name);
    std::fs::create_dir_all(&evidence).unwrap();
    let scratch = evidence.canonicalize().unwrap().join("tmp");
    std::fs::create_dir(&scratch).unwrap();
    let native = evidence.join("native");
    let log_path = evidence.join("owner.log");
    let log = std::fs::File::create(&log_path).unwrap();
    // A worker kill must be followed by the next Start (RU-020): the restart plan fails run 1 and
    // completes run 2. An agent kill ends the whole qualification (no signed quorum exists).
    let child = Command::new(env!("CARGO_BIN_EXE_mycelium-node"))
        .args([
            match case.kill {
                Kill::Worker => "qualify-restart",
                Kill::Agent => "qualify",
            },
            &checkpoint,
            &tokenizer,
        ])
        .arg(&native)
        .env("TMPDIR", &scratch)
        .process_group(0)
        .stdin(Stdio::null())
        .stdout(log.try_clone().unwrap())
        .stderr(log)
        .spawn()
        .unwrap();
    let mut owner = Owner {
        child,
        log: log_path,
        begun: Instant::now(),
        workers: Vec::new(),
        natives: Vec::new(),
    };
    let outcome = inject(&mut owner, case, duration(B::MemberChosenWall), true);
    let report = serde_json::json!({
        "case": case.name, "after": case.after, "role": case.role.node(),
        "killed_pid": outcome.killed, "code": outcome.code,
        "killed_to_failure_ms": outcome.failure_after.as_millis(),
        "failure_to_exit_ms": outcome.exit_after_failure.as_millis(),
        "owner_exit_success": outcome.exit_success, "workers": owner.workers, "natives": owner.natives,
        "kill": format!("{:?}", case.kill), "detected_ms": outcome.detected_after.map(|d| d.as_millis()),
    });
    std::fs::write(
        evidence.join("injected-death.json"),
        serde_json::to_vec_pretty(&report).unwrap(),
    )
    .unwrap();
    assert_prompt_and_clean(&mut owner, case, &outcome, true);
    if case.kill == Kill::Worker {
        // RU-020: the failed run was followed by a successful next Start.
        assert!(
            outcome.exit_success,
            "case {}: the next Start after the failed run did not complete",
            case.name
        );
        worker_kill_evidence(&native, case, &outcome, &scratch);
        drop(owner);
        std::fs::remove_dir_all(scratch).unwrap();
        return;
    }
    assert!(!outcome.exit_success, "a failed job must not exit cleanly");
    // v0.1.4 records a bounded private diagnostic of the exit before closing the worker.
    let diagnostic: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/native-exit.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(diagnostic["error_code"], "worker_native_exited");
    assert_eq!(diagnostic["node_id"], case.role.node());
    assert_eq!(diagnostic["pid"], outcome.killed);
    // The supervisor may reap the worker before the watch looks: then the cause is unknown.
    assert!(
        diagnostic["signal"] == 9
            || (diagnostic["signal"].is_null() && diagnostic["exit_status"].is_null()),
        "{diagnostic}"
    );
    // v0.1.4 ends a run whose worker died `failed` with its cleanup proven once the worker is
    // reaped (executed oracle FX-SLICE1-post-worker-death): the local reap proof is recorded,
    // names every spawned child as reaped, and states that it is not a signed quorum.
    let reap: Value = serde_json::from_slice(
        &std::fs::read(native.join("generation-1/local-reap.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(reap["died"]["pid"], outcome.killed);
    assert_eq!(reap["died"]["code"], "worker_native_exited");
    assert_eq!(reap["signed_cleanup_quorum"], false);
    assert_eq!(reap["staged_copy_discarded"], true);
    let children = reap["children"].as_array().unwrap();
    assert_eq!(children.len(), case.workers, "{reap}");
    assert!(children.iter().all(|c| c["reaped"] == true), "{reap}");
    for pid in &owner.workers {
        assert!(
            children.iter().any(|c| c["pid"] == *pid),
            "worker {pid} missing from the reap proof: {reap}"
        );
    }
    // The surviving worker's final state is the supervisor's reap, never an orphan.
    assert!(!native.join("generation-1/result.json").exists());
    // No staged model copy (~942 MB) may outlive the failed run; what it was stays as evidence.
    for entry in std::fs::read_dir(&scratch).unwrap() {
        let entry = entry.unwrap().path();
        if entry
            .file_name()
            .is_some_and(|n| n.to_string_lossy().starts_with("slice-private-"))
        {
            assert!(
                !entry.join("member/staging/model.safetensors").exists(),
                "case {}: staged model copy survived the failed run",
                case.name
            );
        }
    }
    let record = native.join("generation-1/staged-discard.json");
    if case.after == "token_signed" {
        // Staging had completed: the discarded copy is the offered source, byte for byte.
        let discarded: Value = serde_json::from_slice(&std::fs::read(&record).unwrap()).unwrap();
        assert_eq!(discarded["matches_source"], true);
        assert_eq!(discarded["removed"], true);
    } else if record.exists() {
        let discarded: Value = serde_json::from_slice(&std::fs::read(&record).unwrap()).unwrap();
        assert_eq!(discarded["removed"], true);
    }
    drop(owner);
    std::fs::remove_dir_all(scratch).unwrap();
}

#[test]
#[ignore = "explicit isolated native qualification: real release owner, two agents, two native workers"]
fn member_worker_killed_before_the_first_token_settles_failed_and_the_next_start_succeeds() {
    // Both agents and both workers are ready and the receipts are open; the owner is about to
    // send the first step. (A worker that dies while its agent idles is noticed at the next step:
    // the agent has no push channel to the owner, see the round-9 report.)
    run(&Case {
        name: "member-worker-before-first-token",
        kill: Kill::Worker,
        after: "token_grant",
        role: Role::Member,
        workers: 2,
    });
}
#[test]
#[ignore = "explicit isolated native qualification: real release owner, two agents, two native workers"]
fn member_worker_killed_mid_job_fails_the_run_from_a_signed_quorum_and_the_next_start_succeeds() {
    run(&Case {
        name: "member-worker-mid-job",
        kill: Kill::Worker,
        after: "token_signed",
        role: Role::Member,
        workers: 2,
    });
}
#[test]
#[ignore = "explicit isolated native qualification: real release owner, two agents, two native workers"]
fn owner_leaf_worker_killed_mid_job_fails_the_run_from_a_signed_quorum_and_the_next_start_succeeds()
{
    run(&Case {
        name: "owner-leaf-worker-mid-job",
        kill: Kill::Worker,
        after: "token_signed",
        role: Role::OwnerLeaf,
        workers: 2,
    });
}
#[test]
#[ignore = "explicit isolated native qualification: real release owner, two agents, two native workers"]
fn member_agent_killed_mid_job_has_no_quorum_and_still_ends_failed() {
    // Not a worker death: the signer itself is gone, so no signed cleanup exists (v0.1.4 would lose
    // its app process too). The local reap proof stays the fallback; the run still ends failed.
    run(&Case {
        name: "member-agent-mid-job",
        kill: Kill::Agent,
        after: "token_signed",
        role: Role::Member,
        workers: 2,
    });
}
