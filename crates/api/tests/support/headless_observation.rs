// SPDX-License-Identifier: AGPL-3.0-only
//! Two real headless processes (owner + joined member), each with its own agent: the member signs
//! its own exchange reports and descriptor and runs its own admitted model-free hello helper. No
//! model load or inference claim; one Mac is one physical host for the observation.
use super::*;

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
    // The member is a real headless member with its own agent: nothing is signed for it here.
    let (owner, member, _proxy, edge) =
        pair_up_with_envs(&base, false, &env, &env, &seed_member_cache);
    if configured_bootstrap {
        let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
            .expect("explicit artifact qualification config");
        let document: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
        for instance in [&owner, &member] {
            api::preferences::private_state::store(
                &instance.root.join("bootstrap-program.json"),
                &document,
                api::preferences::private_state::private_json_limit().unwrap(),
            )
            .unwrap();
        }
    }
    let (code, started) = owner.post("/api/demo/start", &json!({}));
    assert_eq!(code, 200, "{started}");
    assert_eq!(started["state"], "running");
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
        // Both peers now run their own helper, so the observation may finish its hello and stop
        // before this Stop: a receipt is then the helper's own STOPPED answer to that stop; a
        // forced cancellation still records none (never a fabricated receipt).
        let receipt = &finished["native_stop_receipt"];
        if !receipt.is_null() {
            assert_eq!(receipt["ok"], true, "{finished}");
            assert_eq!(receipt["result"]["state"], "STOPPED", "{finished}");
            assert_eq!(
                receipt["node_id"], finished["binding"]["target_node_id"],
                "{finished}"
            );
        }
        assert!(!Path::new(finished["stage_root"].as_str().unwrap()).exists());
        println!(
            "public configured bootstrap: actual group/poll/stop verified on two headless processes; no model claim"
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
    let member_node = api::preferences::private_state::load(
        &member.root.join("membership.state.json"),
        api::preferences::private_state::private_json_limit().unwrap(),
    )
    .unwrap()
    .unwrap()["node_id"]
        .clone();
    for (metadata, requested, ack) in rows {
        let offer: Value = serde_json::from_str(&metadata).unwrap();
        assert_eq!(offer["run_id"], run["run_id"]);
        assert_eq!(offer["deployment_id"], run["deployment_id"]);
        assert!(
            requested,
            "real cleanup must be requested for every published offer"
        );
        if offer["target_node_id"] != member_node {
            assert!(ack.is_none(), "no fabricated target cleanup acknowledgment");
        }
    }
    drop(query);
    drop(db);
    // The member itself answers the owner's cleanup request (it staged nothing: removed, signed).
    let acked = owner.until(
        "the member's own cleanup acknowledgment",
        || {
            let db = rusqlite::Connection::open_with_flags(
                owner.root.join("seed-rs/managed/mailbox.sqlite"),
                rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
            )
            .unwrap();
            db.query_row(
                "SELECT cleanup_ack FROM managed_stage_offers WHERE node_id=?1",
                [member_node.as_str().unwrap()],
                |r| r.get::<_, Option<String>>(0),
            )
            .ok()
            .flatten()
            .map_or(Value::Null, |a| serde_json::from_str(&a).unwrap())
        },
        |a| !a.is_null(),
    );
    assert_eq!(
        acked["protocol"],
        "mycelium.controller_remote_cleanup_ack.v1"
    );
    assert_eq!(acked["node_id"], member_node);
    assert_eq!(acked["removed"], true);
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

/// Whole-snapshot mode: a joined Mac needs the owner's pinned snapshot. Its download is S1's (not
/// ported), so - as the stranger path seeds a member's cache - the owner's VERIFIED cache is cloned
/// into the member's target acquisition root before it joins; the member's own acquisition opens it
/// as unverified and re-hashes it itself.
fn seed_member_cache(owner: &Instance, member_root: &Path) {
    prepare_model(owner);
    let serving = member_root.join("target/serving");
    for entry in fs::read_dir(&owner.root).unwrap().flatten() {
        let name = entry.file_name().to_string_lossy().into_owned();
        if !name.starts_with("model-source") || !entry.file_type().unwrap().is_dir() {
            continue;
        }
        let destination = if name == "model-source" {
            serving.clone()
        } else {
            serving.join(&name)
        };
        fs::create_dir_all(destination.parent().unwrap()).unwrap();
        let copied = std::process::Command::new("/bin/cp")
            .arg("-cRp")
            .arg(entry.path())
            .arg(&destination)
            .status()
            .unwrap();
        assert!(copied.success());
    }
    for dir in [member_root, &member_root.join("target"), &serving] {
        fs::create_dir_all(dir).unwrap();
        fs::set_permissions(dir, fs::Permissions::from_mode(0o700)).unwrap();
    }
    // The member opens its own run model's acquisition (without the cached snapshot it reports
    // `member_model_download_not_ported`): it takes the owner's model, `a9-route`, explicitly too,
    // since v0.2's starter is Qwen3-0.6B (B13).
    api::preferences::run_model::save_choice(member_root, "a9-route").unwrap();
}

/// `/api/model/download` from the test repository until this process's model is prepared.
fn prepare_model(instance: &Instance) {
    // The test repository serves the tiny Qwen2 snapshot of `a9-route` (`tiny_repository`): this
    // Mac takes that run model explicitly, through the picker, since v0.2's starter is Qwen3-0.6B
    // (B13, `contracts/divergences.json` `run_model_default`). This test is about the download and
    // the observation, not the default.
    let (code, chosen) = instance.post("/api/run-model", &json!({"choice": "a9-route"}));
    assert_eq!(code, 200, "{chosen}");
    assert_eq!(chosen["selected"], "a9-route", "{chosen}");
    let grant = json!({"consent":{"download_acknowledged":true,"max_download_bytes":STORAGE,"storage_limit_bytes":STORAGE}});
    let mut retries = 0;
    let (code, answer) = loop {
        let response = instance.post("/api/model/download", &grant);
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
    let prepared = instance.until(
        "actual public acquisition prepared",
        || instance.get("/api/status").1,
        |s| {
            matches!(
                s["acquisition"]["state"].as_str(),
                Some("prepared" | "failed")
            )
        },
    );
    assert_eq!(prepared["acquisition"]["state"], "prepared", "{prepared}");
}

/// Row 3 (b): a headless MEMBER process runs its own agent - signed run-exchange client, its own
/// descriptor, the owner's bootstrap offer fetched from the owner's mailbox and its own admitted
/// hello worker - so two headless processes reach the owner's observation and both answer hello.
/// Nothing is signed or staged for the member here. On one Mac the observation then refuses by
/// name (`managed_observation_hosts_not_distinct`): the physical two-host check stays.
#[test]
#[ignore = "requires explicitly pinned admitted model-free Rust helper artifact"]
fn two_headless_processes_answer_hello_with_their_own_agents() {
    let _serial = one_at_a_time();
    build();
    let base = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("b19-pair-observe-{}", std::process::id()));
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
    let (owner, member, _proxy, edge) =
        pair_up_with_envs(&base, false, &env, &env, &seed_member_cache);
    let config = std::env::var("MYCELIUM_BOOTSTRAP_TEST_CONFIG")
        .expect("explicit artifact qualification config");
    let document: Value = serde_json::from_slice(&fs::read(config).unwrap()).unwrap();
    for instance in [&owner, &member] {
        api::preferences::private_state::store(
            &instance.root.join("bootstrap-program.json"),
            &document,
            api::preferences::private_state::private_json_limit().unwrap(),
        )
        .unwrap();
    }
    // A cache the member did not prepare itself is never trusted as prepared: it re-hashes it.
    let seeded = member.get("/api/status").1;
    assert_eq!(
        seeded["acquisition"]["error_code"], "prepared_source_revalidation_required",
        "{seeded}"
    );
    let (code, started) = owner.post("/api/demo/start", &json!({}));
    assert_eq!(code, 200, "{started}");
    let member_native = member.root.join("native-bootstrap/state.json");
    let read_native = || {
        fs::read(&member_native)
            .ok()
            .and_then(|b| serde_json::from_slice::<Value>(&b).ok())
            .unwrap_or(Value::Null)
    };
    // The member's own helper runs under its own pump (no test-side signing or staging).
    let running = owner.until(
        "the member's own hello worker",
        || json!({"native": read_native(), "status": owner.get("/api/status").1}),
        |s| s["native"]["phase"] == "running" || s["status"]["owner_run"]["state"] == "failed",
    );
    assert_eq!(
        running["native"]["phase"],
        "running",
        "member stderr: {}\n{running}",
        fs::read_to_string(member.root.with_extension("stderr")).unwrap_or_default()
    );
    let pid = rustix::process::Pid::from_raw(
        running["native"]["process"]["pid"]
            .as_u64()
            .unwrap()
            .try_into()
            .unwrap(),
    )
    .unwrap();
    // The observe step's own registered budgets: initial cleanup, bootstrap, hello collection.
    let observe = budget(BudgetId::InitialCleanup) * 2
        + budget(BudgetId::Bootstrap)
        + budget(BudgetId::Collect);
    let observed = owner.until_with(
        "observation outcome",
        observe,
        || json!({"run": owner.get("/api/status").1["owner_run"], "member_native": read_native(),
            "member_stderr": fs::read_to_string(member.root.with_extension("stderr")).unwrap_or_default()}),
        |s| s["run"]["state"] == "failed" || s["run"]["steps"][3]["state"] == "done",
    );
    let run = &observed["run"];
    println!("B19 two-process observe: {run}");
    assert_eq!(run["steps"][2]["name"], "descriptors", "{run}");
    assert_eq!(run["steps"][2]["state"], "done", "{run}");
    assert_eq!(run["steps"][2]["detail"], "2 of 2 Macs reported", "{run}");
    let membership: Value = api::preferences::private_state::load(
        &member.root.join("membership.state.json"),
        api::preferences::private_state::private_json_limit().unwrap(),
    )
    .unwrap()
    .map(Value::Object)
    .unwrap();
    let member_node = membership["node_id"].as_str().unwrap();
    let participants = run["participants"].as_array().unwrap();
    assert_eq!(participants.len(), 2, "{run}");
    assert!(
        participants.iter().any(|p| p["node_id"] == member_node),
        "{member_node} {run}"
    );
    // Both hosts answered hello (the distinctness check compares the SECOND host's identity with
    // the first): two processes on ONE Mac are one physical host, refused by name. Passing observe
    // needs two Macs (or an admitted host-identity seam); nothing here relabels it.
    assert_eq!(run["steps"][3]["name"], "observe", "{run}");
    assert_eq!(run["failed_step"], "observe", "{observed}");
    assert_eq!(
        run["error_code"], "managed_observation_hosts_not_distinct",
        "{observed}"
    );
    // The owner's teardown asked the member to clean up: the member's own agent saw the request,
    // its pump stopped the helper, removed the stage and sent the signed cleanup ACK.
    let closed = owner.until(
        "the member's worker closed on the owner's cleanup request",
        read_native,
        |s| s["phase"] == "closed" || s["phase"] == "cleanup_unproven",
    );
    assert_eq!(closed["phase"], "closed", "{closed}");
    assert_eq!(closed["cleanup_error"], Value::Null, "{closed}");
    assert!(!member.root.join("native-bootstrap/stage").exists());
    assert_eq!(
        rustix::process::test_kill_process_group(pid),
        Err(rustix::io::Errno::SRCH)
    );
    let db = rusqlite::Connection::open_with_flags(
        owner.root.join("seed-rs/managed/mailbox.sqlite"),
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    let acks: Vec<(String, Option<String>, Option<String>)> = db
        .prepare("SELECT node_id, ack, cleanup_ack FROM managed_stage_offers")
        .unwrap()
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)))
        .unwrap()
        .collect::<Result<_, _>>()
        .unwrap();
    let mine: Vec<_> = acks.iter().filter(|(n, _, _)| n == member_node).collect();
    assert_eq!(mine.len(), 1, "{acks:?}");
    let stage_ack: Value = serde_json::from_str(mine[0].1.as_deref().unwrap()).unwrap();
    let cleanup_ack: Value = serde_json::from_str(mine[0].2.as_deref().unwrap()).unwrap();
    assert_eq!(
        stage_ack["protocol"],
        "mycelium.controller_remote_stage_ack.v1"
    );
    assert_eq!(
        cleanup_ack["protocol"],
        "mycelium.controller_remote_cleanup_ack.v1"
    );
    assert_eq!(cleanup_ack["removed"], true);
    drop(db);
    assert_eq!(owner.post("/api/demo/stop", &json!({})).0, 200);
    drop(member);
    drop(owner);
    drop(edge);
    drop(repository);
    fs::remove_dir_all(base).unwrap();
}
