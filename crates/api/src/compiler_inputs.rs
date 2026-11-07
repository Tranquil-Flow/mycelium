// SPDX-License-Identifier: AGPL-3.0-only
//! Private compiler inputs, metadata assignments and verified stage-shard bytes.
//! None of these artifacts is an operator plan, loaded-stage or native proof.
//! The owning artifact retains their identities and never converts to BuildOutput.
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
    /// Emit source-bound metadata assignments only. This does not authorize an
    /// offer, claim a loaded stage, or produce an operator plan / BuildOutput.
    pub fn produce_assignments(
        &mut self,
        current_members: &BTreeMap<String, Value>,
        now: f64,
    ) -> Result<()> {
        let document = self.assignment_document(current_members, now)?;
        let name = "source-assignments.private.json";
        if self.documents.contains_key(name) {
            return Err(Error("compiler_already_started"));
        }
        match fs::symlink_metadata(self.root.join(name)) {
            Ok(_) => Err(Error("compiler_outputs_changed")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(Error("compiler_directory_invalid")),
        }?;
        private_state::store(
            &self.root.join(name),
            &document,
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        self.documents.insert(name.into(), document);
        self.validate()
    }
    /// Reopen the emitted document and source, recompute all metadata, and bind
    /// it to freshly read members. The caller must repeat this after async work.
    pub fn validate_assignments(
        &self,
        current_members: &BTreeMap<String, Value>,
        now: f64,
    ) -> Result<()> {
        let expected = self.assignment_document(current_members, now)?;
        if self.documents.get("source-assignments.private.json") != Some(&expected) {
            return Err(Error("compiler_outputs_changed"));
        }
        Ok(())
    }
    fn assignment_document(
        &self,
        current_members: &BTreeMap<String, Value>,
        now: f64,
    ) -> Result<Value> {
        self.validate_members(current_members, now)?;
        let (manifest, files) = self.source_files()?;
        let mut roots = Map::new();
        let mut runtimes = Map::new();
        for node in self.topology()["nodes"]
            .as_array()
            .ok_or(Error("compiler_topology_invalid"))?
        {
            let id = node["node_id"]
                .as_str()
                .ok_or(Error("compiler_topology_invalid"))?;
            let root = node["staging_root"]
                .as_str()
                .ok_or(Error("compiler_topology_invalid"))?;
            roots.insert(id.into(), json!(format!("{root}/deployment")));
            runtimes.insert(
                id.into(),
                json!({"backend":node["runtime_backend"],
                "dtype":"float32","quantization":"int8-weight-only"}),
            );
        }
        // The host rechecks exact current members separately; a cloned request
        // is not presented as fresh planner/resource/consent admission.
        let output = planner::source_plan::producer::compile_source_assignments(
            &self.request,
            &self.request,
            planner::source_plan::PreparedSource {
                manifest: &manifest,
                manifest_digest: &self.source.manifest_digest,
                files: &files,
            },
            self.documents["compile-start.private.json"]["epoch"]
                .as_u64()
                .ok_or(Error("compiler_inputs_changed"))?,
            &Value::Object(roots),
            &Value::Object(runtimes),
        )
        .map_err(|e| Error(e.code))?;
        Ok(json!({"protocol":"mycelium.source_assignments.v1",
            "state":"metadata_only", "route_ready":false,
            "run_id":self.documents["compile-start.private.json"]["run_id"],
            "deployment_id":self.request.deployment_id,
            "compilation_cache_key":self.request.compilation_cache_key,
            "observations_digest":planner::digest(self.observations(), true).map_err(|e| Error(e.code))?,
            "members_digest":planner::digest(self.membership(), true).map_err(|e| Error(e.code))?,
            "route":output.route,"assignments":output.assignments}))
    }
    fn source_files(&self) -> Result<(Value, BTreeMap<String, fs::File>)> {
        self.validate()?;
        let manifest = crate::local_manifest::compile_local_model_manifest(
            &crate::local_manifest::LocalModelSource {
                root: self.source.source_root.clone(),
                model_id: self.request.model_id.clone(),
                requested_revision: self.source.manifest["requested_revision"]
                    .as_str()
                    .ok_or(Error("compiler_model_changed"))?
                    .into(),
                resolved_commit: self.request.resolved_commit.clone(),
                tokenizer_assets: Ok(Vec::new()),
            },
        )
        .map_err(|_| Error("compiler_model_changed"))?;
        if manifest != Value::Object(self.source.manifest.clone()) {
            return Err(Error("compiler_model_changed"));
        }
        let mut files = BTreeMap::new();
        for row in manifest["files"]
            .as_array()
            .ok_or(Error("compiler_model_changed"))?
        {
            let name = row["path"]
                .as_str()
                .ok_or(Error("compiler_source_path_invalid"))?;
            files.insert(
                name.into(),
                open_source_file(&self.source.source_root, name)?,
            );
        }
        Ok((manifest, files))
    }

    /// Real source-byte sharding only; no stage load, quantization or native proof.
    /// Requires an explicitly stage-sharded request (whole-source requests do not
    /// silently duplicate a model). Partial failures leave unadmitted bytes.
    pub fn produce_stage_shards(
        &mut self,
        current: &BTreeMap<String, Value>,
        now: f64,
    ) -> Result<()> {
        self.validate_members(current, now)?;
        let (manifest, files) = self.source_files()?;
        let name = "source-shards.private.json";
        if self.documents.contains_key(name) {
            return Err(Error("compiler_already_started"));
        }
        match fs::symlink_metadata(self.root.join(name)) {
            Ok(_) => Err(Error("compiler_outputs_changed")),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(_) => Err(Error("compiler_directory_invalid")),
        }?;
        let produced = planner::source_plan::producer::emit_stage_shards(
            &self.request,
            &self.request,
            planner::source_plan::PreparedSource {
                manifest: &manifest,
                manifest_digest: &self.source.manifest_digest,
                files: &files,
            },
            &self.root.join("stage-shards"),
        )
        .map_err(|e| Error(e.code))?;
        let document = json!({"protocol":"mycelium.source_stage_shards.v1",
            "state":"bytes_only","route_ready":false,
            "run_id":self.documents["compile-start.private.json"]["run_id"],
            "deployment_id":self.request.deployment_id,
            "compilation_cache_key":self.request.compilation_cache_key,
            "manifest_digest":self.source.manifest_digest,
            "observations_digest":planner::digest(self.observations(),true).map_err(|e|Error(e.code))?,
            "members_digest":planner::digest(self.membership(),true).map_err(|e|Error(e.code))?,
            "node_files":produced.node_files,"node_files_digest":produced.node_files_digest,
            "slice_plan":produced.slices.plan,"node_slice_plans":produced.slices.node_plans});
        private_state::store(
            &self.root.join(name),
            &document,
            private_state::private_json_limit().map_err(state)?,
        )
        .map_err(state)?;
        self.documents.insert(name.into(), document);
        self.validate_stage_shards(current, now)
    }

    /// Reopen emitted bytes, rehash the prepared source, and independently
    /// recompile slice recipes against retained pins and current membership.
    pub fn validate_stage_shards(&self, current: &BTreeMap<String, Value>, now: f64) -> Result<()> {
        self.validate_members(current, now)?;
        let (manifest, source_files) = self.source_files()?;
        let document = self
            .documents
            .get("source-shards.private.json")
            .ok_or(Error("compiler_outputs_changed"))?;
        let node_files: BTreeMap<String, Vec<Value>> =
            serde_json::from_value(document["node_files"].clone())
                .map_err(|_| Error("compiler_outputs_changed"))?;
        let mut files = BTreeMap::new();
        for rows in node_files.values() {
            for row in rows {
                let name = row["path"]
                    .as_str()
                    .and_then(|s| s.strip_prefix("deployment/"))
                    .filter(|s| !s.contains('/'))
                    .ok_or(Error("compiler_outputs_changed"))?;
                files.insert(
                    name.into(),
                    open_source_file(&self.root, &format!("stage-shards/{name}"))?,
                );
            }
        }
        let slices = planner::source_plan::compile_slices(
            &self.request,
            &self.request,
            planner::source_plan::PreparedSource {
                manifest: &manifest,
                manifest_digest: &self.source.manifest_digest,
                files: &source_files,
            },
            planner::source_plan::EmittedShards {
                files: &files,
                node_files: &node_files,
                node_files_digest: document["node_files_digest"]
                    .as_str()
                    .ok_or(Error("compiler_outputs_changed"))?,
                max_segment_bytes:
                    budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
                        .bytes()
                        .map_err(|_| Error("budget_unavailable"))?
                        .get(),
            },
        )
        .map_err(|e| Error(e.code))?;
        if slices.plan != document["slice_plan"]
            || json!(slices.node_plans) != document["node_slice_plans"]
        {
            return Err(Error("compiler_outputs_changed"));
        }
        Ok(())
    }
    /// Cheap post-worker identity/lease check; not a consent or resource grant.
    pub fn validate_members(&self, current: &BTreeMap<String, Value>, now: f64) -> Result<()> {
        let bad = Error("compiler_member_changed");
        let members = self.membership()["members"].as_array().ok_or(bad.clone())?;
        if !now.is_finite()
            || members.len() != self.request.node_ids.len()
            || current.keys().collect::<Vec<_>>()
                != self.request.node_ids.iter().collect::<Vec<_>>()
        {
            return Err(bad);
        }
        let mut seen = BTreeSet::new();
        for prior in members {
            let id = prior["node_id"].as_str().ok_or(bad.clone())?;
            let member = current.get(id).ok_or(bad.clone())?;
            if !seen.insert(id)
                || member["node_id"] != id
                || member["lease_expires_at"]
                    .as_f64()
                    .is_none_or(|end| !end.is_finite() || now >= end)
                || [
                    "generation",
                    "incarnation",
                    "verification_key_digest",
                    "endpoint_id",
                ]
                .iter()
                .any(|key| prior[*key].is_null() || prior[*key] != member[*key])
            {
                return Err(bad);
            }
        }
        Ok(())
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
            let actual = private_state::load(&self.root.join(name), max)
                .map_err(state)?
                .map(Value::Object)
                .ok_or(Error("compiler_inputs_changed"))?;
            // The private writer canonicalizes numeric spellings (e.g. a real
            // Qwen RMS epsilon 1e-6 becomes 1e-06). Compare that same wire form,
            // not serde's arbitrary-precision number spelling.
            let canonical = |value: &Value| {
                protocol::canonical::to_bytes(value).map_err(|_| Error("compiler_inputs_changed"))
            };
            if canonical(&actual)? != canonical(expected)? {
                return Err(Error("compiler_inputs_changed"));
            }
        }
        Ok(())
    }
}

/// Whole-source producer currently accepts regular snapshot files only. HF blob
/// symlinks remain explicitly refused here; manifest compilation accepting one
/// is not permission to follow an arbitrary link while reopening it.
fn open_source_file(root: &Path, name: &str) -> Result<fs::File> {
    use std::{os::unix::fs::OpenOptionsExt, path::Component};
    let bad = Error("compiler_source_path_invalid");
    if fs::canonicalize(root).map_err(|_| bad.clone())? != root {
        return Err(bad);
    }
    let suffix = Path::new(name);
    if name.is_empty()
        || suffix
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(bad);
    }
    let mut path = root.to_owned();
    for part in suffix.components() {
        path.push(part);
        if fs::symlink_metadata(&path)
            .map_err(|_| bad.clone())?
            .file_type()
            .is_symlink()
        {
            return Err(bad);
        }
    }
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| bad.clone())?;
    if !file.metadata().map_err(|_| bad.clone())?.is_file() {
        return Err(bad);
    }
    Ok(file)
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
