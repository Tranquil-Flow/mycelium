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
/// Diagnostic lines this module wrote to the log, kept for the tests that check the log names
/// what it should (lane A, B11 (a)).
#[cfg(test)]
pub(crate) static DIAGNOSTICS: std::sync::Mutex<Vec<String>> = std::sync::Mutex::new(Vec::new());
/// Lane A, B11 (a) (owner ruling 4 a, ledger row 77): a private state file this module cannot
/// read is NAMED in the log beside its closed code (`authority_state_invalid:<path>`); the code
/// the run reports stays the closed one. Row 77's indented ledger said only "authority state
/// invalid", and finding which file took a whole session.
fn named_state(path: &Path, error: private_state::StateError) -> Error {
    let line = format!("{}:{}", error.0, path.display());
    eprintln!("{line}");
    #[cfg(test)]
    DIAGNOSTICS
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .push(line);
    state(error)
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

fn validate_loopback_processes(document: &Value, facts: usize) -> Result<()> {
    let rows = document["observations"]
        .as_array()
        .ok_or(Error("loopback_process_observation_invalid"))?;
    let pids: BTreeSet<_> = rows
        .iter()
        .filter_map(|r| r["process_id"].as_u64().filter(|p| *p > 0))
        .collect();
    if document["scope"] != "loopback_process_qualification"
        || document["route_ready"] != false
        || rows.len() < MIN_HOSTS
        || rows.len() > MAX_HOSTS
        || rows.len() != facts
        || pids.len() != rows.len()
    {
        return Err(Error("loopback_process_observation_invalid"));
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
    cancel: Option<std::sync::Arc<std::sync::atomic::AtomicBool>>,
    /// The run directory, exclusively locked while this artifact lives: a retirement pass
    /// ([`retire_dead_runs`]) never touches a run whose custody is held (lane PHY item 1).
    _custody: fs::File,
}
impl InputArtifact {
    pub fn bind_cancellation(
        &mut self,
        token: std::sync::Arc<std::sync::atomic::AtomicBool>,
    ) -> Result<()> {
        if self.cancel.is_some() {
            return Err(Error("compiler_already_started"));
        }
        self.cancel = Some(token);
        self.checkpoint()
    }
    fn checkpoint(&self) -> Result<()> {
        if self
            .cancel
            .as_ref()
            .is_some_and(|c| c.load(std::sync::atomic::Ordering::SeqCst))
        {
            Err(Error("compiler_stopped"))
        } else {
            Ok(())
        }
    }
    pub fn root(&self) -> &Path {
        &self.root
    }
    pub fn source(&self) -> &Prepared {
        &self.source
    }
    pub fn request(&self) -> &BuildRequest {
        &self.request
    }
    /// Borrow the retained source-shard document after validating its bound on-disk identity.
    pub fn stage_shards(&self) -> Result<&Value> {
        self.validate()?;
        self.documents
            .get("source-shards.private.json")
            .ok_or(Error("compiler_outputs_changed"))
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
        // Shard compilation retains the same source-bound assignment identity.
        // This projection changes no source facts and does not itself emit bytes.
        let mut whole = self.request.clone();
        whole.stage_sharded = false;
        let output = planner::source_plan::producer::compile_source_assignments(
            &whole,
            &whole,
            planner::source_plan::PreparedSource {
                manifest: &manifest,
                manifest_digest: &self.source.manifest_digest,
                files: &files,
                checkpoint: &|| self.checkpoint().map_err(|e| planner::Error::new(e.0)),
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
        let manifest = crate::local_manifest::compile_local_model_manifest_checked(
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
            &|| {
                self.checkpoint()
                    .map_err(|e| crate::local_manifest::CompileError(e.0.into()))
            },
        )
        .map_err(|e| {
            if e.0 == "compiler_stopped" {
                Error("compiler_stopped")
            } else {
                Error("compiler_model_changed")
            }
        })?;
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

    /// Durable coordinator-local reservation. Dropping it does NOT release the
    /// charge: interrupted writes and even reservation-before-crash remain owed.
    pub fn reserve_stage_output(&self, bytes: u64) -> Result<ShardReservation> {
        self.validate()?;
        let runs = self
            .root
            .parent()
            .ok_or(Error("compiler_storage_ledger_invalid"))?;
        let run = self
            .root
            .file_name()
            .and_then(|s| s.to_str())
            .ok_or(Error("compiler_storage_ledger_invalid"))?;
        reserve_output(runs, run, self.directory_identity, bytes)
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
        let mut reservation = None;
        let produced = planner::source_plan::producer::emit_stage_shards(
            &self.request,
            &self.request,
            planner::source_plan::PreparedSource {
                manifest: &manifest,
                manifest_digest: &self.source.manifest_digest,
                files: &files,
                checkpoint: &|| self.checkpoint().map_err(|e| planner::Error::new(e.0)),
            },
            &self.root.join("stage-shards"),
            &mut |bytes| {
                reservation = Some(
                    self.reserve_stage_output(bytes)
                        .map_err(|e| planner::Error::new(e.0))?,
                );
                Ok(())
            },
        )
        .map_err(|e| Error(e.code))?;
        reservation
            .ok_or(Error("compiler_storage_reservation_missing"))?
            .complete()?;
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
                checkpoint: &|| self.checkpoint().map_err(|e| planner::Error::new(e.0)),
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
        self.checkpoint()?;
        let metadata =
            fs::symlink_metadata(&self.root).map_err(|_| Error("compiler_inputs_changed"))?;
        if !metadata.is_dir() || (metadata.dev(), metadata.ino()) != self.directory_identity {
            return Err(Error("compiler_inputs_changed"));
        }
        let max = private_state::private_json_limit().map_err(state)?;
        for (name, expected) in &self.documents {
            let path = self.root.join(name);
            let actual = private_state::load(&path, max)
                .map_err(|e| named_state(&path, e))?
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

/// Member byte custody reuses the compiler ledger, without source or proof fabrication.
pub mod member_shard;
/// The owner retires the ledger entries and stage shards of its own dead compile runs.
pub mod owner_retire;
pub use owner_retire::{Retirement, retire_dead_runs};

fn reserve_output(
    runs: &Path,
    run: &str,
    directory_identity: (u64, u64),
    bytes: u64,
) -> Result<ShardReservation> {
    let host = runs
        .parent()
        .ok_or(Error("compiler_storage_ledger_invalid"))?;
    let (_lock, mut ledger) = storage_ledger(runs)?;
    let cap = crate::preferences::resource_policy::read(host)
        .map_err(|_| Error("compiler_storage_consent_required"))?
        .and_then(|p| p["storage_bytes"].as_u64())
        .filter(|n| *n > 0)
        .ok_or(Error("compiler_storage_consent_required"))?;
    let cap = match crate::preferences::standing_consent::load(host)
        .map_err(|_| Error("compiler_storage_consent_required"))?
    {
        Some(grant) => {
            if grant["state"] != "granted" {
                return Err(Error("compiler_storage_consent_required"));
            }
            cap.min(
                grant["caps"]["storage_bytes"]
                    .as_u64()
                    .ok_or(Error("compiler_storage_consent_required"))?,
            )
        }
        None => cap, // Owner preparation is bounded by its explicit resource policy.
    };
    let entries = ledger["entries"]
        .as_object_mut()
        .ok_or(Error("compiler_storage_ledger_invalid"))?;
    let (retained, materialized) = storage_inventory(runs, entries)?;
    let total = retained
        .checked_add(bytes)
        .ok_or(Error("compiler_storage_quota_exceeded"))?;
    if bytes == 0 || entries.len() >= MAX_RETAINED_SHARD_RUNS || total > cap {
        return Err(Error("compiler_storage_quota_exceeded"));
    }
    if entries.contains_key(run) {
        return Err(Error("compiler_storage_run_reserved"));
    }
    let reserve = budgets::BudgetId::PyFinalMyceliumLiveLocalPreparerArtifactDiskReserveBytesL50C0
        .bytes()
        .map_err(|_| Error("budget_unavailable"))?
        .get();
    let free = storage_free_bytes(runs)?;
    // Inventory precedes statvfs: production writers only append to new,
    // exclusively created files, so the sampled allocation is already absent
    // from free space. Retain unwritten promises, including concurrent writers.
    if total
        .checked_sub(materialized)
        .and_then(|need| need.checked_add(reserve))
        .is_none_or(|need| need > free)
    {
        return Err(Error("compiler_storage_headroom_exceeded"));
    }
    let row = json!({"bytes":bytes,"run_dev":directory_identity.0,
            "run_ino":directory_identity.1,"phase":"cleanup_required"});
    entries.insert(run.into(), row.clone());
    store_storage_ledger(runs, &ledger)?;
    Ok(ShardReservation {
        runs: runs.into(),
        run: run.into(),
        row,
    })
}

// Bounded retained output slots. Full ledgers refuse; no automatic deletion,
// adoption, refund-on-Drop, or deletion of files based on a public pathname.
const MAX_RETAINED_SHARD_RUNS: usize = 8;
const STORAGE_LEDGER: &str = "compiler-storage.private.json";
const STORAGE_LOCK: &str = "compiler-storage.lock";

#[cfg(test)]
std::thread_local! {
    static STORAGE_FREE_BYTES: std::cell::Cell<Option<u64>> = const { std::cell::Cell::new(None) };
}
fn storage_free_bytes(runs: &Path) -> Result<u64> {
    #[cfg(test)]
    if let Some(bytes) = STORAGE_FREE_BYTES.with(|free| free.get()) {
        return Ok(bytes);
    }
    crate::acquisition::disk_free_bytes(runs).map_err(|_| Error("compiler_storage_probe_failed"))
}

#[derive(Debug)]
pub struct ShardReservation {
    runs: PathBuf,
    run: String,
    row: Value,
}
impl ShardReservation {
    pub fn complete(self) -> Result<()> {
        let (_lock, mut ledger) = storage_ledger(&self.runs)?;
        let entries = ledger["entries"]
            .as_object_mut()
            .ok_or(Error("compiler_storage_ledger_invalid"))?;
        storage_inventory(&self.runs, entries)?;
        if entries.get(&self.run) != Some(&self.row) {
            return Err(Error("compiler_storage_reservation_changed"));
        }
        let actual = shard_output_bytes(&self.runs.join(&self.run).join("stage-shards"))?;
        if Some(actual) != self.row["bytes"].as_u64() {
            return Err(Error("compiler_storage_output_size_changed"));
        }
        entries
            .get_mut(&self.run)
            .ok_or(Error("compiler_storage_reservation_changed"))?["phase"] = json!("retained");
        store_storage_ledger(&self.runs, &ledger)
    }
}
fn shard_output_bytes(path: &Path) -> Result<u64> {
    shard_output_usage(path).map(|(bytes, _)| bytes)
}
fn shard_output_usage(path: &Path) -> Result<(u64, u64)> {
    let directory =
        fs::symlink_metadata(path).map_err(|_| Error("compiler_storage_output_changed"))?;
    if !directory.is_dir()
        || directory.mode() & 0o7777 != 0o700
        || directory.uid() != rustix::process::getuid().as_raw()
    {
        return Err(Error("compiler_storage_output_changed"));
    }
    let mut total = 0u64;
    let mut materialized = 0u64;
    let mut count = 0;
    for entry in fs::read_dir(path).map_err(|_| Error("compiler_storage_inventory_failed"))? {
        let entry = entry.map_err(|_| Error("compiler_storage_inventory_failed"))?;
        count += 1;
        let meta = fs::symlink_metadata(entry.path())
            .map_err(|_| Error("compiler_storage_inventory_failed"))?;
        if count > MAX_HOSTS
            || !meta.is_file()
            || meta.nlink() != 1
            || meta.uid() != rustix::process::getuid().as_raw()
            || meta.mode() & 0o7777 != 0o600
        {
            return Err(Error("compiler_storage_output_changed"));
        }
        total = total
            .checked_add(meta.len())
            .ok_or(Error("compiler_storage_output_changed"))?;
        // Sparse/unallocated extents are promises, not consumed disk. Blocks are
        // POSIX 512-byte units; never credit allocation beyond the byte charge.
        let allocated = meta
            .blocks()
            .checked_mul(512)
            .ok_or(Error("compiler_storage_output_changed"))?;
        materialized = materialized
            .checked_add(meta.len().min(allocated))
            .ok_or(Error("compiler_storage_output_changed"))?;
    }
    Ok((total, materialized))
}
fn storage_rows(runs: &Path, entries: &Map<String, Value>) -> Result<u64> {
    let bad = Error("compiler_storage_ledger_invalid");
    if entries.len() > MAX_RETAINED_SHARD_RUNS {
        return Err(bad);
    }
    let mut total = 0u64;
    for (run, row) in entries {
        if !segment(run) || !matches!(row["phase"].as_str(), Some("retained" | "cleanup_required"))
        {
            return Err(bad);
        }
        let bytes = row["bytes"]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or(bad.clone())?;
        let meta = fs::symlink_metadata(runs.join(run)).map_err(|_| bad.clone())?;
        if !meta.is_dir()
            || meta.mode() & 0o7777 != 0o700
            || meta.uid() != rustix::process::getuid().as_raw()
            || Some(meta.dev()) != row["run_dev"].as_u64()
            || Some(meta.ino()) != row["run_ino"].as_u64()
        {
            return Err(Error("compiler_storage_owner_changed"));
        }
        total = total.checked_add(bytes).ok_or(bad.clone())?;
    }
    Ok(total)
}
fn storage_inventory(runs: &Path, entries: &Map<String, Value>) -> Result<(u64, u64)> {
    let total = storage_rows(runs, entries)?;
    let mut materialized = 0u64;
    let bad = Error("compiler_storage_ledger_invalid");
    // Legacy B2 partials have no reservation: refuse rather than silently adopt
    // or fail to account for them. Bound traversal even for metadata-only runs.
    for (index, entry) in fs::read_dir(runs).map_err(|_| bad.clone())?.enumerate() {
        if index >= 4096 {
            return Err(Error("compiler_storage_inventory_exceeded"));
        }
        let entry = entry.map_err(|_| bad.clone())?;
        let name = entry.file_name();
        let name = name.to_str().ok_or(bad.clone())?;
        if matches!(name, STORAGE_LEDGER | STORAGE_LOCK) {
            continue;
        }
        let meta = fs::symlink_metadata(entry.path()).map_err(|_| bad.clone())?;
        if !meta.is_dir() {
            return Err(Error("compiler_storage_owner_changed"));
        }
        let output = entry.path().join("stage-shards");
        match fs::symlink_metadata(&output) {
            Ok(_) => {
                let row = entries
                    .get(name)
                    .ok_or(Error("compiler_storage_untracked_output"))?;
                let (bytes, allocated) = shard_output_usage(&output)?;
                if bytes > row["bytes"].as_u64().ok_or(bad.clone())? {
                    return Err(Error("compiler_storage_output_size_changed"));
                }
                materialized = materialized.checked_add(allocated).ok_or(bad.clone())?;
            }
            // An unwritten reservation is still fully charged; there is no
            // materialized output to credit against free space for this run.
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(_) => return Err(Error("compiler_storage_inventory_failed")),
        }
    }
    Ok((total, materialized))
}
fn storage_ledger(runs: &Path) -> Result<(fs::File, Value)> {
    use std::os::unix::fs::OpenOptionsExt;
    directory(runs)?;
    let lock = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .custom_flags(libc::O_NOFOLLOW)
        .open(runs.join(STORAGE_LOCK))
        .map_err(|_| Error("compiler_storage_lock_failed"))?;
    let meta = lock
        .metadata()
        .map_err(|_| Error("compiler_storage_lock_failed"))?;
    if !meta.is_file()
        || meta.nlink() != 1
        || meta.mode() & 0o7777 != 0o600
        || meta.uid() != rustix::process::getuid().as_raw()
    {
        return Err(Error("compiler_storage_lock_failed"));
    }
    lock.try_lock()
        .map_err(|_| Error("compiler_storage_busy"))?;
    let path = runs.join(STORAGE_LEDGER);
    let ledger = private_state::load(&path, private_state::private_json_limit().map_err(state)?)
        .map_err(|e| named_state(&path, e))?
        .map(Value::Object)
        .unwrap_or_else(|| json!({"protocol":"mycelium.compiler_storage.v1","entries":{}}));
    if ledger["protocol"] != "mycelium.compiler_storage.v1" || !ledger["entries"].is_object() {
        return Err(Error("compiler_storage_ledger_invalid"));
    }
    reconcile_storage_temporaries(runs, ledger["entries"].as_object().unwrap())?;
    Ok((lock, ledger))
}

/// Discard only a complete private ledger replacement bound to this run owner.
/// The committed ledger remains authoritative: before reserve's rename no byte
/// producer has permission to write; before complete's rename its full charge is
/// already committed. Recovery never adopts outputs or refunds old charges.
fn reconcile_storage_temporaries(runs: &Path, committed: &Map<String, Value>) -> Result<()> {
    let bad = Error("compiler_storage_owner_changed");
    storage_rows(runs, committed)?;
    let mut stale = Vec::new();
    for (index, entry) in fs::read_dir(runs).map_err(|_| bad.clone())?.enumerate() {
        if index >= 4096 {
            return Err(Error("compiler_storage_inventory_exceeded"));
        }
        let entry = entry.map_err(|_| bad.clone())?;
        let name = entry.file_name();
        let Some(nonce) = name.to_str().and_then(|name| {
            name.strip_prefix(".compiler-storage.private.json.")?
                .strip_suffix(".tmp")
        }) else {
            continue;
        };
        if nonce.len() != 32
            || !nonce
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(bad);
        }
        let path = entry.path();
        let meta = fs::symlink_metadata(&path).map_err(|_| bad.clone())?;
        // load uses O_NOFOLLOW and checks private mode, uid, single link, bounded
        // canonical bytes and opened inode identity. A pathname prefix is NOT ownership.
        let candidate =
            private_state::load(&path, private_state::private_json_limit().map_err(state)?)
                .map_err(|_| bad.clone())?
                .ok_or(bad.clone())?;
        let rows = candidate
            .get("entries")
            .and_then(Value::as_object)
            .ok_or(bad.clone())?;
        if candidate.get("protocol").and_then(Value::as_str) != Some("mycelium.compiler_storage.v1")
            || (rows.is_empty() && committed.is_empty())
            || rows.len() > committed.len() + 1
            || committed.iter().any(|(run, row)| match rows.get(run) {
                Some(next) => ["bytes", "run_dev", "run_ino"]
                    .iter()
                    .any(|key| next[*key] != row[*key]),
                None => {
                    !member_shard::refund_matches(runs, run, row)
                        && !owner_retire::refund_matches(runs, run, row)
                }
            })
        {
            return Err(bad);
        }
        storage_rows(runs, rows).map_err(|_| bad.clone())?;
        stale.push((path, meta));
        if stale.len() > MAX_RETAINED_SHARD_RUNS {
            return Err(Error("compiler_storage_inventory_exceeded"));
        }
    }
    // Validate the complete bounded set before unlinking any of it, then recheck
    // each identity. Never follow a symlink or delete an unrelated replacement.
    for (path, prior) in &stale {
        let meta = fs::symlink_metadata(path).map_err(|_| bad.clone())?;
        if !meta.is_file()
            || meta.nlink() != 1
            || meta.mode() & 0o7777 != 0o600
            || meta.uid() != rustix::process::getuid().as_raw()
            || (meta.dev(), meta.ino(), meta.len()) != (prior.dev(), prior.ino(), prior.len())
        {
            return Err(bad);
        }
        fs::remove_file(path).map_err(|_| Error("compiler_storage_recovery_failed"))?;
    }
    if !stale.is_empty() {
        fs::File::open(runs)
            .and_then(|dir| dir.sync_all())
            .map_err(|_| Error("compiler_storage_recovery_failed"))?;
    }
    Ok(())
}
fn store_storage_ledger(runs: &Path, ledger: &Value) -> Result<()> {
    private_state::store(
        &runs.join(STORAGE_LEDGER),
        ledger,
        private_state::private_json_limit().map_err(state)?,
    )
    .map_err(state)
}

/// Whole-source producer currently accepts regular snapshot files only. HF blob
/// symlinks remain explicitly refused here; manifest compilation accepting one
/// is not permission to follow an arbitrary link while reopening it.
fn open_source_file(root: &Path, name: &str) -> Result<fs::File> {
    use std::{os::unix::fs::OpenOptionsExt, path::Component};
    let bad = Error("compiler_source_path_invalid");
    // emit retained a canonical root. This is a replacement check, not a
    // restriction on the caller's original configuration path spelling.
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
    emit_mode(
        root,
        run,
        deployment,
        epoch,
        request,
        source,
        topology,
        observations,
        keys,
        members,
        false,
    )
}

/// Explicit one-host process qualification. It never satisfies the physical-host
/// admission in `emit`; its stored prefix is versioned and route_ready is false.
#[allow(clippy::too_many_arguments)]
pub fn emit_loopback_qualification(
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
    emit_mode(
        root,
        run,
        deployment,
        epoch,
        request,
        source,
        topology,
        observations,
        keys,
        members,
        true,
    )
}
#[allow(clippy::too_many_arguments)]
fn emit_mode(
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
    loopback: bool,
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
    if loopback {
        validate_loopback_processes(observations, request.node_ids.len())?;
    } else {
        validate_hosts(observations, request.node_ids.len())?;
    }
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
    // Configuration roots may have symlinked ancestors. Resolve them once at
    // admission, before creating descendants, and retain only physical paths.
    // Never canonicalize chain-runs, run directories or source file suffixes:
    // directory/open_source_file must still refuse links BELOW these roots.
    let root = fs::canonicalize(root).map_err(|_| Error("compiler_directory_invalid"))?;
    let mut source = source.clone();
    source.source_root =
        fs::canonicalize(&source.source_root).map_err(|_| Error("compiler_source_path_invalid"))?;
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
    let custody = owner_retire::custody(&output, directory_identity)?;
    let documents = BTreeMap::from([
        (
            "compile-start.private.json".into(),
            if loopback {
                json!({"protocol":"mycelium.chain_compile_start.loopback.v1","run_id":run,"deployment_id":deployment,"epoch":epoch,"scope":"loopback_process_qualification","route_ready":false,"custody":owner_retire::CUSTODY})
            } else {
                // `custody`: this process holds the run directory's lock while it keeps the run, so
                // a retirement pass may read a free lock as a dead run (PHY-01).
                json!({"protocol":"mycelium.chain_compile_start.v1","run_id":run,"deployment_id":deployment,"epoch":epoch,"custody":owner_retire::CUSTODY})
            },
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
        cancel: None,
        _custody: custody,
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
#[path = "compiler_storage_tests.rs"]
mod storage_tests;

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
    fn loopback_process_observations_never_satisfy_physical_admission() {
        // Pure shape controls, not physical/native evidence. The model gate uses
        // the exact child PIDs observed through owned process handles.
        let doc = json!({"scope":"loopback_process_qualification","route_ready":false,
            "observations":[{"node_id":"a","process_id":1},{"node_id":"b","process_id":2}]});
        assert_eq!(validate_loopback_processes(&doc, 2), Ok(()));
        assert_eq!(
            validate_hosts(&doc, 2),
            Err(Error("two_distinct_hosts_required"))
        );
        for key in ["scope", "route_ready", "observations"] {
            let mut bad = doc.clone();
            bad.as_object_mut().unwrap().remove(key);
            assert_eq!(
                validate_loopback_processes(&bad, 2),
                Err(Error("loopback_process_observation_invalid"))
            );
        }
        let mut bad = doc.clone();
        bad["observations"][1]["process_id"] = json!(1);
        assert!(validate_loopback_processes(&bad, 2).is_err());
        let mut bad = doc;
        bad["route_ready"] = json!(true);
        assert!(validate_loopback_processes(&bad, 2).is_err());
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
