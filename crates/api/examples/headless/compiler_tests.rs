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
