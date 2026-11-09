// SPDX-License-Identifier: AGPL-3.0-only
//! Opt-in local native compilation. No backend is linked into the API process.
//! Proofs originate in child workers; the reference is an independent pinned
//! Python recording. This is local qualification, not remote member authority.
use crate::{
    compiled_handoff::{Pin, Pins},
    compiler_inputs::InputArtifact,
};
use protocol::{canonical, identity::Identity};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use stage_contract::native_proof as contract;
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::Read,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::Instant,
};
pub mod shard_sandbox;
pub(crate) mod worker;

#[derive(Debug, Clone)]
pub struct Error(pub &'static str);
type Result<T> = std::result::Result<T, Error>;
fn digest(value: &Value) -> Result<String> {
    canonical::digest(value).map_err(|_| Error("native_document_invalid"))
}
fn text<'a>(value: &'a Value, key: &str) -> Result<&'a str> {
    value[key]
        .as_str()
        .filter(|s| !s.is_empty())
        .ok_or(Error("native_document_invalid"))
}
fn number(value: &Value, key: &str) -> Result<u64> {
    value[key].as_u64().ok_or(Error("native_document_invalid"))
}
fn fields(value: &Value, expected: &[&str]) -> bool {
    value.as_object().is_some_and(|map| {
        map.len() == expected.len() && expected.iter().all(|key| map.contains_key(*key))
    })
}
pub(crate) fn reference() -> Result<Value> {
    serde_json::from_str(contract::QWEN3_PACKED_ORACLE)
        .map_err(|_| Error("native_reference_invalid"))
}
pub(crate) fn file_pin(path: &Path) -> Result<Pin> {
    file_pin_until(path, &|| false)
}
/// [`file_pin`] whose hash consults `stopped` once per read; a Stop refuses
/// `compiler_stopped` instead of finishing a model-sized scan.
fn file_pin_until(path: &Path, stopped: &dyn Fn() -> bool) -> Result<Pin> {
    use std::os::unix::fs::OpenOptionsExt;
    if path
        .canonicalize()
        .map_err(|_| Error("native_path_invalid"))?
        != path
    {
        return Err(Error("native_path_invalid"));
    }
    let mut file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| Error("native_file_unavailable"))?;
    let metadata = file
        .metadata()
        .map_err(|_| Error("native_file_unavailable"))?;
    if !metadata.is_file() {
        return Err(Error("native_path_invalid"));
    }
    let mut hash = Sha256::new();
    let mut bytes = 0;
    let mut buffer = vec![0; 64 * 1024];
    loop {
        if stopped() {
            return Err(Error("compiler_stopped"));
        }
        let n = file
            .read(&mut buffer)
            .map_err(|_| Error("native_file_unavailable"))?;
        if n == 0 {
            break;
        }
        hash.update(&buffer[..n]);
        bytes += n as u64;
    }
    if bytes != metadata.len() {
        return Err(Error("native_file_changed"));
    }
    Ok(Pin {
        bytes,
        sha256: format!("{:x}", hash.finalize()),
    })
}
pub(crate) struct Config {
    binary: PathBuf,
    pin: Pin,
}
impl Config {
    pub(crate) fn from_environment() -> Result<Option<Self>> {
        let Some(binary) = std::env::var_os("MYCELIUM_COMPILER_NATIVE_BINARY") else {
            return Ok(None);
        };
        let binary = PathBuf::from(binary);
        let expected = std::env::var("MYCELIUM_COMPILER_NATIVE_SHA256")
            .map_err(|_| Error("native_binary_pin_required"))?;
        let pin = file_pin(&binary)?;
        if expected != pin.sha256 {
            return Err(Error("native_binary_changed"));
        }
        Ok(Some(Self { binary, pin }))
    }
    fn verify(&self) -> Result<()> {
        self.verify_until(&|| false)
    }
    fn verify_until(&self, stopped: &dyn Fn() -> bool) -> Result<()> {
        let pin = file_pin_until(&self.binary, stopped)?;
        if pin.sha256 != self.pin.sha256 || pin.bytes != self.pin.bytes {
            return Err(Error("native_binary_changed"));
        }
        Ok(())
    }
}
#[derive(Debug, Clone)]
pub struct Qualified {
    pub output: planner::chain_compile::BuildOutput,
    pub pins: Pins,
}
fn selected_weights(reference: &Value, range: [usize; 2]) -> Result<Value> {
    let weights = reference["recorded"]["weights"]
        .as_object()
        .ok_or(Error("native_reference_invalid"))?;
    let selected: serde_json::Map<_, _> = weights
        .iter()
        .filter(|(name, _)| {
            if name.as_str() == "model.embed_tokens.weight" {
                range[0] == 0 || range[1] == 28
            } else if name.as_str() == "model.norm.weight" {
                range[1] == 28
            } else {
                name.strip_prefix("model.layers.")
                    .and_then(|s| s.split('.').next())
                    .and_then(|s| s.parse::<usize>().ok())
                    .is_some_and(|n| (range[0]..range[1]).contains(&n))
            }
        })
        .map(|(k, v)| (k.clone(), v.clone()))
        .collect();
    if selected.is_empty() {
        return Err(Error("native_reference_invalid"));
    }
    Ok(Value::Object(selected))
}
pub(crate) fn validate_proof(
    proof: &Value,
    stage: &Value,
    nonce: &str,
    reference: &Value,
) -> Result<()> {
    let range: [usize; 2] = serde_json::from_value(stage["range"].clone())
        .map_err(|_| Error("native_stage_order_changed"))?;
    let identity = &reference["recorded"]["identity"];
    if !fields(proof, contract::PROOF_FIELDS)
        || proof["protocol"] != contract::PROOF_PROTOCOL
        || proof["nonce"] != nonce
        || proof["request_id"] != stage["request_id"]
        || proof["load_generation"] != stage["member_generation"]
        || proof["start"] != range[0]
        || proof["end"] != range[1]
        || proof["checkpoint_sha256"] != identity["checkpoint_sha256"]
        || proof["tensor_manifest_sha256"] != identity["manifest_sha256"]
        || proof["revision"] != identity["revision"]
        || proof["profile"] != "mlx_packed_int8"
        || number(proof, "worker_pid")? == 0
        || !text(proof, "host_id")?.starts_with("host-")
        || !text(proof, "boot_id")?.starts_with("boot-")
    {
        return Err(Error("native_proof_binding_mismatch"));
    }
    if proof["weights"] != selected_weights(reference, range)?
        || proof["weights_digest"] != digest(&proof["weights"])?
    {
        return Err(Error("native_resident_digest_mismatch"));
    }
    Ok(())
}
pub(crate) fn validate_evidence(plan: &Value, checkpoint: &Value, reference: &Value) -> Result<()> {
    let stages = plan["stages"]
        .as_array()
        .ok_or(Error("native_stage_order_changed"))?;
    let proofs = checkpoint["proofs"]
        .as_array()
        .ok_or(Error("native_proof_binding_mismatch"))?;
    let members = plan["members"]["members"]
        .as_array()
        .ok_or(Error("compiler_member_changed"))?;
    if stages.len() != 2 || proofs.len() != stages.len() || members.len() != stages.len() {
        return Err(Error("native_stage_order_changed"));
    }
    let nonce = text(plan, "nonce")?;
    if !contract::valid_nonce(nonce) {
        return Err(Error("native_proof_binding_mismatch"));
    }
    let mut end = 0;
    let mut nodes = BTreeSet::new();
    let mut pids = BTreeSet::new();
    for (stage, proof) in stages.iter().zip(proofs) {
        let range: [usize; 2] = serde_json::from_value(stage["range"].clone())
            .map_err(|_| Error("native_stage_order_changed"))?;
        if range[0] != end
            || range[1] <= range[0]
            || range[1] > 28
            || !nodes.insert(text(stage, "node_id")?)
            || !pids.insert(number(proof, "worker_pid")?)
        {
            return Err(Error("native_stage_order_changed"));
        }
        end = range[1];
        let member = members
            .iter()
            .find(|m| m["node_id"] == stage["node_id"])
            .ok_or(Error("compiler_member_changed"))?;
        if stage["member_generation"] != member["generation"]
            || stage["member_incarnation"] != member["incarnation"]
        {
            return Err(Error("compiler_member_changed"));
        }
        if digest(&stage["assignment"])? != format!("sha256:{}", text(stage, "request_id")?) {
            return Err(Error("native_assignment_changed"));
        }
        planner::compile::validate_assignment(&stage["assignment"])
            .map_err(|_| Error("native_assignment_changed"))?;
        if stage["assignment"]["node_id"] != stage["node_id"]
            || stage["assignment"]["range"]["start_layer"] != range[0]
            || stage["assignment"]["range"]["end_layer_exclusive"] != range[1]
        {
            return Err(Error("native_stage_order_changed"));
        }
        if plan["protocol"] == stage_contract::native_shard::PLAN_PROTOCOL {
            let shard = &stage["shard"];
            let recipe = &shard["recipe"];
            let mut body = recipe.clone();
            body.as_object_mut()
                .ok_or(Error("native_shard_recipe_mismatch"))?
                .remove("node_plan_digest");
            if shard["protocol"] != stage_contract::native_shard::PROTOCOL
                || shard["range"] != stage["range"]
                || shard["node_id"] != stage["node_id"]
                || recipe["node_id"] != stage["node_id"]
                || recipe["model_id"] != "Qwen/Qwen3-0.6B"
                || recipe["resolved_commit"] != reference["recorded"]["identity"]["revision"]
                || shard["recipe_digest"] != digest(&body)?
                || recipe["node_plan_digest"] != shard["recipe_digest"]
                || recipe["files"].as_array().is_none_or(|a| a.len() != 1)
            {
                return Err(Error("native_shard_recipe_mismatch"));
            }
            for key in ["path", "size_bytes", "content_digest"] {
                if shard["transfer"][key] != recipe["files"][0][key] {
                    return Err(Error("native_shard_binding_mismatch"));
                }
            }
        }
        validate_proof(proof, stage, nonce, reference)?;
    }
    if !fields(checkpoint, contract::CHECKPOINT_FIELDS) {
        return Err(Error("native_challenge_mismatch"));
    }
    let steps = checkpoint["steps"]
        .as_array()
        .ok_or(Error("native_challenge_mismatch"))?;
    let cleanup = checkpoint["cleanup"]
        .as_array()
        .ok_or(Error("native_cleanup_unproven"))?;
    if steps.len() != contract::CHALLENGE_OUTPUT_TOKENS
        || cleanup.len() != stages.len()
        || cleanup.iter().any(|c| {
            c["active_bytes"] != 0 || c["reserved_memory_bytes"] != 0 || c["context_closed"] != true
        })
    {
        return Err(Error("native_cleanup_unproven"));
    }
    for (i, step) in steps.iter().enumerate() {
        let input = if i == 0 {
            checkpoint["prompt_token_ids"].clone()
        } else {
            json!([checkpoint["output_token_ids"][i - 1]])
        };
        if step["input_token_ids"] != input
            || step["output_token_id"] != checkpoint["output_token_ids"][i]
            || step["stage_payload_digests"]
                .as_array()
                .is_none_or(|hashes| {
                    hashes.len() != stages.len()
                        || hashes.iter().any(|h| {
                            h.as_str().is_none_or(|s| {
                                s.len() != 71
                                    || !s.starts_with("sha256:")
                                    || !s[7..]
                                        .bytes()
                                        .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
                            })
                        })
                })
        {
            return Err(Error("native_challenge_mismatch"));
        }
    }
    let expected = &reference["recorded"]["tokens"]
        .as_array()
        .ok_or(Error("native_reference_invalid"))?[..contract::CHALLENGE_OUTPUT_TOKENS];
    if end != 28
        || checkpoint["protocol"] != contract::CHECKPOINT_PROTOCOL
        || checkpoint["state"] != "passed"
        || checkpoint["prompt_token_ids"] != reference["recorded"]["workload"]["prompt_ids"]
        || checkpoint["reference_digest"] != digest(reference)?
        || plan["reference_digest"] != checkpoint["reference_digest"]
    {
        return Err(Error("native_challenge_mismatch"));
    }
    if checkpoint["output_token_ids"] != json!(expected) {
        return Err(Error("native_reference_token_mismatch"));
    }
    Ok(())
}

