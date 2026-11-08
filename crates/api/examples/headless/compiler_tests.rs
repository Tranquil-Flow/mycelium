// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded source-artifact tests. Controlled member/observation documents are
//! not authenticated hello sessions, native model execution, or BuildOutput.
use super::*;
use api::compiler_inputs::{self, InputArtifact};
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);

fn storage_policy(root: &Path, bytes: u64) {
    resource_policy::write(
        root,
        &json!({"peer_visibility":true,"power":"ac_only",
        "storage_bytes":bytes,"network":"unmetered_only","background":false,
        "diagnostics":false,"thermal":"stop_on_serious"}),
    )
    .unwrap();
}

struct Fixture {
    root: PathBuf,
    source: api::serving_source::Prepared,
    request: BuildRequest,
    members: BTreeMap<String, Value>,
}
impl Fixture {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "laneB-compiler-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        storage_policy(&root, 4_000_000_000);
        let fixture: Value = serde_json::from_str(include_str!(
            "../../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let row = &fixture["cases"][0];
        let source_root = root.join("snapshot");
        fs::create_dir(&source_root).unwrap();
        for (name, encoded) in row["source_files"].as_object().unwrap() {
            let encoded = encoded.as_str().unwrap();
            let bytes: Vec<_> = (0..encoded.len())
                .step_by(2)
                .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
                .collect();
            fs::write(source_root.join(name), bytes).unwrap();
        }
        let manifest = api::local_manifest::compile_local_model_manifest(
            &api::local_manifest::LocalModelSource {
                root: source_root.clone(),
                model_id: row["manifest"]["model_id"].as_str().unwrap().into(),
                requested_revision: "main".into(),
                resolved_commit: row["manifest"]["resolved_commit"].as_str().unwrap().into(),
                tokenizer_assets: Ok(Vec::new()),
            },
        )
        .unwrap();
        let source = api::serving_source::Prepared {
            source_root,
            source_bytes: manifest["files"]
                .as_array()
                .unwrap()
                .iter()
                .map(|r| r["size_bytes"].as_u64().unwrap())
                .sum(),
            manifest_digest: planner::bound::manifest_digest_ref(&manifest).unwrap(),
            manifest: manifest.as_object().unwrap().clone(),
        };
        let ids: Vec<String> = vec!["node-0".into(), "node-1".into(), "node-2".into()];
        let ranges: Vec<[usize; 2]> = serde_json::from_value(row["ranges"].clone()).unwrap();
        let request = BuildRequest {
            model_id: manifest["model_id"].as_str().unwrap().into(),
            resolved_commit: manifest["resolved_commit"].as_str().unwrap().into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
            route_label: "a13",
            stage_sharded: false,
            compilation_cache_key: "controlled-source-boundary-not-admitted-native".into(),
            node_ids: ids.clone(),
            managed_layer_ranges: ranges.clone(),
            layer_split: json!({"stages":ids.iter().zip(&ranges).map(|(n,r)|json!({"node_id":n,"start":r[0],"end":r[1]})).collect::<Vec<_>>()}),
        };
        let members = ids.into_iter().map(|n| (n.clone(), json!({"node_id":n,
            "generation":1,"incarnation":"controlled-incarnation","endpoint_id":format!("controlled-{n}"),
            "verification_key_digest":"controlled-not-a-key","lease_expires_at":100}))).collect();
        Self {
            root,
            source,
            request,
            members,
        }
    }
    fn emit(&self) -> InputArtifact {
        self.emit_run("source-boundary")
    }
    fn emit_run(&self, run: &str) -> InputArtifact {
        let nodes: Vec<_> = self
            .request
            .node_ids
            .iter()
            .map(|n| {
                json!({"node_id":n,
            "staging_root":format!("/controlled/{n}"),"runtime_backend":"mlx"})
            })
            .collect();
        let observations: Vec<_> = self
            .request
            .node_ids
            .iter()
            .map(|n| json!({"node_id":n,"host_id":format!("controlled-{n}")}))
            .collect();
        compiler_inputs::emit(&self.root, run, &self.request.deployment_id, 0,
            &self.request, &self.source,
            &json!({"protocol":"mycelium.qwen_live_topology.v1","nodes":nodes}),
            &json!({"run_id":run,"deployment_id":self.request.deployment_id,"observations":observations}),
            &json!({}), &self.members.values().cloned().collect::<Vec<_>>()).unwrap()
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}

#[test]
fn source_assignment_artifacts_reopen_and_refuse_member_or_source_drift() {
    let f = Fixture::new();
    let mut artifact = f.emit();
    artifact.produce_assignments(&f.members, 0.0).unwrap();
    artifact.validate_assignments(&f.members, 0.0).unwrap();
    let document: Value = serde_json::from_slice(
        &fs::read(artifact.root().join("source-assignments.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(document["state"], "metadata_only");
    assert_eq!(document["route_ready"], false);
    let assignments = document["assignments"].as_array().unwrap();
    assert_eq!(assignments.len(), f.request.node_ids.len());
    for assignment in assignments {
        planner::compile::validate_assignment(assignment).unwrap();
        assert_eq!(assignment["manifest_digest"], f.source.manifest_digest);
        assert_eq!(assignment["route_ready"], false);
    }
    assert!(
        !artifact
            .root()
            .join("compile-complete.private.json")
            .exists()
    );
    assert!(!artifact.root().join("compiled/operator-plan.json").exists());
    for key in [
        "generation",
        "incarnation",
        "endpoint_id",
        "verification_key_digest",
    ] {
        let mut changed = f.members.clone();
        changed.get_mut("node-0").unwrap()[key] = json!("changed");
        assert_eq!(
            artifact.validate_assignments(&changed, 0.0).unwrap_err().0,
            "compiler_member_changed"
        );
    }
    assert_eq!(
        artifact
            .validate_assignments(&f.members, 100.0)
            .unwrap_err()
            .0,
        "compiler_member_changed"
    );
    let weights = f.source.source_root.join("model.safetensors");
    let mut data = fs::read(&weights).unwrap();
    *data.last_mut().unwrap() ^= 1;
    fs::write(weights, data).unwrap();
    assert_eq!(
        artifact
            .validate_assignments(&f.members, 0.0)
            .unwrap_err()
            .0,
        "compiler_model_changed"
    );
}

#[test]
fn source_assignment_artifact_tampering_is_not_republished() {
    let f = Fixture::new();
    let mut artifact = f.emit();
    artifact.produce_assignments(&f.members, 0.0).unwrap();
    let path = artifact.root().join("source-assignments.private.json");
    let mut document: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
    document["assignments"][0]["route_ready"] = json!(true);
    api::preferences::private_state::store(
        &path,
        &document,
        api::preferences::private_state::private_json_limit().unwrap(),
    )
    .unwrap();
    assert_eq!(
        artifact
            .validate_assignments(&f.members, 0.0)
            .unwrap_err()
            .0,
        "compiler_inputs_changed"
    );
    assert!(artifact.produce_assignments(&f.members, 0.0).is_err());
}

#[test]
fn source_assignments_refuse_stale_members_before_writing_or_overwriting() {
    let f = Fixture::new();
    let mut artifact = f.emit();
    let mut changed = f.members.clone();
    changed.get_mut("node-0").unwrap()["generation"] = json!(2);
    assert_eq!(
        artifact.produce_assignments(&changed, 0.0).unwrap_err().0,
        "compiler_member_changed"
    );
    let path = artifact.root().join("source-assignments.private.json");
    assert!(!path.exists());
    fs::write(&path, b"foreign-output").unwrap();
    assert_eq!(
        artifact.produce_assignments(&f.members, 0.0).unwrap_err().0,
        "compiler_outputs_changed"
    );
    assert_eq!(fs::read(path).unwrap(), b"foreign-output");
}

#[test]
fn source_assignment_producer_refuses_changed_config_and_symlinked_weight() {
    for symlink in [false, true] {
        let f = Fixture::new();
        let mut artifact = f.emit();
        if symlink {
            let path = f.source.source_root.join("model.safetensors");
            let other = f.root.join("foreign.safetensors");
            fs::rename(&path, &other).unwrap();
            std::os::unix::fs::symlink(&other, path).unwrap();
        } else {
            let path = f.source.source_root.join("config.json");
            let mut config: Value = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
            config["hidden_size"] = json!(999);
            fs::write(path, serde_json::to_vec(&config).unwrap()).unwrap();
        }
        assert_eq!(
            artifact.produce_assignments(&f.members, 0.0).unwrap_err().0,
            "compiler_model_changed"
        );
        assert!(
            !artifact
                .root()
                .join("source-assignments.private.json")
                .exists()
        );
    }
}

#[test]
fn actual_compile_port_without_admitted_observation_never_becomes_ready() {
    let f = Fixture::new();
    let core = Arc::new(Mutex::new(load_core(f.root.join("host")).unwrap()));
    let observation = Observation(Arc::new(Mutex::new(observation::adapter(Arc::downgrade(
        &core,
    )))));
    let mut compiler = Compiler::new(Arc::downgrade(&core), observation);
    assert!(compiler.compile_context().is_err());
    compiler.start_build(&f.request);
    assert!(matches!(compiler.poll_build(), BuildPoll::Done(Err(_))));
    assert_eq!(status(&core).to_value()["route_ready"], false);
}

#[test]
fn repeated_shard_runs_cannot_exceed_retained_storage_consent() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    for index in 0..8 {
        let mut first = f.emit_run(&format!("quota-{index}"));
        first.produce_stage_shards(&f.members, 0.0).unwrap();
        drop(first);
    }
    let mut second = f.emit_run("quota-overflow");
    assert_eq!(
        second.produce_stage_shards(&f.members, 0.0).unwrap_err().0,
        "compiler_storage_quota_exceeded"
    );
    assert!(!second.root().join("stage-shards").exists());
}

#[test]
fn interrupted_shard_write_keeps_exact_reservation_and_blocks_new_bytes() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    storage_policy(&f.root, 100_000_000);
    let artifact = f.emit_run("interrupted");
    let files: BTreeMap<_, _> = f.source.manifest["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|r| {
            let n = r["path"].as_str().unwrap();
            (
                n.to_owned(),
                fs::File::open(f.source.source_root.join(n)).unwrap(),
            )
        })
        .collect();
    let source_path = f.source.source_root.join(files.keys().next().unwrap());
    let original = fs::read(&source_path).unwrap();
    let manifest = Value::Object(f.source.manifest.clone());
    let mut held = None;
    let mut exact = 0;
    let output = artifact.root().join("stage-shards");
    let result = planner::source_plan::producer::emit_stage_shards(
        &f.request,
        &f.request,
        planner::source_plan::PreparedSource {
            manifest: &manifest,
            manifest_digest: &f.source.manifest_digest,
            files: &files,
            checkpoint: &|| Ok(()),
        },
        &output,
        &mut |bytes| {
            held = Some(artifact.reserve_stage_output(bytes).unwrap());
            exact = bytes;
            // Real IO interruption after input verification/reservation: the
            // writer emits its header, then encounters truncated source bytes.
            fs::OpenOptions::new()
                .write(true)
                .open(&source_path)
                .unwrap()
                .set_len(0)
                .unwrap();
            Ok(())
        },
    );
    assert_eq!(result.unwrap_err().code, "slice_source_truncated");
    let written: u64 = fs::read_dir(&output)
        .unwrap()
        .map(|e| e.unwrap().metadata().unwrap().len())
        .sum();
    assert!(written > 0 && written < exact);
    fs::write(source_path, original).unwrap();
    drop(held); // No refund, including an abrupt process loss before complete().
    drop(artifact);
    let ledger: Value = serde_json::from_slice(
        &fs::read(f.root.join("chain-runs/compiler-storage.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(ledger["entries"]["interrupted"]["bytes"], exact);
    assert_eq!(
        ledger["entries"]["interrupted"]["phase"],
        "cleanup_required"
    );
    let next = f.emit_run("after-interruption");
    assert_eq!(
        next.reserve_stage_output(100_000_000 - exact + 1)
            .unwrap_err()
            .0,
        "compiler_storage_quota_exceeded"
    );
    assert!(!next.root().join("stage-shards").exists());
}

#[test]
fn shard_storage_requires_consent_and_refuses_untracked_outputs_or_busy_ledger() {
    use std::os::unix::fs::DirBuilderExt;
    let f = Fixture::new();
    let artifact = f.emit_run("storage-guards");
    fs::remove_file(f.root.join(resource_policy::FILE)).unwrap();
    assert_eq!(
        artifact.reserve_stage_output(1).unwrap_err().0,
        "compiler_storage_consent_required"
    );
    storage_policy(&f.root, 100_000_000);
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(f.root.join("chain-runs/compiler-storage.lock"))
        .unwrap();
    lock.lock().unwrap();
    assert_eq!(
        artifact.reserve_stage_output(1).unwrap_err().0,
        "compiler_storage_busy"
    );
    drop(lock);
    fs::DirBuilder::new()
        .mode(0o700)
        .create(artifact.root().join("stage-shards"))
        .unwrap();
    assert_eq!(
        artifact.reserve_stage_output(1).unwrap_err().0,
        "compiler_storage_untracked_output"
    );
    assert!(artifact.root().join("stage-shards").exists());
}

#[test]
fn compiler_cancellation_reaches_manifest_hash_and_artifact_revalidation() {
    let mut f = Fixture::new();
    f.request.stage_sharded = true;
    let checks = std::cell::Cell::new(0);
    let source = api::local_manifest::LocalModelSource {
        root: f.source.source_root.clone(),
        model_id: f.request.model_id.clone(),
        requested_revision: f.source.manifest["requested_revision"]
            .as_str()
            .unwrap()
            .into(),
        resolved_commit: f.request.resolved_commit.clone(),
        tokenizer_assets: Ok(Vec::new()),
    };
    let result = api::local_manifest::compile_local_model_manifest_checked(&source, &|| {
        checks.set(checks.get() + 1);
        if checks.get() == 3 {
            Err(api::local_manifest::CompileError("compiler_stopped".into()))
        } else {
            Ok(())
        }
    });
    assert_eq!(result.unwrap_err().0, "compiler_stopped");
    assert_eq!(checks.get(), 3);
    let mut artifact = f.emit_run("cancel-revalidation");
    artifact.produce_stage_shards(&f.members, 0.0).unwrap();
    let cancel = Arc::new(std::sync::atomic::AtomicBool::new(false));
    artifact.bind_cancellation(cancel.clone()).unwrap();
    cancel.store(true, Ordering::SeqCst);
    assert_eq!(
        artifact
            .validate_stage_shards(&f.members, 0.0)
            .unwrap_err()
            .0,
        "compiler_stopped"
    );
    assert!(artifact.root().join("stage-shards").exists());
}

#[test]
fn stage_shard_artifact_reopens_real_bytes_and_rejects_tampering() {
    for mutation in ["payload", "document", "symlink", "source", "member"] {
        let mut f = Fixture::new();
        f.request.stage_sharded = true;
        let mut artifact = f.emit();
        let mut stale = f.members.clone();
        stale.get_mut("node-0").unwrap()["generation"] = json!(2);
        assert_eq!(
            artifact.produce_stage_shards(&stale, 0.0).unwrap_err().0,
            "compiler_member_changed"
        );
        assert!(!artifact.root().join("stage-shards").exists());
        artifact.produce_stage_shards(&f.members, 0.0).unwrap();
        artifact.validate_stage_shards(&f.members, 0.0).unwrap();
        assert_eq!(
            artifact
                .produce_stage_shards(&f.members, 0.0)
                .unwrap_err()
                .0,
            "compiler_already_started"
        );
        let document_path = artifact.root().join("source-shards.private.json");
        let mut document: Value =
            serde_json::from_slice(&fs::read(&document_path).unwrap()).unwrap();
        assert_eq!(document["state"], "bytes_only");
        assert_eq!(document["route_ready"], false);
        assert!(
            api::compiled_handoff::CompiledHandoff::open(artifact.root(), &BTreeMap::new())
                .is_err(),
            "source/shard output must not satisfy a compiled handoff"
        );
        let shard = artifact
            .root()
            .join("stage-shards/model-stage-001-of-003.safetensors");
        let expected = match mutation {
            "payload" => {
                let mut bytes = fs::read(&shard).unwrap();
                *bytes.last_mut().unwrap() ^= 1;
                fs::write(&shard, bytes).unwrap();
                "slice_plan_file_digest_mismatch"
            }
            "document" => {
                document["route_ready"] = json!(true);
                api::preferences::private_state::store(
                    &document_path,
                    &document,
                    api::preferences::private_state::private_json_limit().unwrap(),
                )
                .unwrap();
                "compiler_inputs_changed"
            }
            "symlink" => {
                let foreign = f.root.join("replacement.safetensors");
                fs::rename(&shard, &foreign).unwrap();
                std::os::unix::fs::symlink(&foreign, &shard).unwrap();
                "compiler_source_path_invalid"
            }
            "source" => {
                let source = f.source.source_root.join("model.safetensors");
                let mut bytes = fs::read(&source).unwrap();
                *bytes.last_mut().unwrap() ^= 1;
                fs::write(source, bytes).unwrap();
                "compiler_model_changed"
            }
            "member" => "compiler_member_changed",
            _ => unreachable!(),
        };
        let current = if mutation == "member" {
            &stale
        } else {
            &f.members
        };
        assert_eq!(
            artifact.validate_stage_shards(current, 0.0).unwrap_err().0,
            expected,
            "{mutation}"
        );
        assert!(!artifact.root().join("compiled/operator-plan.json").exists());
    }
}

/// Controlled hello responses exercise the real observation validator. They are
/// not physical-host or native-load evidence; signatures below are real.
struct ControlledHellos {
    run: String,
    deployment: String,
    nodes: Vec<String>,
}
struct ControlledHello(Value);
impl runtime::observe::HelloSession for ControlledHello {
    fn send_hello(&mut self, command: &str) -> Result<Value, Option<String>> {
        let mut response = self.0.clone();
        response["command_id"] = json!(command);
        Ok(response)
    }
    fn close(&mut self) -> Result<(), runtime::observe::CloseFailed> {
        Ok(())
    }
    fn cleanup_error(&self) -> bool {
        false
    }
    fn stop_response_ok(&self) -> Option<bool> {
        None
    }
}
impl runtime::observe::HelloSessions for ControlledHellos {
    fn run_id(&self) -> &str {
        &self.run
    }
    fn deployment_id(&self) -> &str {
        &self.deployment
    }
    fn bound_nodes(&self) -> std::collections::BTreeSet<String> {
        self.nodes.iter().cloned().collect()
    }
    fn binding_current(&mut self, node: &str) -> bool {
        self.nodes.iter().any(|n| n == node)
    }
    fn open(
        &mut self,
        node: &str,
        _: f64,
    ) -> Result<Box<dyn runtime::observe::HelloSession + '_>, Option<String>> {
        let index = self.nodes.iter().position(|n| n == node).unwrap();
        Ok(Box::new(ControlledHello(json!({
            "protocol":runtime::observe::CONTROL_COMMAND_PROTOCOL,"node_id":node,
            "command_id":"pending","ok":true,"route_ready":false,
            "result":{"protocol":runtime::observe::CONTROL_COMMAND_PROTOCOL,
                "node_id":node,"run_id":self.run,"deployment_id":self.deployment,
                "route_ready":false,"state":"NEW",
                "host_id":format!("host-{index:032x}"),"boot_id":format!("boot-{index:032x}")}
        }))))
    }
}

#[test]
fn admitted_observations_drive_actual_compiler_worker_to_native_challenge_refusal() {
    worker_boundary(false);
}

#[test]
fn stage_sharded_worker_retains_real_bytes_but_refuses_native_challenge() {
    worker_boundary(true);
}

/// Full-size source admission is deliberately opt-in. This is the acceptance RED
/// for the missing native producer, not a model-free fixture made executable.
#[test]
#[ignore = "requires local Qwen3-0.6B + Metal"]
fn local_qwen3_two_stage_compiler_produces_native_handoff() {
    let mut f = Fixture::new();
    let snapshot = PathBuf::from(std::env::var("HOME").unwrap()).join(
        ".cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots/c1899de289a04d12100db370d81485cdf75e47ca",
    );
    // Copies dereference HF links into fixture-owned regular files. Neither the
    // cache nor its blobs are written; Fixture::drop removes the copies on RED.
    for entry in fs::read_dir(&f.source.source_root).unwrap() {
        fs::remove_file(entry.unwrap().path()).unwrap();
    }
    for name in [
        "config.json",
        "model.safetensors",
        "tokenizer.json",
        "tokenizer_config.json",
    ] {
        fs::copy(snapshot.join(name), f.source.source_root.join(name)).unwrap();
    }
    let manifest =
        api::local_manifest::compile_local_model_manifest(&api::local_manifest::LocalModelSource {
            root: f.source.source_root.clone(),
            model_id: "Qwen/Qwen3-0.6B".into(),
            requested_revision: "main".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
            tokenizer_assets: Ok(Vec::new()),
        })
        .unwrap();
    f.source.manifest_digest = planner::bound::manifest_digest_ref(&manifest).unwrap();
    f.source.source_bytes = manifest["files"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["size_bytes"].as_u64().unwrap())
        .sum();
    f.source.manifest = manifest.as_object().unwrap().clone();
    f.request.model_id = "Qwen/Qwen3-0.6B".into();
    f.request.resolved_commit = "c1899de289a04d12100db370d81485cdf75e47ca".into();
    f.request.node_ids = vec!["node-0".into(), "node-1".into()];
    f.request.managed_layer_ranges = vec![[0, 14], [14, 28]];
    f.request.layer_split = json!({"stages":[
        {"node_id":"node-0","start":0,"end":14},
        {"node_id":"node-1","start":14,"end":28}
    ]});
    // Whole-source two-stage loading is supported by the existing native
    // backend; byte-shard loading is a separate currently unsupported boundary.
    f.request.stage_sharded = false;
    worker_fixture(f, true);
}

fn worker_boundary(stage_sharded: bool) {
    let mut f = Fixture::new();
    f.request.stage_sharded = stage_sharded;
    worker_fixture(f, false);
}

fn worker_fixture(f: Fixture, require_native: bool) {
    use api::installation_descriptor::{self, Member, Paths, Source};
    let stage_sharded = f.request.stage_sharded;
    let core = Arc::new(Mutex::new(load_core(f.root.join("host")).unwrap()));
    let mut host = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    host.create_owner_json(
        json!({"storage_bytes":4_000_000_000u64})
            .as_object()
            .unwrap(),
    )
    .unwrap();
    storage_policy(&f.root.join("host"), 4_000_000_000);
    let seed = core.lock().unwrap().seed.clone().unwrap();
    let run = "a13-b2-qwen-20261029T100000Z";
    let deployment = &f.request.deployment_id;
    let ttl = BudgetId::ControlTtl.span(Entries::ZERO).unwrap().millis() as f64 / 1000.0;
    let mut facts = BTreeMap::new();
    let mut members = BTreeMap::new();
    let mut keys = Vec::new();
    for (i, node) in f.request.node_ids.iter().enumerate() {
        let signer =
            Identity::from_secret(&format!("endpoint-{node}"), &[i as u8 + 70; 32]).unwrap();
        let nonce = format!("b2-invite-{i}");
        let bundle = seed.coordinator.mint_invite(&nonce, ttl as u64).unwrap();
        let at = now();
        let message = json!({"protocol":"mycelium.membership.join_request.v1",
            "message_id":format!("b2-join-{i}"),"swarm_id":seed.coordinator.swarm_id(),
            "sender_node_id":node,"sender_endpoint_id":signer.endpoint_id(),
            "recipient_node_id":seed.coordinator.seed_node_id(),"generation":0,
            "incarnation":format!("b2-inc-{i}"),"invite_nonce":nonce,
            "issued_at":at,"expires_at":at+ttl,"peer_class":"mac_mlx_iroh",
            "software_version":"0.2.0","endpoint_addr":{"id":signer.endpoint_id(),"addrs":["iroh-relay://relay.invalid/controlled"]},
            "runtime_capability":{"activation_protocol":"mycelium.router_wire.v1","runtime_backend":"mlx","transport":"iroh"}});
        let envelope =
            protocol::membership::sign(&signer, &message, seed::coordinator::limits().unwrap())
                .unwrap();
        seed.coordinator
            .accept_join_value(&bundle["token"], &envelope)
            .unwrap();
        let current = seed.coordinator.member(node).unwrap();
        let binding = Member {
            node_id: node.clone(),
            endpoint_id: signer.endpoint_id().into(),
            verification_key_digest: signer.verification_key_digest(),
            generation: current["generation"].as_u64().unwrap(),
            incarnation: current["incarnation"].as_str().unwrap().into(),
        };
        let card = installation_descriptor::installation_descriptor(
            &signer,
            &binding,
            seed.coordinator.swarm_id(),
            run,
            deployment,
            true,
            &Source::Whole {
                source_root: f.source.source_root.display().to_string(),
                manifest_digest: f.source.manifest_digest.clone(),
            },
            &Paths {
                state_root: f.root.display().to_string(),
                endpoint_secret_file: f.root.join("key").display().to_string(),
                python_executable: "/controlled/host".into(),
                sidecar_binary: "/controlled/sidecar".into(),
            },
            at,
        )
        .unwrap();
        facts.insert(
            node.clone(),
            installation_descriptor::verify_installation_descriptor(
                &card,
                &current,
                seed.coordinator.swarm_id(),
                run,
                deployment,
                at,
            )
            .unwrap(),
        );
        keys.push(signer.public_key_record());
        members.insert(node.clone(), current);
    }
    let mut hellos = ControlledHellos {
        run: run.into(),
        deployment: deployment.clone(),
        nodes: f.request.node_ids.clone(),
    };
    let observations = runtime::observe::collect_hello_observations(
        &mut hellos,
        run,
        deployment,
        &f.request.node_ids,
        &mut || 1793268000.0,
        &mut || "1234567890abcdef".into(),
    )
    .unwrap();
    let inputs = CompileInputs {
        facts,
        observation_hosts: Some(
            observations
                .observations
                .iter()
                .map(|o| o.host_id.clone())
                .collect(),
        ),
        local: None,
        joined_reports: json!({}),
        now: now(),
        owner_ready: true,
        already_started: false,
        resource_denied: false,
        model: ModelContract {
            model_id: f.request.model_id.clone(),
            resolved_commit: f.request.resolved_commit.clone(),
            source_representation: "bfloat16-safetensors".into(),
            runtime_dtype: "float32".into(),
            runtime_quantization: "int8-weight-only".into(),
        },
        manifest_digest: f.source.manifest_digest.clone(),
        manifest: Value::Object(f.source.manifest.clone()),
        source_bytes: Some(f.source.source_bytes),
        context_members: members.values().cloned().collect(),
        current_members: members.clone(),
        keys: json!({"gossip":[seed.coordinator.signer().public_key_record()],"load_proof":keys}),
        epoch: 0,
    };
    let observation = Observation(Arc::new(Mutex::new(observation::adapter(Arc::downgrade(
        &core,
    )))));
    let mut compiler = Compiler::new(Arc::downgrade(&core), observation);
    // Enter the same retained-input boundary as acquire, without changing Core
    // or Compiler fields. Acquisition/consent and physical hello are not claimed.
    compiler
        .retain_prepared_source(f.source.clone(), &inputs, &observations)
        .unwrap();
    compiler.start_build(&f.request);
    let deadline = std::time::Instant::now()
        + std::time::Duration::from_millis(
            BudgetId::ControlTtl.span(Entries::ZERO).unwrap().millis(),
        );
    let result = loop {
        match compiler.poll_build() {
            BuildPoll::Pending => {
                assert!(std::time::Instant::now() < deadline);
                std::thread::yield_now();
            }
            BuildPoll::Done(result) => break result,
        }
    };
    let output = f.root.join("host/chain-runs").join(run);
    if require_native {
        // Close the real seed before the expected RED assertion, not only on
        // GREEN. No fixture-owned model copies survive this assertion's unwind.
        compiler.teardown().unwrap();
        seed.close();
        let built = result.expect("real Qwen3 source must reach native BuildOutput");
        let mut pins = api::compiled_handoff::Pins::new();
        for name in [
            "compile-start.private.json",
            "compile-complete.private.json",
            "template.json",
            "topology.json",
            "observations.json",
            "layer-split.json",
            "compiled/operator-plan.json",
            "compiled/build-report.json",
            "compiled/local-challenge-checkpoint.json",
            "compiled/compilation-cache.json",
        ] {
            use sha2::{Digest, Sha256};
            let bytes = fs::read(output.join(name)).unwrap();
            pins.insert(
                name.into(),
                api::compiled_handoff::Pin {
                    bytes: bytes.len() as u64,
                    sha256: format!("{:x}", Sha256::digest(&bytes)),
                },
            );
        }
        let handoff = api::compiled_handoff::CompiledHandoff::open(&output, &pins).unwrap();
        handoff.revalidate().unwrap();
        assert_eq!(handoff.nodes().len(), f.request.node_ids.len());
        assert_eq!(
            protocol::canonical::digest(handoff.plan()).unwrap(),
            built.operator_plan_digest
        );
        return;
    }
    assert_eq!(
        result.unwrap_err().code_or("missing"),
        "native_challenge_producer_unavailable"
    );
    let name = if stage_sharded {
        "source-shards.private.json"
    } else {
        "source-assignments.private.json"
    };
    let document: Value = serde_json::from_slice(&fs::read(output.join(name)).unwrap()).unwrap();
    assert_eq!(
        document["state"],
        if stage_sharded {
            "bytes_only"
        } else {
            "metadata_only"
        }
    );
    assert_eq!(document["route_ready"], false);
    if stage_sharded {
        assert_eq!(
            document["node_files"].as_object().unwrap().len(),
            f.request.node_ids.len()
        );
        for rows in document["node_files"].as_object().unwrap().values() {
            for row in rows.as_array().unwrap() {
                let path = row["path"]
                    .as_str()
                    .unwrap()
                    .strip_prefix("deployment/")
                    .unwrap();
                let bytes = fs::read(output.join("stage-shards").join(path)).unwrap();
                assert_eq!(row["size_bytes"], bytes.len());
                assert_eq!(
                    row["content_digest"],
                    protocol::artifact_grant::range_digest(&bytes)
                );
            }
        }
    } else {
        for assignment in document["assignments"].as_array().unwrap() {
            planner::compile::validate_assignment(assignment).unwrap();
            assert_eq!(assignment["manifest_digest"], f.source.manifest_digest);
        }
    }
    assert_eq!(
        document["members_digest"],
        planner::digest(
            &json!({"members":members.values().cloned().collect::<Vec<_>>()}),
            true
        )
        .unwrap()
    );
    assert!(!output.join("compile-complete.private.json").exists());
    assert!(!output.join("compiled/operator-plan.json").exists());
    assert_eq!(status(&core).to_value()["route_ready"], false);
    compiler.teardown().unwrap();
    seed.close();
}
