// SPDX-License-Identifier: AGPL-3.0-only
//! Owner-run `compile` step (SE-L7-CHAIN-COMPILE): the planner entry the chain calls, replayed against the
//! pinned v0.1.4 oracle. FX-L7-chain-compile-r11: 907 cases recorded by EXECUTING
//! `demo_chain.AppDemoChain._step_compile` -> `member_capability_records` -> `chain_compile.start_compile`
//! (incl. its compile thread) -> ceiling admission -> wall limits on a stub app
//! (`fixtures/chain-compile.json`; probe `round11-capture-chain-compile.py`, provenance beside it).
//! Per case the Rust port must reproduce, from the recorded inputs alone: the capability records, the
//! pre-thread refusal (code + human message), the builder request, the `_chain_compile_state`, the
//! admission outcome and every field `_step_compile` writes into the run context.
use planner::chain_compile::*;
use serde_json::{Value, json};
use std::collections::BTreeMap;

/// The oracle's `DEMO_CONTEXT_TOKENS` (model_resources.py:76 @ f4e366e9): the context every fixture
/// case was computed with. The product reads the model contract's own figure.
const ORACLE_CONTEXT_TOKENS: u64 = 4096;

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/chain-compile.json")).unwrap()
}

/// `execution_admission.py:11-47` `checked_ceiling` + `proposal_within_ceiling` (reference copy; the production
/// comparator is owner-run's `ceiling::proposal_within`, passed to `admit` as a closure).
fn within_ceiling(proposal: &Value, ceiling: &Value) -> bool {
    const MAX: [(&str, u64); 5] = [
        ("max_execution_download_bytes", 0),
        ("max_combined_rss_bytes", 8_589_934_592),
        ("max_additional_disk_bytes", 8_589_934_592),
        ("max_wall_seconds", 1800),
        ("run_count", 1),
    ];
    let Some(c) = ceiling.as_object() else {
        return false;
    };
    if c.len() != MAX.len() || MAX.iter().any(|(k, _)| !c.contains_key(*k)) {
        return false;
    }
    for (k, upper) in MAX {
        let Some(v) = c[k].as_u64() else {
            return false;
        };
        if v > upper || (!matches!(k, "max_execution_download_bytes" | "run_count") && v == 0) {
            return false;
        }
    }
    if c["run_count"] != 1 {
        return false;
    }
    let Some(p) = proposal.as_object() else {
        return false;
    };
    MAX.iter().all(|(k, _)| {
        let bound = c[*k].as_u64().unwrap();
        match p.get(*k).and_then(|v| v.as_u64()) {
            Some(v) => {
                v <= bound
                    && (*k == "max_execution_download_bytes" || v > 0)
                    && (!matches!(*k, "max_execution_download_bytes" | "run_count") || v == bound)
            }
            None => false,
        }
    })
}

fn strings(v: &Value) -> Vec<String> {
    v.as_array()
        .unwrap()
        .iter()
        .map(|x| x.as_str().unwrap().to_owned())
        .collect()
}