/// `None` preserves the existing explicit missing-producer refusal when no
/// native executable has been independently pinned by the local operator.
pub fn build(
    artifact: &InputArtifact,
    signer: &Identity,
    current: &dyn Fn() -> Result<BTreeMap<String, Value>>,
    cancel: Arc<AtomicBool>,
) -> Result<Option<Qualified>> {
    let request = artifact.request();
    if request.model_id != "Qwen/Qwen3-0.6B" {
        return Ok(None);
    }
    let Some(config) = Config::from_environment()? else {
        return Ok(None);
    };
    let started = Instant::now();
    let reference = reference()?;
    let oracle = &reference["recorded"];
    if request.resolved_commit != text(&oracle["identity"], "revision")?
        || request.node_ids.len() != 2
        || request.managed_layer_ranges.len() != 2
    {
        return Err(Error("native_model_unsupported"));
    }
    let check = || {
        let members = current().map_err(|e| Error(e.0))?;
        artifact
            .validate_members(&members, crate::app_host::now())
            .map_err(|e| Error(e.0))?;
        artifact.validate().map_err(|e| Error(e.0))
    };
    check()?;
    if !request.stage_sharded {
        artifact
            .validate_assignments(&current()?, crate::app_host::now())
            .map_err(|e| Error(e.0))?;
    }
    // Shard production has already independently checked source equality and
    // retained the recipe in this artifact. check() compares held documents;
    // the source pin below and worker transfer/recipe checks detect later drift.
    let shard_document = if request.stage_sharded {
        Some(
            serde_json::from_slice::<Value>(
                &fs::read(artifact.root().join("source-shards.private.json"))
                    .map_err(|_| Error("native_shard_recipe_mismatch"))?,
            )
            .map_err(|_| Error("native_shard_recipe_mismatch"))?,
        )
    } else {
        None
    };
    // Every parent-side whole-file scan below observes the compiler's Stop.
    let stopped = || cancel.load(Ordering::SeqCst);
    let source = artifact.source().source_root.join("model.safetensors");
    let source_pin = file_pin_until(&source, &stopped)?;
    if source_pin.sha256 != text(&oracle["identity"], "checkpoint_sha256")?
        || source_pin.bytes != number(&oracle["identity"], "checkpoint_bytes")?
    {
        return Err(Error("native_source_mismatch"));
    }
    let nonce = crate::app_host::hex(
        &crate::app_host::random(16).map_err(|_| Error("native_nonce_unavailable"))?,
    );
    let assignments: Value = serde_json::from_slice(
        &fs::read(artifact.root().join("source-assignments.private.json"))
            .map_err(|_| Error("native_assignment_unavailable"))?,
    )
    .map_err(|_| Error("native_assignment_unavailable"))?;
    let mut workers = Vec::new();
    let mut stages = Vec::new();
    let mut proofs = Vec::new();
    for (node, range) in request.node_ids.iter().zip(&request.managed_layer_ranges) {
        check()?;
        let member = artifact.membership()["members"]
            .as_array()
            .and_then(|m| m.iter().find(|m| m["node_id"] == *node))
            .ok_or(Error("compiler_member_changed"))?;
        let assignment = assignments["assignments"]
            .as_array()
            .and_then(|a| a.iter().find(|a| a["node_id"] == *node))
            .ok_or(Error("native_assignment_unavailable"))?;
        let id = digest(assignment)?;
        let id = id
            .strip_prefix("sha256:")
            .ok_or(Error("native_assignment_changed"))?;
        let mut stage = json!({"node_id":node,"range":range,"request_id":id,"member_generation":member["generation"],"member_incarnation":member["incarnation"],"assignment":assignment});
        let memory =
            budgets::BudgetId::PyFinalScriptsA13OperatorSingleMacUiRehearsalMaxMemoryBytesL171C55
                .bytes()
                .map_err(|_| Error("budget_unavailable"))?
                .get();
        let tokens = oracle["workload"]["prompt_ids"]
            .as_array()
            .ok_or(Error("native_reference_invalid"))?
            .len()
            + contract::CHALLENGE_OUTPUT_TOKENS;
        let mut prepare = json!({"command":"prepare","checkpoint":source,"start":range[0],"end":range[1],"total":28,"memory":memory,"tokens":tokens,"id":id,"generation":member["generation"],
            "stage_image":{"model_id":request.model_id,"revision":request.resolved_commit,"image_digest":oracle["identity"]["checkpoint_sha256"],"tensor_manifest":oracle["identity"]["manifest_sha256"],"profile":"packed_int8","layers":{"start":range[0],"end":range[1]},"total_layers":28,"source_metadata":{"header_sha256":oracle["identity"]["header_sha256"]}}});
        if let Some(document) = &shard_document {
            let recipe = &document["node_slice_plans"][node];
            let transfer = &document["node_files"][node][0];
            let name = text(transfer, "path")?
                .strip_prefix("deployment/")
                .filter(|s| !s.contains('/'))
                .ok_or(Error("native_shard_path_invalid"))?;
            let shard = json!({"protocol":stage_contract::native_shard::PROTOCOL,
                "node_id":node,"range":range,"transfer":transfer,"recipe":recipe,"recipe_digest":recipe["node_plan_digest"]});
            prepare["command"] = json!(stage_contract::native_shard::COMMAND);
            prepare["checkpoint"] = json!(artifact.root().join("stage-shards").join(name));
            prepare["shard"] = shard.clone();
            stage["shard"] = shard;
        }
        let mut worker = worker::Worker::start(
            &config,
            &prepare,
            cancel.clone(),
            &artifact.source().source_root,
        )?;
        let (reply,body)=worker.request(&json!({"command":contract::COMMAND,"protocol":contract::REQUEST_PROTOCOL,"nonce":nonce}),&[])?;
        if reply["ok"] != true
            || !body.is_empty()
            || reply["proof"]["worker_pid"] != worker.pid()?
        {
            return Err(Error("native_proof_binding_mismatch"));
        }
        validate_proof(&reply["proof"], &stage, &nonce, &reference)?;
        proofs.push(reply["proof"].clone());
        stages.push(stage);
        workers.push(worker);
    }
    let mut input = oracle["workload"]["prompt_ids"].clone();
    let mut tokens = Vec::new();
    let mut steps = Vec::new();
    for _ in 0..contract::CHALLENGE_OUTPUT_TOKENS {
        check()?;
        let mut payload = Vec::new();
        let mut hashes = Vec::new();
        for (i, worker) in workers.iter_mut().enumerate() {
            let command = if i == 0 {
                json!({"command":"step","tokens":input})
            } else {
                json!({"command":"step"})
            };
            let (reply, body) = worker.request(&command, &payload)?;
            if reply != json!({"ok":true}) {
                return Err(Error("native_step_reply_invalid"));
            }
            hashes.push(protocol::artifact_grant::range_digest(&body));
            payload = body;
        }
        let token = worker::greedy(&payload)?;
        steps.push(
            json!({"input_token_ids":input,"stage_payload_digests":hashes,"output_token_id":token}),
        );
        tokens.push(token);
        input = json!([token]);
    }
    let mut cleanup = Vec::new();
    for worker in workers {
        cleanup.push(worker.close()?);
    }
    check()?;
    config.verify_until(&stopped)?;
    let final_source = file_pin_until(&source, &stopped)?;
    if final_source.sha256 != source_pin.sha256 || final_source.bytes != source_pin.bytes {
        return Err(Error("native_source_mismatch"));
    }
    let checkpoint = json!({"protocol":contract::CHECKPOINT_PROTOCOL,"state":"passed","run_id":artifact.observations()["run_id"],"deployment_id":request.deployment_id,
        "reference_digest":digest(&reference)?,"prompt_token_ids":oracle["workload"]["prompt_ids"],"output_token_ids":tokens,"proofs":proofs,"steps":steps,"cleanup":cleanup});
    let mut plan = json!({"protocol":contract::PLAN_PROTOCOL,"run_id":artifact.observations()["run_id"],"deployment_id":request.deployment_id,"epoch":0,
        "scope":"local_native_qualification","route_ready":false,"nonce":nonce,"stages":stages,"members":artifact.membership(),
        "source":{"path":source,"bytes":source_pin.bytes,"sha256":source_pin.sha256},
        "binary":{"path":config.binary,"bytes":config.pin.bytes,"sha256":config.pin.sha256},
        "reference_digest":digest(&reference)?,"documents":{},"verification_keys":artifact.verification_keys(),"signer":signer.public_key_record()});
    if request.stage_sharded {
        plan["protocol"] = json!(stage_contract::native_shard::PLAN_PROTOCOL);
        // Source was rehashed above; emit/open rehashes every retained shard.
        // Recompiling the same recipe again cannot strengthen those byte pins.
    }
    validate_evidence(&plan, &checkpoint, &reference)?;
    emit(
        artifact,
        signer,
        &mut plan,
        &checkpoint,
        started.elapsed().as_secs_f64(),
        &stopped,
    )
    .map(Some)
}

