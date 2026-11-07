// SPDX-License-Identifier: AGPL-3.0-only
//! Executable acceptance harness; all output comes from the real HTTP stream.
use super::*;
use crate::loopback;
use crate::service::NodeService;
use api::{
    actions::{Action, Backend},
    requests::Gateway,
};
use std::net::{TcpListener, TcpStream};
use std::{
    io::{BufRead, BufReader, Read, Write},
    thread::{self, JoinHandle},
};
fn connection(
    listener: &TcpListener,
    service: NodeService,
    bearer: &str,
    method: &str,
    path: &str,
    session: Option<&str>,
    body: &[u8],
) -> Result<(TcpStream, JoinHandle<Result<()>>)> {
    let server = listener.try_clone()?;
    let task = thread::spawn(move || {
        let (stream, _) = server.accept()?;
        let gateway = Gateway::new(service);
        api::http::serve_response_connection(
            stream,
            service::duration(B::MemberChosenWall),
            api::requests::MAX_REQUEST_BODY,
            |r| gateway.preflight(r),
            |r| gateway.handle(r),
        )
        .map_err(|e| format!("HTTP: {e:?}"))?;
        Ok(())
    });
    let mut client = TcpStream::connect(listener.local_addr()?)?;
    loopback::configure(&client)?;
    client.set_read_timeout(Some(service::duration(B::MemberChosenWall)))?;
    write!(
        client,
        "{method} {path} HTTP/1.1\r\nHost: {}\r\nAuthorization: Bearer {bearer}\r\nContent-Type: application/json\r\nContent-Length: {}\r\n",
        listener.local_addr()?,
        body.len()
    )?;
    if let Some(session) = session {
        write!(client, "X-Mycelium-Session: {session}\r\n")?;
    }
    client.write_all(b"\r\n")?;
    client.write_all(body)?;
    client.flush()?;
    Ok((client, task))
}
fn join(task: JoinHandle<Result<()>>) -> Result<()> {
    task.join().map_err(|_| "HTTP worker panicked")?
}
fn buffered(
    listener: &TcpListener,
    service: NodeService,
    bearer: &str,
    method: &str,
    path: &str,
    session: Option<&str>,
    body: &[u8],
) -> Result<Value> {
    eprintln!("HTTP begin {method} {path}");
    let (mut client, task) = connection(listener, service, bearer, method, path, session, body)?;
    let mut bytes = String::new();
    client.read_to_string(&mut bytes)?;
    eprintln!("HTTP end {method}");
    drop(client);
    join(task)?;
    let (headers, body) = bytes.split_once("\r\n\r\n").ok_or("http_framing")?;
    if !headers.starts_with("HTTP/1.1 2") {
        return Err(format!("HTTP refused: {body}").into());
    }
    Ok(serde_json::from_str(body)?)
}
fn rollover(service: &mut NodeService) -> Result<()> {
    let deadline = Instant::now() + service::duration(B::WorkerCleanupReserve);
    loop {
        match service.execute(Action::DemoRebuild, &serde_json::Map::new()) {
            Ok(_) => return Ok(()),
            Err(e) if e.code == "cleanup_unproven" && Instant::now() < deadline => {
                thread::sleep(service::duration(B::RegistryPoll))
            }
            Err(e) => return Err(e.code.into()),
        }
    }
}
/// What the qualification drives.
#[derive(Clone, Copy, PartialEq, Eq)]
enum Step {
    Normal,
    Cancel,
    /// RU-020: this run's native worker is killed from outside; the run must end `failed` from a
    /// real signed quorum and the NEXT Start must succeed.
    Failed,
}
pub fn run(checkpoint: &Path, tokenizer: &Path, evidence: &Path) -> Result<()> {
    run_plan(
        checkpoint,
        tokenizer,
        evidence,
        &[Step::Normal, Step::Cancel, Step::Normal],
    )
}
/// A run whose worker is killed from outside, then a normal run on the next Start.
pub fn run_failed_then_normal(checkpoint: &Path, tokenizer: &Path, evidence: &Path) -> Result<()> {
    run_plan(
        checkpoint,
        tokenizer,
        evidence,
        &[Step::Failed, Step::Normal],
    )
}
fn run_plan(checkpoint: &Path, tokenizer: &Path, evidence: &Path, plan: &[Step]) -> Result<()> {
    pressure()?;
    fs::create_dir(evidence)?;
    let fixture: Value = serde_json::from_str(include_str!("../../data/qwen2-oracle.json"))?;
    let ids: Vec<u32> = serde_json::from_value(fixture["workload"]["prompt_ids"].clone())?;
    let expected: Vec<u32> = serde_json::from_value(fixture["tokens"].clone())?;
    let tokenizer_bytes = fs::read(tokenizer)?;
    if canonical::sha256_prefixed(&tokenizer_bytes)
        != format!("sha256:{}", text(&fixture["identity"], "tokenizer_sha256")?)
    {
        return Err("tokenizer_pin_mismatch".into());
    }
    drop(tokenizer_bytes);
    let codec = tokenizers::Tokenizer::from_file(tokenizer)?;
    let prompt = codec.decode(&ids, false)?;
    if codec.encode(prompt.clone(), false)?.get_ids() != ids {
        return Err("tokenizer_roundtrip_mismatch".into());
    }
    let qualification = development_qualification()?;
    let bearer = service::random_id().map_err(|e| e.code)?;
    let private = std::env::temp_dir().canonicalize()?.join(format!(
        "slice-private-{}",
        service::random_id().map_err(|e| e.code)?
    ));
    let driver = configured_driver_mode(checkpoint, tokenizer, evidence, &private, true)?;
    let owner_key = crate::enrollment::identity(&private.join("owner"))?.public_key_record();
    let member_key = crate::enrollment::identity(&private.join("member"))?.public_key_record();
    let local_membership = crate::enrollment::LocalMembership::load(&private.join("member"))?;
    let consent = serde_json::to_value(&local_membership.consent)?;
    let mut service = NodeService::new(driver, bearer.clone(), qualification.clone());
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let mut summaries = Vec::new();
    for step in plan.iter().copied() {
        let cancel = step == Step::Cancel;
        let generation = service.generation().map_err(|e| e.code)?;
        let document = json!({"protocol":"mycelium.request_gateway.v1","prompt":prompt,"max_new_tokens":expected.len(),"qualification":qualification});
        let accepted = buffered(
            &listener,
            service.clone(),
            &bearer,
            "POST",
            "/v1/inference",
            None,
            &serde_json::to_vec(&document)?,
        )?;
        eprintln!("opening SSE generation {generation}");
        let session = text(&accepted, "session_token")?;
        let (client, task) = connection(
            &listener,
            service.clone(),
            &bearer,
            "GET",
            text(&accepted, "stream_path")?,
            Some(session),
            &[],
        )?;
        let mut reader = BufReader::new(client);
        let mut line = String::new();
        let mut headers = String::new();
        loop {
            line.clear();
            if reader.read_line(&mut line)? == 0 {
                return Err("HTTP headers EOF".into());
            }
            if line == "\r\n" {
                break;
            }
            headers.push_str(&line);
        }
        eprintln!("SSE headers received");
        if !headers.starts_with("HTTP/1.1 200") {
            return Err(format!("stream failed: {headers}").into());
        }
        let mut events = Vec::new();
        let mut stop = false;
        let mut stop_ms = None;
        loop {
            line.clear();
            if reader.read_line(&mut line)? == 0 {
                break;
            }
            if let Some(data) = line.strip_prefix("data: ") {
                let event: Value = serde_json::from_str(data.trim())?;
                protocol::request::event(&event)?;
                if event["publisher_generation"] != generation {
                    return Err("cross_generation_event".into());
                }
                let is_token = event["type"] == "token";
                events.push(event);
                if cancel && is_token && !stop {
                    let stopping = Instant::now();
                    let result = buffered(
                        &listener,
                        service.clone(),
                        &bearer,
                        "DELETE",
                        text(&accepted, "cancel_path")?,
                        Some(session),
                        &[],
                    )?;
                    if result["status"] != "cancelling" {
                        return Err("cancel_not_admitted".into());
                    }
                    stop = true;
                    // Exercise L10 action backend separately; its return requires
                    // L9 cleanup terminal, not merely DELETE's cancellation intent.
                    service
                        .prepare(Action::ServingStop, &serde_json::Map::new())
                        .map_err(|e| e.code)?;
                    service
                        .execute(Action::ServingStop, &serde_json::Map::new())
                        .map_err(|e| e.code)?;
                    stop_ms = Some(stopping.elapsed().as_millis());
                }
            }
        }
        drop(reader);
        join(task)?;
        let terminal = match step {
            Step::Cancel => "cancelled",
            Step::Failed => "failed",
            Step::Normal => "completed",
        };
        if events.last().is_none_or(|e| e["type"] != terminal) {
            return Err(format!("terminal missing: {events:?}").into());
        }
        if step == Step::Failed {
            let settled: Value = serde_json::from_slice(&fs::read(
                evidence.join(format!("generation-{generation}/failed-settle.json")),
            )?)?;
            let last = events.last().ok_or("failed_event_missing")?;
            if last["code"] != "worker_native_exited"
                || settled["terminal"] != "failed"
                || settled["error_code"] != "worker_native_exited"
                || settled["signed_cleanup_quorum"] != true
                || settled["receipts"] != json!(["b-member", "a-owner"])
                || settled["agents"]["survived_to_close"] != true
                || settled["died"]["pid"].as_u64().is_none()
                || evidence
                    .join(format!("generation-{generation}/result.json"))
                    .exists()
            {
                return Err(format!("failed_settle_evidence_invalid: {settled}").into());
            }
            // Stop on a failed run does not raise (v0.1.4 `stop_on_failed_run_raises:false`).
            service
                .prepare(Action::ServingStop, &serde_json::Map::new())
                .map_err(|e| e.code)?;
            service
                .execute(Action::ServingStop, &serde_json::Map::new())
                .map_err(|e| e.code)?;
            fs::write(
                evidence.join(format!("events-{generation}.json")),
                serde_json::to_vec_pretty(&events)?,
            )?;
            summaries.push(json!({"generation":generation,"terminal":"failed","error_code":settled["error_code"],"died_pid":settled["died"]["pid"],"died_signal":settled["died"]["signal"],"signed_cleanup_quorum":true}));
            // The NEXT Start is allowed (v0.1.4 `next_start_allowed:true`).
            rollover(&mut service)?;
            pressure()?;
            continue;
        }
        let native: Value = serde_json::from_slice(&fs::read(
            evidence.join(format!("generation-{generation}/result.json")),
        )?)?;
        if !cancel && native["token_ids"] != fixture["tokens"] {
            return Err("independent_oracle_token_mismatch".into());
        }
        let serving: Value = serde_json::from_slice(&fs::read(
            evidence.join(format!("generation-{generation}/core-serving.json")),
        )?)?;
        let cleaned: Value = serde_json::from_slice(&fs::read(
            evidence.join(format!("generation-{generation}/core-cleaned.json")),
        )?)?;
        if serving["phase"] != "Serving"
            || serving["workers"].as_object().is_none_or(|w| w.len() != 2)
            || cleaned["phase"] != "Idle"
            || cleaned["workers"].as_object().is_none_or(|w| !w.is_empty())
            || cleaned["members"]["b-member"]["consent"].is_null()
        {
            return Err("durable_core_lifecycle_failed".into());
        }
        let stop_proof: Value = serde_json::from_slice(&fs::read(evidence.join(format!(
            "generation-{generation}/{}",
            if native["lifecycle_boundary"] == "wall_timed_rollover" {
                "rollover-cleanup.json"
            } else {
                "stop-proof.json"
            }
        )))?)?;
        let acceptance: Value = serde_json::from_slice(&fs::read(
            evidence.join(format!("generation-{generation}/owner-acceptance.json")),
        )?)?;
        protocol::identity::verify(&acceptance["message"], &acceptance["signature"], &owner_key)?;
        if stop_proof["cleanup_proven"] != true
            || stop_proof["epoch"] != cleaned["epoch"]
            || stop_proof["run"] != cleaned["run"]
            || acceptance["verification_key"] != owner_key
            || acceptance["message"]["membership_generation"] != 1
            || acceptance["message"]["accepted_incarnation"]
                != serving["members"]["b-member"]["incarnation"]
            || serving["members"]["b-member"]["key_digest"] != member_key["verification_key_digest"]
            || serving["members"]["b-member"]["consent"] != consent
            || native["iroh_activation_transport"] != true
            || native["iroh_control_transport"] != "signed-node-control"
            || native["loopback_tcp_control"] != false
            || native["child_tcp_sockets"] != json!({"member":0,"owner_leaf":0})
            || native["artifact"]["member"]["staged_via"] != "signed-artifact-chunk"
            || native["artifact"]["member"]["staged_file_removed"] != true
            || native["artifact"]["member"]["chunks"] != native["artifact"]["served_chunks"]
            || native["artifact"]["member"]["bytes"] != native["artifact"]["bytes"]
        {
            return Err("enrollment_transport_or_stop_evidence_failed".into());
        }
        if native["lifecycle_boundary"] == "wall_timed_rollover" {
            let trigger: Value = serde_json::from_slice(&fs::read(
                evidence.join(format!("generation-{generation}/rollover-trigger.json")),
            )?)?;
            if trigger["rollover_at"] != serving["rollover_at"]
                || number(&trigger, "trigger_monotonic")?
                    < number(&serving["rollover_at"], "ticks")?
                || trigger["pending_continuation"] != true
                || cleaned["rollover_members"].is_null()
                || trigger["owner_wall_cap_ms"] != serving["owner_policy"]["wall_ms"]
                || trigger["stopped_nodes"] != json!(["a-owner", "b-member"])
            {
                return Err("timed_rollover_trigger_evidence_failed".into());
            }
            let renewals = trigger["renewals"]
                .as_array()
                .filter(|r| !r.is_empty())
                .ok_or("renewal_evidence_missing")?;
            for renewal in renewals {
                protocol::identity::verify(
                    &renewal["join"]["message"],
                    &renewal["join"]["signature"],
                    &member_key,
                )?;
                protocol::identity::verify(
                    &renewal["acceptance"]["message"],
                    &renewal["acceptance"]["signature"],
                    &owner_key,
                )?;
                protocol::identity::verify(
                    &renewal["ack"]["statement"],
                    &renewal["ack"]["signature"],
                    &member_key,
                )?;
                if renewal["ack"]["statement"]["accepted"]
                    != canonical::digest(&renewal["acceptance"])?
                    || renewal["acceptance"]["message"]["request_message_id"]
                        != renewal["join"]["message"]["message_id"]
                    || renewal["acceptance"]["message"]["accepted_incarnation"]
                        != serving["members"]["b-member"]["incarnation"]
                {
                    return Err("renewal_evidence_binding".into());
                }
            }
        }
        for stats in [&native["owner_native"]["stats"], &native["member_native"]] {
            if stats["context_closed"] != true
                || stats["active_bytes"] != 0
                || stats["reserved_memory_bytes"] != 0
                || stats["pressure_max"] != 1
                || stats["iroh_router_closed"] != true
                || stats["iroh_endpoint_closed_by_exit"] != true
            {
                return Err("native_resource_cleanup_evidence_failed".into());
            }
        }
        if native["planner_active"] != 0 || native["member_reaped"] != true {
            return Err("cleanup_evidence_failed".into());
        }
        fs::write(
            evidence.join(format!("events-{generation}.json")),
            serde_json::to_vec_pretty(&events)?,
        )?;
        service.publish().map_err(|e| e.code)?;
        let status = service.published_status().map_err(|e| e.code)?;
        if status["running"] != false
            || status["test_seams_active"] != true
            || status["test_seams"]["l6_coordinator"] != true
            || status["authority"]["swarm_created"] != true
        {
            return Err("published_status_binding".into());
        }
        fs::write(
            evidence.join(format!("status-{generation}.json")),
            serde_json::to_vec_pretty(&status)?,
        )?;
        summaries.push(json!({"lifecycle_boundary":native["lifecycle_boundary"],"started_by":native["started_by"],"lifecycle_run":native["lifecycle_run"],"lifecycle_epoch":native["lifecycle_epoch"],"stop_ms":stop_ms,"generation":generation,"terminal":terminal,"tokens":events.iter().filter(|e|e["type"]=="token").count(),"oracle_equal":!cancel,"member_pid":native["member_pid"],"owner_leaf_pid":native["owner_leaf_pid"]}));
        rollover(&mut service)?;
        pressure()?;
    }
    if crate::enrollment::identity(&private.join("owner"))?.public_key_record() != owner_key
        || crate::enrollment::identity(&private.join("member"))?.public_key_record() != member_key
        || serde_json::to_value(
            crate::enrollment::LocalMembership::load(&private.join("member"))?.consent,
        )? != consent
    {
        return Err("persisted_identity_or_consent_changed".into());
    }
    {
        let owner_endpoint = crate::iroh_frames::endpoint_secret(&private.join("owner"))?
            .public()
            .to_string();
        let member_endpoint = crate::iroh_frames::endpoint_secret(&private.join("member"))?
            .public()
            .to_string();
        for run in summaries.iter().filter(|run| run["terminal"] != "failed") {
            let generation = number(run, "generation")?;
            let native: Value = serde_json::from_slice(&fs::read(
                evidence.join(format!("generation-{generation}/result.json")),
            )?)?;
            if native["iroh_endpoints"] != json!({"owner":owner_endpoint,"member":member_endpoint})
            {
                return Err("persisted_iroh_endpoint_changed".into());
            }
        }
    }
    fs::write(
        evidence.join("summary.json"),
        serde_json::to_vec_pretty(
            &json!({"http":listener.local_addr()?.to_string(),"runs":summaries,"development_only":true,"native_profile":"MlxPackedInt8","transport":"Iroh-signed-NodeControl/ArtifactChunk/Router","persisted_iroh_endpoint_identity":true,"control_transport":"signed-node-control","loopback_tcp_control":false,"persisted_identity_and_consent":true,"signed_enrollment_and_fenced_stop":true,"wall_timed_rollover":summaries.first().is_some_and(|r| r["lifecycle_boundary"]=="wall_timed_rollover") && summaries.get(1).is_some_and(|r| r["started_by"]=="l6_continue_rollover"), "rollover":"L6 tick at injected L1 wall deadline, real cleanup, L7 continue_rollover on the same durable Core"}),
        )?,
    )?;
    println!("SLICE NATIVE PASS: {}", evidence.display());
    fs::remove_dir_all(private)?;
    Ok(())
}