/// Replay one recorded case; `Err` is a description of the first difference.
fn replay(name: &str, rec: &Value) -> Result<(), String> {
    let i = &rec["inputs"];
    let nodes = strings(&i["nodes"]);
    let local = i["local_node"].as_str();
    let now = i["now"].as_f64().unwrap();
    let outcome = &rec["outcome"];
    // 1. capability records (demo_chain.member_capability_records + standing_consent.planning_caps)
    let grant = i["grant"].as_object().map(|g| GrantCaps {
        storage_bytes: g.get("storage_bytes").and_then(Value::as_u64),
        max_memory_bytes: g.get("max_memory_bytes").and_then(Value::as_u64),
        max_wall_seconds: g.get("max_wall_seconds").and_then(Value::as_u64),
    });
    let device = &i["local_device"];
    let local_record = local.map(|n| LocalRecord {
        node_id: n,
        memory_bytes: device["memory_bytes"].as_u64().unwrap(),
        available_storage_bytes: device.get("available_storage_bytes"),
        generation: &i["current_members"][n]["generation"],
        grant: grant.clone(),
        policy_storage_bytes: i["policy"]["storage_bytes"].as_u64(),
    });
    let records =
        member_capability_records(&nodes, local_record.as_ref(), &i["joined_reports"], now);
    if records != rec["records"] {
        return Err(format!(
            "{name}: records differ\n rust   {records}\n oracle {}",
            rec["records"]
        ));
    }
    // 2. pre-thread part of start_compile
    let facts: BTreeMap<String, Value> = i["facts"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let current: BTreeMap<String, Value> = i["current_members"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    let hosts = strings(&i["observation_hosts"]);
    let p = &i["prepared"];
    let model = ModelContract {
        model_id: p["model_id"].as_str().unwrap().into(),
        resolved_commit: p["resolved_commit"].as_str().unwrap().into(),
        source_representation: p["source_representation"].as_str().unwrap().into(),
        runtime_dtype: p["runtime_dtype"].as_str().unwrap().into(),
        runtime_quantization: p["runtime_quantization"].as_str().unwrap().into(),
        // The oracle's `DEMO_CONTEXT_TOKENS` (model_resources.py:76), which its fixtures were
        // computed with; the product reads it from the model contract.
        serving_context_tokens: ORACLE_CONTEXT_TOKENS,
    };
    let policy = ChainPolicy::pinned();
    let context_members: Vec<Value> = i["context_members"].as_array().unwrap().clone();
    let input = PrepareInput {
        owner_ready: i["owner_ready"].as_bool().unwrap(),
        observation_hosts: Some(&hosts),
        facts: &facts,
        already_started: i["already_started"].as_bool().unwrap(),
        resource_denied: i["resource_denied"].as_bool().unwrap(),
        model: &model,
        manifest_digest: p["manifest_digest"].as_str().unwrap(),
        manifest: &p["manifest"],
        source_bytes: p["source_bytes"].as_u64(),
        context_members: &context_members,
        current_members: &current,
        records: Some(&records),
        owner_node_id: local,
        slice_staging: false,
        slice_mode: i["slice_mode"].as_bool().unwrap(),
        keys: &i["keys"],
        deployment_id: i["deployment_id"].as_str().unwrap(),
        epoch: i["epoch"].as_u64().unwrap(),
        now,
        policy: &policy,
        stage_reservation: None,
    };
    let prepared = match prepare(&input) {
        Err(r) => {
            let want = outcome["refused"]
                .as_object()
                .ok_or_else(|| format!("{name}: oracle accepted, rust refused {r:?}"))?;
            if want["code"] != json!(r.code) || want["message"] != json!(r.message) {
                return Err(format!(
                    "{name}: refusal differs\n rust   {} / {:?}\n oracle {} / {}",
                    r.code, r.message, want["code"], want["message"]
                ));
            }
            if rec["retire_calls"] != 0 || !rec["build_calls"].as_array().unwrap().is_empty() {
                return Err(format!("{name}: oracle retired/built despite the refusal"));
            }
            return Ok(());
        }
        Ok(p) => p,
    };
    if rec["retire_calls"] != 1 || rec["build_calls"].as_array().unwrap().len() != 1 {
        return Err(format!(
            "{name}: rust prepared, oracle did not reach the builder: {outcome}"
        ));
    }
    // 3. the builder request
    let b = &rec["build_calls"][0];
    let r = &prepared.request;
    let ranges: Vec<Value> = r
        .managed_layer_ranges
        .iter()
        .map(|x| json!([x[0], x[1]]))
        .collect();
    let got = json!({
        "model_id": r.model_id, "resolved_commit": r.resolved_commit, "deployment_id": r.deployment_id,
        "route_label": r.route_label, "stage_sharded": r.stage_sharded, "emit_stage_shards": r.stage_sharded,
        "compilation_cache_key": r.compilation_cache_key, "managed_layer_ranges": ranges,
        "topology": r.node_ids, "layer_split": r.layer_split,
    });
    let want = json!({
        "model_id": b["model_id"], "resolved_commit": b["resolved_commit"], "deployment_id": b["deployment_id"],
        "route_label": b["route_label"], "stage_sharded": b["stage_sharded"], "emit_stage_shards": b["emit_stage_shards"],
        "compilation_cache_key": b["compilation_cache_key"], "managed_layer_ranges": b["managed_layer_ranges"],
        "topology": b["topology_nodes"].as_array().unwrap().iter().map(|n| n["node_id"].clone()).collect::<Vec<_>>(),
        "layer_split": b["layer_split_file"],
    });
    if got != want || b["managed_transport"] != true {
        return Err(format!(
            "{name}: build request differs\n rust   {got}\n oracle {want}"
        ));
    }
    // 4. the thread's tail -> _chain_compile_state
    let a = &i["adapter"];
    if a["block_build"] == true {
        return if outcome["refused"]["code"] == "chain_compile_timeout" {
            Ok(())
        } else {
            Err(format!(
                "{name}: blocked build must time out, oracle {outcome}"
            ))
        };
    }
    let state = if a["build_raises"] == true {
        failed_state()
    } else {
        let out = BuildOutput {
            operator_plan_digest: a["operator_plan_digest"].as_str().unwrap().into(),
            physical_host_ids: strings(&a["physical_host_ids"]),
            cache_hit: a["cache_hit"].as_bool().unwrap(),
            build_seconds: a["build_seconds"].as_f64().unwrap(),
            slice_plan_digest: i["slice_mode"]
                .as_bool()
                .unwrap()
                .then(|| a["slice_plan_digest"].as_str().unwrap().to_owned()),
            node_slice_plan_digests: i["slice_mode"].as_bool().unwrap().then(|| {
                a["node_slice_plan_digests"]
                    .as_object()
                    .unwrap()
                    .iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
                    .collect()
            }),
        };
        prepared.finish(&out)
    };
    if state != rec["compile_state"] {
        return Err(format!(
            "{name}: compile_state differs\n rust   {state}\n oracle {}",
            rec["compile_state"]
        ));
    }
    if a["build_raises"] == true {
        return if outcome["refused"]["code"] == "chain_compile_failed" {
            Ok(())
        } else {
            Err(format!(
                "{name}: failed build must refuse chain_compile_failed, oracle {outcome}"
            ))
        };
    }
    // 5. admission (ceiling, wall limits) and the run-context fields
    let ceiling = if i["ceiling"].is_null() {
        None
    } else {
        Some(&i["ceiling"])
    };
    let adm = admit(&state, &records, &nodes, local, ceiling, &within_ceiling);
    match (adm, outcome.get("ok")) {
        (Err(e), None) => {
            let want = &outcome["refused"];
            if want["code"] != json!(e.code) || want["message"] != json!(e.message) {
                return Err(format!(
                    "{name}: admission refusal {e:?} vs oracle {outcome}"
                ));
            }
            Ok(())
        }
        (Err(e), Some(_)) => Err(format!("{name}: oracle admitted, rust refused {e:?}")),
        (Ok(_), None) => Err(format!("{name}: rust admitted, oracle {outcome}")),
        (Ok(a), Some(_)) => {
            let c = &rec["ctx_state"];
            let mut progress = vec![compile_progress(nodes.len())];
            progress.extend(a.progress.clone());
            let got = json!({
                "proposal": a.proposal, "worker_wall_limits": a.worker_wall_limits,
                "wall_cap_fallback_nodes": a.wall_cap_fallback_nodes, "compile_state": a.compile_state,
                "layers": a.layers, "cache_hit": a.compilation_cache_hit, "build_seconds": a.build_seconds,
                "progress": progress,
            });
            let want = json!({
                "proposal": c["proposal"], "worker_wall_limits": c["worker_wall_limits"],
                "wall_cap_fallback_nodes": c["wall_cap_fallback_nodes"], "compile_state": c["compile_state"],
                "layers": c["layers"], "cache_hit": rec["ctx_result"]["compilation_cache_hit"],
                "build_seconds": rec["ctx_result"]["build_seconds"], "progress": rec["progress"],
            });
            if got != want {
                return Err(format!(
                    "{name}: admission differs\n rust   {got}\n oracle {want}"
                ));
            }
            Ok(())
        }
    }
}

fn run_group(label: &str, pick: impl Fn(&str, &Value) -> bool, at_least: usize) -> usize {
    let doc = fixture();
    let mut n = 0;
    let mut bad = Vec::new();
    for (name, rec) in doc["cases"].as_object().unwrap() {
        if !pick(name, rec) {
            continue;
        }
        n += 1;
        if let Err(e) = replay(name, rec) {
            bad.push(e);
        }
    }
    assert!(n >= at_least, "{label}: only {n} cases (need {at_least})");
    assert!(
        bad.is_empty(),
        "{label}: {} of {n} cases differ from the oracle; first 3:\n{}",
        bad.len(),
        bad.iter().take(3).cloned().collect::<Vec<_>>().join("\n")
    );
    n
}

fn code(rec: &Value) -> String {
    match rec["outcome"].get("refused") {
        Some(r) => r["code"].as_str().unwrap().to_owned(),
        None => "ok".into(),
    }
}

/// Proves: B-PLAN-09 (capability records: owner local observation + current verified reports; caps and free
/// disk copied only when typed int; local caps = grant intersected with the storage policy).
#[test]
fn capability_records_equal_the_oracle_for_every_case() {
    // replay() checks the records first; this group is every case that reaches or stops at the records.
    run_group("all cases", |_, _| true, 900);
}

/// Proves: B-CONSENT-04, B-PLAN-03, B-PLAN-05 — the owner-reported escape ("member storage cap omitted during
/// splitting"). A Mac with 64 GiB of RAM but a 100 MB (or the saved 2 GB) storage consent is refused BY NAME
/// before anything is built; where another feasible split exists the planner gives that Mac a smaller share.
#[test]
fn storage_cap_escape_is_refused_by_name_or_planned_around() {
    let n = run_group("escape", |n, _| n.starts_with("ESCAPE-"), 8);
    let doc = fixture();
    // oracle outcomes (executed v0.1.4): these MUST fail with the storage-cap code on node-b.
    for case in [
        "ESCAPE-storage-poor-ram-rich-Qwen3-8B-slice0",
        "ESCAPE-storage-poor-ram-rich-Qwen3-8B-slice1",
        "ESCAPE-storage-poor-ram-rich-Qwen3-14B-slice0",
        "ESCAPE-storage-poor-ram-rich-Qwen3-14B-slice1",
        "ESCAPE-poor-member-other-feasible",
    ] {
        let rec = &doc["cases"][case];
        assert_eq!(code(rec), "member_storage_cap_below_share", "{case}");
        let m = rec["outcome"]["refused"]["message"].as_str().unwrap();
        assert!(
            m.starts_with(
                "The Mac node-b agreed to at most 0.1 GB of storage, but its share of Qwen3"
            ),
            "{m}"
        );
    }
    // a saved 2 GB consent without slice fetch cannot hold the whole snapshot: refused by name
    let rec = &doc["cases"]["ESCAPE-saved-2gb-member-nonslice"];
    assert_eq!(code(rec), "member_storage_cap_below_share");
    assert!(
        rec["outcome"]["refused"]["message"]
            .as_str()
            .unwrap()
            .starts_with("The Mac node-b agreed to at most 2.0 GB of storage")
    );
    // ... with slice fetch (and for the owner's own storage policy) the planner plans around the cap: compiled,
    // and the capped Mac's disk limit is within what it consented to.
    for (case, capped) in [
        ("ESCAPE-saved-2gb-member", "node-b"),
        ("ESCAPE-owner-policy-storage-2gb", "node-a"),
    ] {
        let rec = &doc["cases"][case];
        assert_eq!(code(rec), "ok", "{case}");
        let st = &rec["compile_state"]["resource_plan"]["members"];
        let row = st
            .as_array()
            .unwrap()
            .iter()
            .find(|r| r["node_id"] == capped)
            .unwrap();
        assert!(
            row["max_disk_bytes"].as_u64().unwrap() <= 2_000_000_000,
            "{case}: {row}"
        );
    }
    assert!(n >= 8);
}

/// Proves: B-CONSENT-05, B-CONSENT-06, B-PLAN-05 — memory caps, free disk, RAM ceiling and staging overhead.
#[test]
fn memory_cap_free_disk_and_ram_refusals_name_the_mac_and_the_numbers() {
    run_group(
        "resources",
        |n, _| {
            n.starts_with("memory-cap")
                || n.starts_with("free-disk")
                || n.starts_with("owner-free-disk")
                || n.starts_with("ram-")
                || n.starts_with("7b-")
        },
        14,
    );
    let doc = fixture();
    let mut seen = std::collections::BTreeSet::new();
    for rec in doc["cases"].as_object().unwrap().values() {
        seen.insert(code(rec));
    }
    for want in [
        "member_memory_cap_below_share",
        "member_storage_cap_below_share",
        "member_memory_insufficient_for_share",
        "member_disk_insufficient_for_share",
        "member_cannot_hold_share",
    ] {
        assert!(seen.contains(want), "corpus lost {want}");
    }
}

/// Proves: B-PLAN-09 (stale / foreign / missing reports are never used), B-PLAN-05 (host counts, duplicate hosts),
/// plus the binding refusals of `start_compile` (member changed, model changed, consent, already started).
#[test]
fn preflight_ladder_and_stale_reports_refuse_with_the_oracle_codes() {
    run_group(
        "ladder",
        |n, _| {
            n.starts_with("report-")
                || n.starts_with("lease-")
                || n.starts_with("member-changed")
                || n.starts_with("model-changed")
                || n.starts_with("role-")
                || n.starts_with("not-running")
                || n.starts_with("already-")
                || n.starts_with("resource-check")
                || n.starts_with("duplicate-hosts")
                || n.starts_with("one-host")
                || n.starts_with("hosts-")
                || n.starts_with("no-local")
                || n.starts_with("slice-source")
                || n.contains("slice-not-permitted")
        },
        42,
    );
}

/// Proves: B-CONSENT-07, B-CONSENT-08, B-CONSENT-09 (F15/F15b) — each worker's wall = min(owner proposal,
/// that Mac's consented time, 3,600); the owner's own Mac with no caps runs under the owner's proposal, a joined
/// Mac with unknown time gets 60 s and is listed; a known limit outside 60..3,600 is refused.
#[test]
fn wall_limits_follow_each_members_consent() {
    let n = run_group("wall", |n, _| n.starts_with("wall-"), 22);
    assert!(n >= 22);
    let doc = fixture();
    let f15 = &doc["cases"]["wall-F15-member600-owner-local"]["ctx_state"];
    assert_eq!(
        f15["worker_wall_limits"],
        json!({"node-a": 1800, "node-b": 600, "node-c": 1800}),
        "oracle: member 600 s while the owner's Mac runs 1800 s"
    );
    let own = &doc["cases"]["wall-owner-no-caps-gets-proposal"]["ctx_state"];
    assert_eq!(
        own["worker_wall_limits"]["node-a"], 1800,
        "owner's own Mac is not given the 60 s fallback"
    );
    assert_eq!(own["wall_cap_fallback_nodes"], json!(["node-c"]));
}

/// Proves: B-CONSENT-16 — the compiled proposal is checked against the optional execution ceiling, at, one below
/// and one above the edge, before anything is published.
#[test]
fn execution_ceiling_admission_matches_the_oracle_at_the_edges() {
    let n = run_group("ceiling", |n, _| n.starts_with("ceiling-"), 39);
    let doc = fixture();
    let (mut outside, mut within) = (0, 0);
    for (name, rec) in doc["cases"].as_object().unwrap() {
        if !name.starts_with("ceiling-") {
            continue;
        }
        match code(rec).as_str() {
            "execution_proposal_outside_ceiling" => outside += 1,
            "ok" => within += 1,
            other => panic!("{name}: unexpected {other}"),
        }
    }
    assert!(
        outside >= 15 && within >= 15 && outside + within == n,
        "outside={outside} within={within} of {n}"
    );
}

/// Proves: B-CONSENT-04, B-CONSENT-05, B-CONSENT-06, B-PLAN-03, B-PLAN-05 at the EDGES — for random fleets the
/// oracle was bisected on one member's storage cap, memory cap, free disk, RAM and the owner's storage policy; every
/// outcome-class transition is recorded at flip-1, flip, flip+1, so an off-by-one in any comparison (cap, RAM
/// ceiling, free disk, staging footprint) changes one of them.
#[test]
fn chain_outcomes_flip_exactly_where_the_oracle_flips() {
    for dim in ["storage", "memcap", "freedisk", "ownerpolicy", "ram"] {
        let n = run_group(dim, |n, _| n.starts_with(&format!("bisect-{dim}-")), 50);
        assert!(n >= 50);
    }
    // every dimension must really cross an outcome boundary inside the sweep
    let doc = fixture();
    for dim in ["storage", "memcap", "freedisk", "ownerpolicy", "ram"] {
        let mut classes = std::collections::BTreeSet::new();
        for (name, rec) in doc["cases"].as_object().unwrap() {
            if name.starts_with(&format!("bisect-{dim}-")) {
                classes.insert(code(rec));
            }
        }
        assert!(classes.len() >= 2, "{dim}: {classes:?}");
    }
}

/// Proves: B-PLAN-10, B-PLAN-08, B-CONSENT-06 — two- and three-member (and larger) fleets compile to the same
/// `_chain_compile_state` the oracle returns: layer split, per-member resource plan (guard/disk limits with the
/// 4 MiB + 64 MiB staging overhead), proposal, artifact reuse key, cache hit and build seconds.
#[test]
fn compile_state_for_two_three_and_more_member_fleets_equals_the_oracle() {
    let n = run_group(
        "fleets",
        |n, _| n.starts_with("ok-") || n.starts_with("cache-") || n.starts_with("build-"),
        17,
    );
    assert!(n >= 17);
    let doc = fixture();
    // random fleets: both compiled and refused outcomes must be well represented
    let (mut ok, mut refused) = (0, 0);
    for (name, rec) in doc["cases"].as_object().unwrap() {
        if !name.starts_with("random-") {
            continue;
        }
        if code(rec) == "ok" {
            ok += 1
        } else {
            refused += 1
        }
    }
    assert!(
        ok >= 30 && refused >= 200,
        "random sweep ok={ok} refused={refused}"
    );
    run_group("random", |n, _| n.starts_with("random-"), 250);
    let feasible = run_group(
        "feasible",
        |n, r| n.starts_with("feasible-") && code(r) == "ok",
        100,
    );
    assert!(feasible >= 100);
}

/// `prepare` over a recorded case's inputs with the model contract's serving context set to
/// `context`: the compile's memory plan and its builder request, without the oracle comparison.
fn prepare_case(rec: &Value, context: u64) -> Result<PreparedCompile, Refusal> {
    prepare_case_with(rec, context, None)
}
/// [`prepare_case`] with the host's stage-worker reservation (lane A, B7 / CAP-1).
fn prepare_case_with(
    rec: &Value,
    context: u64,
    stage_reservation: Option<StageReservation>,
) -> Result<PreparedCompile, Refusal> {
    let i = &rec["inputs"];
    let nodes = strings(&i["nodes"]);
    let local = i["local_node"].as_str();
    let now = i["now"].as_f64().unwrap();
    let grant = i["grant"].as_object().map(|g| GrantCaps {
        storage_bytes: g.get("storage_bytes").and_then(Value::as_u64),
        max_memory_bytes: g.get("max_memory_bytes").and_then(Value::as_u64),
        max_wall_seconds: g.get("max_wall_seconds").and_then(Value::as_u64),
    });
    let device = &i["local_device"];
    let local_record = local.map(|n| LocalRecord {
        node_id: n,
        memory_bytes: device["memory_bytes"].as_u64().unwrap(),
        available_storage_bytes: device.get("available_storage_bytes"),
        generation: &i["current_members"][n]["generation"],
        grant: grant.clone(),
        policy_storage_bytes: i["policy"]["storage_bytes"].as_u64(),
    });
    let records =
        member_capability_records(&nodes, local_record.as_ref(), &i["joined_reports"], now);
    let object = |v: &Value| -> BTreeMap<String, Value> {
        v.as_object()
            .unwrap()
            .iter()
            .map(|(k, v)| (k.clone(), v.clone()))
            .collect()
    };
    let (facts, current) = (object(&i["facts"]), object(&i["current_members"]));
    let hosts = strings(&i["observation_hosts"]);
    let p = &i["prepared"];
    let model = ModelContract {
        model_id: p["model_id"].as_str().unwrap().into(),
        resolved_commit: p["resolved_commit"].as_str().unwrap().into(),
        source_representation: p["source_representation"].as_str().unwrap().into(),
        runtime_dtype: p["runtime_dtype"].as_str().unwrap().into(),
        runtime_quantization: p["runtime_quantization"].as_str().unwrap().into(),
        serving_context_tokens: context,
    };
    let policy = ChainPolicy::pinned();
    let context_members: Vec<Value> = i["context_members"].as_array().unwrap().clone();
    prepare(&PrepareInput {
        owner_ready: i["owner_ready"].as_bool().unwrap(),
        observation_hosts: Some(&hosts),
        facts: &facts,
        already_started: i["already_started"].as_bool().unwrap(),
        resource_denied: i["resource_denied"].as_bool().unwrap(),
        model: &model,
        manifest_digest: p["manifest_digest"].as_str().unwrap(),
        manifest: &p["manifest"],
        source_bytes: p["source_bytes"].as_u64(),
        context_members: &context_members,
        current_members: &current,
        records: Some(&records),
        owner_node_id: local,
        slice_staging: false,
        slice_mode: i["slice_mode"].as_bool().unwrap(),
        keys: &i["keys"],
        deployment_id: i["deployment_id"].as_str().unwrap(),
        epoch: i["epoch"].as_u64().unwrap(),
        now,
        policy: &policy,
        stage_reservation,
    })
}

/// Lane CAP (owner ruling 15 Nov): the compile charges every stage's KV memory for the MODEL
/// CONTRACT's serving context, and its builder request carries that same figure to the stages, so a
/// stage is prepared for exactly what the compile planned. At the oracle's 4,096 the compile state
/// is the oracle's (every case above); at half of it, every planned stage's `kv_bytes` halves and
/// the request names the half, which no figure pinned elsewhere in the planner could produce.
#[test]
fn the_compile_plans_and_requests_the_model_contracts_serving_context() {
    let doc = fixture();
    let mut checked = 0;
    for (name, rec) in doc["cases"].as_object().unwrap() {
        let a = &rec["inputs"]["adapter"];
        if rec["build_calls"].as_array().unwrap().len() != 1
            || a["block_build"] == true
            || a["build_raises"] == true
            || !rec["compile_state"]["resource_plan"]["members"].is_array()
        {
            continue;
        }
        let planned = rec["compile_state"]["resource_plan"]["members"]
            .as_array()
            .unwrap();
        let Ok(at_oracle) = prepare_case(rec, ORACLE_CONTEXT_TOKENS) else {
            continue;
        };
        assert_eq!(
            at_oracle.request.serving_context_tokens, ORACLE_CONTEXT_TOKENS,
            "{name}"
        );
        let half = prepare_case(rec, ORACLE_CONTEXT_TOKENS / 2)
            .unwrap_or_else(|r| panic!("{name}: half the context refused {r:?}"));
        assert_eq!(
            half.request.serving_context_tokens,
            ORACLE_CONTEXT_TOKENS / 2,
            "{name}: the builder request carries the contract's context"
        );
        let out = BuildOutput {
            operator_plan_digest: a["operator_plan_digest"].as_str().unwrap().into(),
            physical_host_ids: strings(&a["physical_host_ids"]),
            cache_hit: a["cache_hit"].as_bool().unwrap(),
            build_seconds: a["build_seconds"].as_f64().unwrap(),
            slice_plan_digest: Some(String::new()),
            node_slice_plan_digests: Some(BTreeMap::new()),
        };
        let state = half.finish(&out);
        let rows = state["resource_plan"]["members"].as_array().unwrap();
        // KV bytes per layer and token of this case's model, from the oracle's own plan at 4,096.
        let layers = |r: &Value| r["end"].as_u64().unwrap() - r["start"].as_u64().unwrap();
        let per_layer_token =
            planned[0]["kv_bytes"].as_u64().unwrap() / layers(&planned[0]) / ORACLE_CONTEXT_TOKENS;
        for oracle in planned {
            assert_eq!(
                oracle["kv_bytes"].as_u64().unwrap(),
                layers(oracle) * per_layer_token * ORACLE_CONTEXT_TOKENS,
                "{name}"
            );
        }
        // The split may move with the smaller memory need; every planned stage's KV is charged
        // for the contract's context over its own layers.
        for row in rows {
            assert_eq!(
                row["kv_bytes"].as_u64().unwrap(),
                layers(row) * per_layer_token * (ORACLE_CONTEXT_TOKENS / 2),
                "{name}: kv_bytes follows the contract's context: {row}"
            );
        }
        checked += 1;
    }
    assert!(checked >= 20, "only {checked} cases reached the builder");
}

/// The reservation [`reserving`] reports for every stage (a test-owned figure).
static RESERVED: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
fn reserving(_: &str, _: [u64; 2], _: u64, _: u64) -> Option<u64> {
    Some(RESERVED.load(std::sync::atomic::Ordering::SeqCst))
}
fn no_native_figure(_: &str, _: [u64; 2], _: u64, _: u64) -> Option<u64> {
    None
}

/// Lane A, release blocker B7 (CAP-1, REVIEW-laneCAP-astra.md): the compile plans each member's
/// memory share with the stage worker's OWN reservation when the host supplies it. A share is
/// never below what the worker reserves (so the member can enforce it), never lowered by it, the
/// proposal's aggregate follows, and a reservation no Mac can hold is refused by name before the
/// builder. Without a native figure the plan is the oracle's modelled one, byte for byte.
#[test]
fn the_compile_plans_each_share_with_the_stage_workers_own_reservation() {
    let doc = fixture();
    let out = |a: &Value| BuildOutput {
        operator_plan_digest: a["operator_plan_digest"].as_str().unwrap().into(),
        physical_host_ids: strings(&a["physical_host_ids"]),
        cache_hit: a["cache_hit"].as_bool().unwrap(),
        build_seconds: a["build_seconds"].as_f64().unwrap(),
        slice_plan_digest: Some(String::new()),
        node_slice_plan_digests: Some(BTreeMap::new()),
    };
    let (mut raised, mut refused) = (0, 0);
    for (name, rec) in doc["cases"].as_object().unwrap() {
        let a = &rec["inputs"]["adapter"];
        if rec["build_calls"].as_array().unwrap().len() != 1
            || a["block_build"] == true
            || a["build_raises"] == true
        {
            continue;
        }
        let Ok(modelled) = prepare_case(rec, 2356) else {
            continue;
        };
        let modelled = modelled.finish(&out(a));
        let rows = |s: &Value| s["resource_plan"]["members"].as_array().unwrap().clone();
        let highest = rows(&modelled)
            .iter()
            .map(|r| r["max_rss_bytes"].as_u64().unwrap())
            .max()
            .unwrap();
        // No native figure: exactly the modelled plan.
        let unchanged = prepare_case_with(rec, 2356, Some(no_native_figure))
            .unwrap_or_else(|r| panic!("{name}: {r:?}"))
            .finish(&out(a));
        assert_eq!(unchanged, modelled, "{name}");
        // A reservation 1 MB above every modelled share: every share is that reservation.
        RESERVED.store(highest + 1_000_000, std::sync::atomic::Ordering::SeqCst);
        match prepare_case_with(rec, 2356, Some(reserving)) {
            Ok(planned) => {
                let planned = planned.finish(&out(a));
                for row in rows(&planned) {
                    assert_eq!(row["max_rss_bytes"], json!(highest + 1_000_000), "{name}: {row}");
                }
                assert_eq!(
                    planned["execution_proposal"]["max_combined_rss_bytes"],
                    json!(2 * (highest + 1_000_000)),
                    "{name}"
                );
                raised += 1;
            }
            Err(r) => assert!(
                matches!(
                    r.code.as_str(),
                    "member_memory_cap_below_share" | "member_memory_insufficient_for_share"
                ),
                "{name}: {r:?}"
            ),
        }
        // A reservation below every share never lowers one.
        RESERVED.store(1, std::sync::atomic::Ordering::SeqCst);
        let lower = prepare_case_with(rec, 2356, Some(reserving))
            .unwrap_or_else(|r| panic!("{name}: {r:?}"))
            .finish(&out(a));
        assert_eq!(lower, modelled, "{name}");
        // A reservation no Mac can hold: refused by name before the builder.
        RESERVED.store(1 << 50, std::sync::atomic::Ordering::SeqCst);
        match prepare_case_with(rec, 2356, Some(reserving)) {
            Err(r) => {
                assert!(
                    matches!(
                        r.code.as_str(),
                        "member_memory_cap_below_share" | "member_memory_insufficient_for_share"
                    ),
                    "{name}: {r:?}"
                );
                assert!(r.message.is_some_and(|m| m.contains("Mac")), "{name}");
                refused += 1;
            }
            Ok(_) => panic!("{name}: a 1 PiB stage reservation was planned"),
        }
    }
    assert!(raised >= 10 && refused >= 10, "raised {raised}, refused {refused}");
}