fn emit(
    artifact: &InputArtifact,
    signer: &Identity,
    plan: &mut Value,
    checkpoint: &Value,
    seconds: f64,
    stopped: &dyn Fn() -> bool,
) -> Result<Qualified> {
    let root = artifact.root();
    fs::create_dir(root.join("compiled")).map_err(|_| Error("native_output_exists"))?;
    let store = |name: &str, doc: &Value| {
        crate::preferences::private_state::store(
            &root.join(name),
            doc,
            crate::preferences::private_state::private_json_limit()
                .map_err(|_| Error("native_output_failed"))?,
        )
        .map_err(|_| Error("native_output_failed"))
    };
    store(
        "template.json",
        &json!({"protocol":contract::TEMPLATE_PROTOCOL,"members":artifact.membership(),"verification_keys":artifact.verification_keys(),"source_manifest":artifact.source().manifest}),
    )?;
    store("compiled/local-challenge-checkpoint.json", checkpoint)?;
    store(
        "compiled/build-report.json",
        &json!({"protocol":contract::REPORT_PROTOCOL,"run_id":plan["run_id"],"route_ready":false,"scope":"local_native_qualification","build_seconds":seconds,"challenge_output_token_ids":checkpoint["output_token_ids"]}),
    )?;
    store(
        "compiled/compilation-cache.json",
        &json!({"protocol":contract::CACHE_PROTOCOL,"cache_hit":false,"compilation_cache_key":artifact.request().compilation_cache_key}),
    )?;
    let mut pins = Pins::new();
    let mut documents = serde_json::Map::new();
    for name in [
        "compile-start.private.json",
        "template.json",
        "topology.json",
        "observations.json",
        "layer-split.json",
        "compiled/build-report.json",
        "compiled/local-challenge-checkpoint.json",
        "compiled/compilation-cache.json",
    ] {
        let value: Value = serde_json::from_slice(
            &fs::read(root.join(name)).map_err(|_| Error("native_output_failed"))?,
        )
        .map_err(|_| Error("native_output_failed"))?;
        documents.insert(name.into(), json!(digest(&value)?));
        if name == "compile-start.private.json" {
            plan["epoch"] = value["epoch"].clone();
        }
        pins.insert(name.into(), file_pin(&root.join(name))?);
    }
    plan["documents"] = Value::Object(documents);
    plan["signature"] = signer
        .sign(plan)
        .map_err(|_| Error("native_plan_sign_failed"))?;
    store("compiled/operator-plan.json", plan)?;
    let plan_digest = digest(plan)?;
    store(
        "compile-complete.private.json",
        &json!({"protocol":"mycelium.chain_compile_complete.v1","state":"compiled","run_id":plan["run_id"],"deployment_id":plan["deployment_id"],"epoch":plan["epoch"],"operator_plan_digest":plan_digest,"route_ready":false}),
    )?;
    for name in [
        "compiled/operator-plan.json",
        "compile-complete.private.json",
    ] {
        pins.insert(name.into(), file_pin(&root.join(name))?);
    }
    let handoff = crate::compiled_handoff::CompiledHandoff::open_until(root, &pins, stopped)
        .map_err(|e| {
            if e.code == "compiled_handoff_stopped" {
                return Error("compiler_stopped");
            }
            eprintln!("native_handoff_refused: {e:?}");
            Error("native_handoff_refused")
        })?;
    handoff.revalidate_until(stopped).map_err(|e| {
        Error(if e.code == "compiled_handoff_stopped" {
            "compiler_stopped"
        } else {
            "native_handoff_changed"
        })
    })?;
    let hosts: BTreeSet<_> = checkpoint["proofs"]
        .as_array()
        .ok_or(Error("native_proof_binding_mismatch"))?
        .iter()
        .map(|p| text(p, "host_id").map(str::to_owned))
        .collect::<Result<_>>()?;
    eprintln!(
        "native compiler challenge {:?}; reference {:?}; native seconds={seconds}",
        checkpoint["output_token_ids"],
        reference()?["recorded"]["tokens"]
            .as_array()
            .map(|v| &v[..contract::CHALLENGE_OUTPUT_TOKENS])
    );
    Ok(Qualified {
        output: planner::chain_compile::BuildOutput {
            operator_plan_digest: plan_digest,
            physical_host_ids: hosts.into_iter().collect(),
            cache_hit: false,
            build_seconds: seconds,
            slice_plan_digest: None,
            node_slice_plan_digests: None,
        },
        pins,
    })
}
