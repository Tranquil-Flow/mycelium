// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded source-artifact tests. Controlled member/observation documents are
//! not authenticated hello sessions, native model execution, or BuildOutput.
use super::*;
use api::compiler_inputs::{self, InputArtifact};
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);

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
        compiler_inputs::emit(&self.root, "source-boundary", &self.request.deployment_id, 0,
            &self.request, &self.source,
            &json!({"protocol":"mycelium.qwen_live_topology.v1","nodes":nodes}),
            &json!({"run_id":"source-boundary","deployment_id":self.request.deployment_id,"observations":observations}),
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

fn worker_boundary(stage_sharded: bool) {
    use api::installation_descriptor::{self, Member, Paths, Source};
    let mut f = Fixture::new();
    f.request.stage_sharded = stage_sharded;
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
    loop {
        match compiler.poll_build() {
            BuildPoll::Pending => {
                assert!(std::time::Instant::now() < deadline);
                std::thread::yield_now();
            }
            BuildPoll::Done(Err(error)) => {
                assert_eq!(
                    error.code_or("missing"),
                    "native_challenge_producer_unavailable"
                );
                break;
            }
            BuildPoll::Done(Ok(_)) => panic!("metadata must not become BuildOutput"),
        }
    }
    let output = f.root.join("host/chain-runs").join(run);
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
