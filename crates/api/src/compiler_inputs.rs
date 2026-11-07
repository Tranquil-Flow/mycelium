// SPDX-License-Identifier: AGPL-3.0-only
//! The model-free output boundary of `chain_compile.start_compile` (f4e366e9).
//! These are real private compiler INPUT files, never an operator plan or
//! evidence of native execution. The owning artifact retains their identities.
use crate::{preferences::private_state, serving_source::Prepared};
use owner_run::chain::PortError;
use planner::chain_compile::{BuildRequest, MAX_HOSTS, MIN_HOSTS};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    os::unix::fs::{DirBuilderExt, MetadataExt},
    path::{Path, PathBuf},
};

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Error(pub &'static str);
impl From<Error> for PortError {
    fn from(error: Error) -> Self {
        Self::Failed {
            code: Some(error.0.into()),
            text: String::new(),
        }
    }
}
type Result<T> = std::result::Result<T, Error>;
fn state(error: private_state::StateError) -> Error {
    Error(error.0)
}

pub fn validate_hosts(document: &Value, facts: usize) -> Result<()> {
    let rows = document["observations"]
        .as_array()
        .ok_or(Error("two_distinct_hosts_required"))?;
    if rows.len() > MAX_HOSTS || facts > MAX_HOSTS {
        return Err(Error("host_count_exceeds_limit"));
    }
    let hosts: BTreeSet<_> = rows.iter().filter_map(|r| r["host_id"].as_str()).collect();
    if rows.len() < MIN_HOSTS || rows.len() != facts || hosts.len() != rows.len() {
        return Err(Error("two_distinct_hosts_required"));
    }
    Ok(())
}

/// Exact sorted projection of authenticated installation facts. The activation
/// endpoint is the decoded key, not the member's protocol endpoint label.
pub fn topology(facts: &BTreeMap<String, Value>) -> Result<Value> {
    let fields = [
        "node_id",
        "process_transport",
        "ssh_target",
        "ssh_identity_file",
        "staging_root",
        "python_executable",
        "sidecar_binary",
        "endpoint_secret_file",
        "endpoint_id",
        "membership_generation",
        "runtime_backend",
    ];
    let mut nodes = Vec::new();
    for (id, fact) in facts {
        if fact["node_id"] != *id {
            return Err(Error("compiler_member_changed"));
        }
        let mut row = Map::new();
        for field in fields {
            row.insert(
                field.into(),
                fact.get(field)
                    .ok_or(Error("installation_descriptor_facts_invalid"))?
                    .clone(),
            );
        }
        row.insert(
            "endpoint_id".into(),
            fact.get("activation_endpoint_id")
                .ok_or(Error("installation_descriptor_facts_invalid"))?
                .clone(),
        );
        nodes.push(Value::Object(row));
    }
    Ok(json!({"protocol":"mycelium.qwen_live_topology.v1", "nodes":nodes}))
}

fn directory(path: &Path) -> Result<(u64, u64)> {
    let meta = match fs::DirBuilder::new().mode(0o700).create(path) {
        Ok(()) => fs::symlink_metadata(path),
        // Existing directories must pass the same ownership/mode/identity checks.
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => fs::symlink_metadata(path),
        Err(e) => Err(e),
    }
    .map_err(|_| Error("compiler_directory_failed"))?;
    if !meta.is_dir()
        || meta.file_type().is_symlink()
        || meta.mode() & 0o7777 != 0o700
        || meta.uid() != rustix::process::getuid().as_raw()
    {
        return Err(Error("compiler_directory_invalid"));
    }
    Ok((meta.dev(), meta.ino()))
}
fn segment(value: &str) -> bool {
    !value.is_empty()
        && !matches!(value, "." | "..")
        && value
            .bytes()
            .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
}

/// Consumed by the future native assignment producer. Deliberately has no
/// conversion to `BuildOutput`, `Controller`, `Launch`, or a ready bit.
#[derive(Debug)]
pub struct InputArtifact {
    root: PathBuf,
    directory_identity: (u64, u64),
    documents: BTreeMap<String, Value>,
    source: Prepared,
    request: BuildRequest,
}
impl InputArtifact {
    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn source(&self) -> &Prepared {
        &self.source
    }
    pub fn request(&self) -> &BuildRequest {
        &self.request
    }
    pub fn topology(&self) -> &Value {
        &self.documents["topology.json"]
    }
    pub fn observations(&self) -> &Value {
        &self.documents["observations.json"]
    }
    pub fn verification_keys(&self) -> &Value {
        &self.documents["verification-keys.private.json"]
    }
    pub fn membership(&self) -> &Value {
        &self.documents["members.private.json"]
    }
    /// Detect a replacement or tampered file before the next producer consumes
    /// it. Never reopen by public digest alone or reconstitute successful state.
    pub fn validate(&self) -> Result<()> {
        let metadata =
            fs::symlink_metadata(&self.root).map_err(|_| Error("compiler_inputs_changed"))?;
        if !metadata.is_dir() || (metadata.dev(), metadata.ino()) != self.directory_identity {
            return Err(Error("compiler_inputs_changed"));
        }
        let max = private_state::private_json_limit().map_err(state)?;
        for (name, expected) in &self.documents {
            let actual = private_state::load(&self.root.join(name), max).map_err(state)?;
            if actual.map(Value::Object).as_ref() != Some(expected) {
                return Err(Error("compiler_inputs_changed"));
            }
        }
        Ok(())
    }
}

/// Emits the source-bound prefix of the actual builder's input files. All
/// public identities must agree before the first write. `template.json` and
/// operator/native artifacts are not claimed: their producer is not present.
#[allow(clippy::too_many_arguments)]
pub fn emit(
    root: &Path,
    run: &str,
    deployment: &str,
    epoch: u64,
    request: &BuildRequest,
    source: &Prepared,
    topology: &Value,
    observations: &Value,
    keys: &Value,
    members: &[Value],
) -> Result<InputArtifact> {
    if !segment(run) || !segment(deployment) {
        return Err(Error("managed_control_identifier_invalid"));
    }
    if observations["run_id"] != run
        || observations["deployment_id"] != deployment
        || request.deployment_id != deployment
    {
        return Err(Error("compiler_observation_changed"));
    }
    validate_hosts(observations, request.node_ids.len())?;
    let nodes = topology["nodes"]
        .as_array()
        .ok_or(Error("compiler_topology_invalid"))?;
    let topology_ids: Vec<_> = nodes.iter().map(|n| n["node_id"].as_str()).collect();
    let request_ids: Vec<_> = request.node_ids.iter().map(|n| Some(n.as_str())).collect();
    let observed_ids: BTreeSet<_> = observations["observations"]
        .as_array()
        .ok_or(Error("compiler_observation_changed"))?
        .iter()
        .filter_map(|n| n["node_id"].as_str())
        .collect();
    if topology["protocol"] != "mycelium.qwen_live_topology.v1"
        || topology_ids != request_ids
        || observed_ids != request.node_ids.iter().map(String::as_str).collect()
        || source.manifest["manifest_digest"]["algorithm"] != "sha256"
        || source.manifest["manifest_digest"]["value"]
            .as_str()
            .map(|v| format!("sha256:{v}"))
            .as_deref()
            != Some(source.manifest_digest.as_str())
    {
        return Err(Error("compiler_inputs_changed"));
    }
    let canonical_root = fs::canonicalize(root).map_err(|_| Error("compiler_directory_invalid"))?;
    if canonical_root != root {
        return Err(Error("compiler_directory_invalid"));
    }
    // Do not silently adopt a symlink or an output belonging to an earlier run.
    let runs = root.join("chain-runs");
    directory(&runs)?;
    let output = runs.join(run);
    let directory_identity = directory(&output)?;
    for name in [
        "compile-complete.private.json",
        "compile-start.private.json",
    ] {
        match fs::symlink_metadata(output.join(name)) {
            Ok(_) => return Err(Error("compiler_run_reused")),
            // An absent sentinel permits checking the next name; other IO errors refuse.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err(Error("compiler_directory_invalid")),
        }
    }
    let documents = BTreeMap::from([
        (
            "compile-start.private.json".into(),
            json!({"protocol":"mycelium.chain_compile_start.v1","run_id":run,"deployment_id":deployment,"epoch":epoch}),
        ),
        ("topology.json".into(), topology.clone()),
        ("observations.json".into(), observations.clone()),
        ("layer-split.json".into(), request.layer_split.clone()),
        (
            "source-manifest.private.json".into(),
            Value::Object(source.manifest.clone()),
        ),
        ("verification-keys.private.json".into(), keys.clone()),
        ("members.private.json".into(), json!({"members":members})),
    ]);
    store_documents(&output, &documents)?;
    let artifact = InputArtifact {
        root: output,
        directory_identity,
        documents,
        source: source.clone(),
        request: request.clone(),
    };
    artifact.validate()?;
    Ok(artifact)
}

fn store_documents(root: &Path, documents: &BTreeMap<String, Value>) -> Result<()> {
    let max = private_state::private_json_limit().map_err(state)?;
    for (name, value) in documents {
        private_state::store(&root.join(name), value, max).map_err(state)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::fs::PermissionsExt;

    /// Real private file emission with inert storage documents, not a
    /// successful model, plan, host observation or native proof fixture.
    #[test]
    fn real_private_input_writer_roundtrip_and_refusal() {
        let root =
            std::env::temp_dir().join(format!("compiler-input-storage-{}", std::process::id()));
        assert!(!root.exists());
        directory(&root).unwrap();
        let documents = BTreeMap::from([(
            "storage-test.private.json".into(),
            json!({"kind":"inert-storage-test"}),
        )]);
        store_documents(&root, &documents).unwrap();
        let path = root.join("storage-test.private.json");
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        assert_eq!(
            private_state::load(&path, private_state::private_json_limit().unwrap())
                .unwrap()
                .map(Value::Object),
            Some(documents["storage-test.private.json"].clone())
        );
        fs::write(&path, b"tampered").unwrap();
        assert!(private_state::load(&path, private_state::private_json_limit().unwrap()).is_err());
        assert!(store_documents(&root.join("absent"), &documents).is_err());
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn directory_refuses_foreign_symlink_without_mutation() {
        let root =
            std::env::temp_dir().join(format!("compiler-input-symlink-{}", std::process::id()));
        let link = root.with_extension("link");
        assert!(!root.exists() && !link.exists());
        directory(&root).unwrap();
        std::os::unix::fs::symlink(&root, &link).unwrap();
        assert_eq!(directory(&link), Err(Error("compiler_directory_invalid")));
        assert!(root.is_dir());
        fs::remove_file(link).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn absent_observations_do_not_become_compiler_inputs() {
        assert_eq!(
            validate_hosts(&json!({}), 2),
            Err(Error("two_distinct_hosts_required"))
        );
        assert_eq!(
            validate_hosts(&json!({"observations":[]}), 2),
            Err(Error("two_distinct_hosts_required"))
        );
        assert_eq!(
            validate_hosts(&json!({"observations":[]}), MAX_HOSTS + 1),
            Err(Error("host_count_exceeds_limit"))
        );
    }
}
