// SPDX-License-Identifier: AGPL-3.0-only
//! CONTROLLED model-free peer: authentic joined identity and signed descriptor,
//! real headless HTTP/Runner dispatch. No native hello or inference claim.
use super::*;
use api::installation_descriptor::{self, Member, Paths, Source};

struct Repository {
    stop: Arc<AtomicBool>,
    thread: Option<JoinHandle<()>>,
}
impl Drop for Repository {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(thread) = self.thread.take() {
            thread.join().expect("owned repository joined");
        }
    }
}

#[test]
fn actual_headless_mounts_observation_and_refuses_missing_native_bootstrap() {
    mounted_observation(false);
}

#[test]
#[ignore = "requires explicitly pinned admitted model-free Rust helper artifact"]
fn actual_headless_owns_configured_bootstrap_and_cancels_it() {
    mounted_observation(true);
}

fn mounted_observation(configured_bootstrap: bool) {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l10-mounted-observation-{}", std::process::id()));
    assert!(!base.exists(), "fresh fixture state");
    fs::create_dir(&base).unwrap();
    fs::set_permissions(&base, fs::Permissions::from_mode(0o700)).unwrap();
    let oracle: Value =
        serde_json::from_str(include_str!("../fixtures/oracle_resources.json")).unwrap();
    let observation = &oracle["policy_cases"][0]["observation"];
    let probe = base.join("probe.sh");
    fs::write(&probe, format!("#!/bin/sh\nprintf '%s' '{observation}'\n")).unwrap();
    fs::set_permissions(&probe, fs::Permissions::from_mode(0o700)).unwrap();
    let (files, _) = tiny_repository::tiny_snapshot();
    let (port, stop, thread) = tiny_repository::serve_repository(files);
    let repository = Repository {
        stop,
        thread: Some(thread),
    };
    let env = [
        (
            "MYCELIUM_L10_TEST_RESOURCE_PROBE",
            probe.display().to_string(),
        ),
        (
            "MYCELIUM_L10_TEST_HUB_ORIGIN",
            format!("http://127.0.0.1:{port}"),
        ),
        // Existing file-admission seam only; never executed as a native worker.
        (
            "MYCELIUM_L10_TEST_SIDECAR",
            exe_dir().join("examples/headless").display().to_string(),
        ),
    ];
    let (owner, member, proxy, edge) = pair_up_with_owner_env(&base, false, &env);
    if configured_bootstrap {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
            .expect("explicit artifact qualification config");
        let document: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        api::preferences::private_state::store(
            &owner.root.join("bootstrap-program.json"),
            &document,
            api::preferences::private_state::private_json_limit().unwrap(),
        )
        .unwrap();
    }
    let grant = json!({"consent":{"download_acknowledged":true,"max_download_bytes":STORAGE,"storage_limit_bytes":4_000_000_000u64}});
    // Retain the existing bounded power-policy retry; the prior failure did not establish its cause.
    let mut retries = 0;
    let (code, answer) = loop {
        let response = owner.post("/api/model/download", &grant);
        if response.0 == 200
            || retries >= 5
            || response.1["error"] != "resource_power_policy_violation"
        {
            break response;
        }
        retries += 1;
        std::thread::sleep(budget(BudgetId::OwnerPoll) * 8);
    };
    assert_eq!(code, 200, "{answer}");
    let prepared = owner.until(
        "actual public acquisition prepared",
        || owner.get("/api/status").1,
        |s| {
            matches!(
                s["acquisition"]["state"].as_str(),
                Some("prepared" | "failed")
            )
        },
    );
    assert_eq!(prepared["acquisition"]["state"], "prepared", "{prepared}");
    let (code, started) = owner.post("/api/demo/start", &json!({}));
    assert_eq!(code, 200, "{started}");
    assert_eq!(started["state"], "running");
    owner.until(
        "actual descriptors stage",
        || owner.get("/api/status").1,
        |s| s["owner_run"]["step"] == "descriptors",
    );
    let report = |signer: &protocol::identity::Identity,
                  current: &Value,
                  swarm: &Value,
                  announcement: &Value,
                  at: f64| {
        assert_eq!(announcement["phase"], "collect");
        let run = announcement["run_id"].as_str().unwrap();
        let deployment = announcement["deployment_id"].as_str().unwrap();
        let binding = Member {
            node_id: current["node_id"].as_str().unwrap().into(),
            endpoint_id: signer.endpoint_id().into(),
            verification_key_digest: signer.verification_key_digest(),
            generation: current["generation"].as_u64().unwrap(),
            incarnation: current["incarnation"].as_str().unwrap().into(),
        };
        let card = installation_descriptor::installation_descriptor(
            signer,
            &binding,
            swarm["swarm_id"].as_str().unwrap(),
            run,
            deployment,
            true,
            &Source::Whole {
                source_root: base.join("CONTROLLED-member-source").display().to_string(),
                manifest_digest: announcement["manifest_digest"].as_str().unwrap().into(),
            },
            &Paths {
                state_root: member.root.display().to_string(),
                endpoint_secret_file: member
                    .root
                    .join("target-identity.json")
                    .display()
                    .to_string(),
                python_executable: exe_dir().join("examples/headless").display().to_string(),
                sidecar_binary: exe_dir().join("examples/headless").display().to_string(),
            },
            at,
        )
        .unwrap();
        json!({"protocol":api::run_exchange::MEMBER_REPORT,"run_id":run,"deployment_id":deployment,"state":"reported","detail":null,
            "model":{"model_id":announcement["model"]["model_id"],"resolved_commit":announcement["model"]["resolved_commit"]},"progress":null,"descriptor":card,"issued_at":at})
    };
    signed_headless_exchange_report(&owner, &member, proxy, Some(&report));
    if configured_bootstrap {
        let path = owner
            .root
            .join("local-contributor/native-bootstrap/state.json");
        let state = owner.until(
            "actual authenticated helper publication",
            || {
                let state = fs::read(&path)
                    .ok()
                    .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
                    .unwrap_or(Value::Null);
                json!({"native":state,"status":owner.get("/api/status").1})
            },
            |s| s["native"]["phase"] == "running" || s["status"]["owner_run"]["state"] == "failed",
        );
        assert_eq!(
            state["native"]["phase"], "running",
            "real configured helper was not mounted: {state}"
        );
        let pid = rustix::process::Pid::from_raw(
            state["native"]["process"]["pid"]
                .as_u64()
                .unwrap()
                .try_into()
                .unwrap(),
        )
        .unwrap();
        assert!(
            rustix::process::test_kill_process_group(pid).is_ok(),
            "owned helper group must really exist"
        );
        owner.until("real signed target poll", || {
            let db = rusqlite::Connection::open_with_flags(owner.root.join("seed-rs/managed/mailbox.sqlite"), rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
            json!(db.query_row("SELECT count(*) FROM managed_auth_requests WHERE protocol='mycelium.managed_control.target_poll.v1'",[], |r|r.get::<_,u64>(0)).unwrap())
        }, |count| count.as_u64().unwrap() > 0);
        assert_eq!(owner.post("/api/demo/stop", &json!({})).0, 200);
        let finished = owner.until(
            "owned helper cancellation and staging cleanup",
            || serde_json::from_slice::<Value>(&fs::read(&path).unwrap()).unwrap(),
            |s| s["phase"] == "closed" || s["phase"] == "cleanup_unproven",
        );
        assert_eq!(finished["phase"], "closed", "{finished}");
        assert_eq!(
            rustix::process::test_kill_process_group(pid),
            Err(rustix::io::Errno::SRCH)
        );
        assert_eq!(
            finished["native_stop_receipt"],
            Value::Null,
            "forced cancellation is not a native Stop receipt"
        );
        assert!(!Path::new(finished["stage_root"].as_str().unwrap()).exists());
        println!(
            "CONTROLLED public configured bootstrap: actual group/poll/cancel verified; second peer has no runtime, no physical OBSERVE/model claim"
        );
        drop(member);
        drop(owner);
        drop(edge);
        drop(repository);
        fs::remove_dir_all(base).unwrap();
        return;
    }
    let status = owner.until(
        "observation outcome",
        || owner.get("/api/status").1,
        |s| {
            s["owner_run"]["state"] == "failed"
                && (s["owner_run"]["error_code"] == "owner_run_step_not_ported"
                    || s["owner_run"]["cleanup_unproven"].is_object())
        },
    );
    let run = &status["owner_run"];
    assert_eq!(run["steps"][0]["state"], "done", "{run}");
    assert_eq!(run["steps"][1]["state"], "done", "{run}");
    assert_eq!(run["steps"][2]["state"], "done", "{run}");
    assert_eq!(run["failed_step"], "observe", "{run}");
    println!("CONTROLLED actual-headless observe boundary: {run}");
    // Desired mount assertion preserved from the archived r27 RED, now coupled
    // to the actual factory/journal/mailbox and signed staging-discovery path.
    assert_ne!(run["error_code"], "owner_run_step_not_ported", "{run}");
    assert_eq!(run["error_code"], "native_bootstrap_unavailable", "{run}");
    assert!(owner.root.join("seed-rs/managed/mailbox.sqlite").is_file());
    let journal: Value = serde_json::from_slice(
        &fs::read(owner.root.join("seed-rs/managed/owner/owner.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(journal["protocol"], "mycelium.a13.owner_control_journal.v3");
    assert_eq!(run["route_ready"], false);
    assert_eq!(
        run["cleanup_unproven"]["exception_class"],
        "offer_cleanup_unconfirmed"
    );
    let db = rusqlite::Connection::open_with_flags(
        owner.root.join("seed-rs/managed/mailbox.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let mut query = db.prepare("SELECT metadata,cleanup_requested,cleanup_ack FROM managed_stage_offers ORDER BY node_id").unwrap();
    let rows = query
        .query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, bool>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        })
        .unwrap()
        .collect::<Result<Vec<_>, _>>()
        .unwrap();
    assert_eq!(rows.len(), run["participants"].as_array().unwrap().len());
    for (metadata, requested, ack) in rows {
        let offer: Value = serde_json::from_str(&metadata).unwrap();
        assert_eq!(offer["run_id"], run["run_id"]);
        assert_eq!(offer["deployment_id"], run["deployment_id"]);
        assert!(
            requested,
            "real cleanup must be requested for every published offer"
        );
        assert!(ack.is_none(), "no fabricated target cleanup acknowledgment");
    }
    drop(query);
    drop(db);
    for step in run["steps"].as_array().unwrap().iter().skip(4) {
        assert_eq!(step["state"], "pending", "{step}");
    }
    let (code, _) = owner.post("/api/demo/stop", &json!({}));
    assert_eq!(code, 200);
    drop(member);
    drop(owner);
    drop(edge);
    drop(repository);
    fs::remove_dir_all(base).unwrap();
}
