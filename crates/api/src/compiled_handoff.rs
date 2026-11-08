// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only consumption of independently admitted compile outputs.
//!
//! This is NOT a compiler, current membership authorization, or an execution
//! grant. Retained Python payloads are opaque hashed data and are never run.
//! The original paths are retained and explicitly resolved under the admitted
//! root; even the relocated output paths are never opened for writing.
use runtime::managed_workflow::{
    Authority, Controller, Launch, LaunchHooks, ManagedServingWorkflow, Result, WorkflowError,
    WorkflowView,
};
use serde::{Deserialize, Serialize};
use serde_json::Value;
mod native;
use sha2::{Digest, Sha256};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    os::unix::fs::OpenOptionsExt,
    path::{Component, Path, PathBuf},
    sync::{Arc, Weak},
};

#[derive(Clone, Debug, Deserialize, Serialize)]
pub struct Pin {
    pub bytes: u64,
    pub sha256: String,
}
/// Pins are supplied by a trusted producer admission, never inferred from the
/// same input being checked. The HTTP request body cannot establish admission.
pub type Pins = BTreeMap<String, Pin>;

fn bad() -> WorkflowError {
    WorkflowError::refused("compiled_handoff_invalid")
}
fn text(v: &Value) -> Result<&str> {
    v.as_str().filter(|s| !s.is_empty()).ok_or_else(bad)
}
fn array(v: &Value) -> Result<&Vec<Value>> {
    v.as_array().filter(|a| !a.is_empty()).ok_or_else(bad)
}
fn equal(a: &Value, b: &Value) -> Result<()> {
    if a.is_null() || a != b {
        Err(bad())
    } else {
        Ok(())
    }
}
fn relative(path: &Path) -> Result<()> {
    if path.as_os_str().is_empty()
        || path
            .components()
            .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(bad());
    }
    Ok(())
}
fn open(root: &Path, name: &str) -> Result<File> {
    let suffix = Path::new(name);
    relative(suffix)?;
    let mut path = root.to_owned();
    for part in suffix.components() {
        path.push(part);
        if std::fs::symlink_metadata(&path)
            .map_err(|_| bad())?
            .file_type()
            .is_symlink()
        {
            return Err(bad());
        }
    }
    let file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW)
        .open(path)
        .map_err(|_| bad())?;
    if !file.metadata().map_err(|_| bad())?.is_file() {
        return Err(bad());
    }
    Ok(file)
}
struct HashWriter(Sha256);
impl Write for HashWriter {
    fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
        self.0.update(bytes);
        Ok(bytes.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        Ok(())
    }
}
fn check(file: &mut File, pin: &Pin) -> Result<()> {
    if pin.sha256.len() != 64
        || !pin
            .sha256
            .bytes()
            .all(|c| c.is_ascii_digit() || (b'a'..=b'f').contains(&c))
        || file.metadata().map_err(|_| bad())?.len() != pin.bytes
    {
        return Err(bad());
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| bad())?;
    let mut hash = HashWriter(Sha256::new());
    let count = std::io::copy(file, &mut hash).map_err(|_| bad())?;
    if count != pin.bytes || format!("{:x}", hash.0.finalize()) != pin.sha256 {
        return Err(bad());
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| bad())?;
    Ok(())
}
fn document(file: &mut File) -> Result<Value> {
    let cap = budgets::BudgetId::PyMyceliumA13MacosServingMaxJsonBytesL360
        .bytes()
        .map_err(|_| bad())?
        .get();
    if file.metadata().map_err(|_| bad())?.len() > cap {
        return Err(bad());
    }
    file.seek(SeekFrom::Start(0)).map_err(|_| bad())?;
    let mut bytes = Vec::new();
    file.take(cap + 1)
        .read_to_end(&mut bytes)
        .map_err(|_| bad())?;
    if bytes.len() as u64 > cap {
        return Err(bad());
    }
    serde_json::from_slice(&bytes).map_err(|_| bad())
}

/// Exact retained per-node material. No digest-only slice recipe is substituted.
#[derive(Clone, Debug)]
pub struct NodeBinding {
    pub node: Value,
    pub peer: Value,
    pub assignment: Value,
    pub offer: Value,
    pub stage_pack: Value,
    pub load_proof: Value,
    pub transfer_manifest: Value,
}
struct Inputs {
    plan: Value,
    checkpoint: Value,
    complete: Value,
    nodes: BTreeMap<String, NodeBinding>,
    resolved_paths: BTreeMap<String, PathBuf>,
    files: BTreeMap<String, File>,
    pins: Pins,
}
/// Non-owning witness: status readers cannot prolong source-handle custody.
#[derive(Clone)]
pub struct Custody(Weak<Inputs>);
impl Custody {
    pub fn retained(&self) -> bool {
        self.0.strong_count() != 0
    }
}

pub struct CompiledController(Arc<Inputs>);
impl Controller for CompiledController {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok((
            text(&self.0.plan["run_id"])?.into(),
            text(&self.0.complete["deployment_id"])?.into(),
        ))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        // Retention admission is deliberately not authority to publish historical
        // offers or send commands to their old endpoints.
        Err(WorkflowError::refused(
            "retained_compile_execution_not_admitted",
        ))
    }
}
pub struct CompiledLaunch(Arc<Inputs>);
impl Launch for CompiledLaunch {
    fn validate_approval(&self, _: &Value) -> Result<()> {
        Err(WorkflowError::refused(
            "retained_compile_execution_not_admitted",
        ))
    }
    fn run(
        &mut self,
        _: &Value,
        _: u16,
        _: &WorkflowView,
        _: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        Err(WorkflowError::refused(
            "retained_compile_execution_not_admitted",
        ))
    }
}
impl CompiledLaunch {
    pub fn checkpoint(&self) -> &Value {
        &self.0.checkpoint
    }
    pub fn verification_keys(&self) -> &Value {
        &self.0.plan["verification_keys"]
    }
}

/// Sole prepared-workflow owner. No worker, mailbox publication or native
/// session is started. Drop/Stop fences the actual runtime view before releasing
/// all retained source handles, including failed host ownership transfers.
pub struct CompiledHandoff {
    pub(crate) controller: CompiledController,
    pub(crate) launch: CompiledLaunch,
    workflow: ManagedServingWorkflow,
}
impl CompiledHandoff {
    pub fn open(root: &Path, pins: &Pins) -> Result<Self> {
        let root = root.canonicalize().map_err(|_| bad())?;
        let required = [
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
        ];
        let mut files = BTreeMap::new();
        let mut docs = BTreeMap::new();
        for name in required {
            let mut file = open(&root, name)?;
            check(&mut file, pins.get(name).ok_or_else(bad)?)?;
            docs.insert(name, document(&mut file)?);
            files.insert(name.to_owned(), file);
        }
        if docs["compiled/operator-plan.json"]["protocol"]
            == stage_contract::native_proof::PLAN_PROTOCOL
        {
            return native::open_native(&root, pins, &docs, files);
        }
        let plan = &docs["compiled/operator-plan.json"];
        let complete = &docs["compile-complete.private.json"];
        let start = &docs["compile-start.private.json"];
        let report = &docs["compiled/build-report.json"];
        let checkpoint = &docs["compiled/local-challenge-checkpoint.json"];
        if plan["protocol"] != "mycelium.physical_runner_operator_plan.v1"
            || complete["protocol"] != "mycelium.chain_compile_complete.v1"
            || start["protocol"] != "mycelium.chain_compile_start.v1"
            || complete["state"] != "compiled"
            || report["route_ready"] != false
            || checkpoint["state"] != "passed"
        {
            return Err(bad());
        }
        let c = &plan["controller"];
        let run = &c["run_plan"];
        for doc in [complete, start, report, run] {
            equal(&doc["run_id"], &plan["run_id"])?;
        }
        for doc in [start, checkpoint, run, &c["membership_snapshot"]] {
            equal(&doc["deployment_id"], &complete["deployment_id"])?;
        }
        equal(&start["epoch"], &complete["epoch"])?;
        equal(
            &checkpoint["challenge_output_token_ids"],
            &run["expected_token_ids"],
        )?;
        equal(
            &checkpoint["challenge_prompt_token_ids"],
            &run["request"]["prompt_token_ids"],
        )?;
        equal(
            &report["challenge_output_token_ids"],
            &checkpoint["challenge_output_token_ids"],
        )?;
        if report["challenge_prompt_token_count"].as_u64()
            != Some(array(&checkpoint["challenge_prompt_token_ids"])?.len() as u64)
        {
            return Err(bad());
        }
        for key in ["model_id", "resolved_commit"] {
            equal(&checkpoint[key], &report[key])?;
        }
        let original_plan = Path::new(text(&report["operator_plan"])?);
        let original_root = original_plan
            .parent()
            .and_then(Path::parent)
            .ok_or_else(bad)?;
        if original_plan != original_root.join("compiled/operator-plan.json")
            || original_root.file_name() != root.file_name()
        {
            return Err(bad());
        }
        let mut resolved_paths = BTreeMap::new();
        for (name, path) in plan["paths"].as_object().ok_or_else(bad)? {
            let suffix = Path::new(text(path)?)
                .strip_prefix(original_root)
                .map_err(|_| bad())?;
            relative(suffix)?;
            resolved_paths.insert(name.clone(), root.join(suffix));
        }
        for key in ["evidence_output_dir", "lock_path", "log_path", "state_path"] {
            if !resolved_paths.contains_key(key) {
                return Err(bad());
            }
        }
        let bundle_suffix = Path::new(text(&c["source_root"])?)
            .strip_prefix(original_root)
            .map_err(|_| bad())?;
        relative(bundle_suffix)?;
        if bundle_suffix != Path::new("compiled/transfer-bundle") {
            return Err(bad());
        }
        let mut payload_pins = Pins::new();
        for row in array(&c["transfer_manifest"]["files"])? {
            let name = text(&row["path"])?;
            let digest = text(&row["content_digest"])?;
            let pin = Pin {
                bytes: row["size_bytes"].as_u64().ok_or_else(bad)?,
                sha256: digest.strip_prefix("sha256:").ok_or_else(bad)?.into(),
            };
            if payload_pins.insert(name.into(), pin.clone()).is_some() {
                return Err(bad());
            }
            let full = format!("compiled/transfer-bundle/{name}");
            let mut file = open(&root, &full)?;
            check(&mut file, &pin)?;
            files.insert(full, file);
        }
        if report["transfer_file_count"].as_u64() != Some(payload_pins.len() as u64)
            || report["transfer_bytes"].as_u64()
                != payload_pins
                    .values()
                    .try_fold(0_u64, |n, p| n.checked_add(p.bytes))
        {
            return Err(bad());
        }
        let nodes = bind_nodes(plan, checkpoint, &docs["layer-split.json"], &mut files)?;
        let mut all_pins = pins.clone();
        all_pins.extend(
            payload_pins
                .into_iter()
                .map(|(n, p)| (format!("compiled/transfer-bundle/{n}"), p)),
        );
        let inputs = Arc::new(Inputs {
            plan: plan.clone(),
            checkpoint: checkpoint.clone(),
            complete: complete.clone(),
            nodes,
            resolved_paths,
            files,
            pins: all_pins,
        });
        let workflow = ManagedServingWorkflow::new();
        workflow.bind_progress_identity(
            text(&plan["run_id"])?,
            text(&complete["deployment_id"])?,
            complete["epoch"].as_u64().ok_or_else(bad)?,
        )?;
        Ok(Self {
            controller: CompiledController(inputs.clone()),
            launch: CompiledLaunch(inputs),
            workflow,
        })
    }
    pub fn controller(&self) -> &CompiledController {
        &self.controller
    }
    pub fn launch(&self) -> &CompiledLaunch {
        &self.launch
    }
    pub fn nodes(&self) -> &BTreeMap<String, NodeBinding> {
        &self.controller.0.nodes
    }
    pub fn plan(&self) -> &Value {
        &self.controller.0.plan
    }
    pub fn progress_identity(&self) -> &Value {
        &self.controller.0.complete
    }
    pub fn resolved_paths(&self) -> &BTreeMap<String, PathBuf> {
        &self.controller.0.resolved_paths
    }
    pub fn view(&self) -> WorkflowView {
        self.workflow.view()
    }
    pub fn custody(&self) -> Custody {
        Custody(Arc::downgrade(&self.controller.0))
    }
    pub fn status(&self) -> Value {
        self.workflow.view().status()
    }
    /// Revalidate the exact already-open handles before taking host ownership.
    /// This detects changes but is not an immutable filesystem snapshot claim.
    pub fn revalidate(&self) -> Result<()> {
        for (name, file) in &self.controller.0.files {
            check(
                &mut file.try_clone().map_err(|_| bad())?,
                self.controller.0.pins.get(name).ok_or_else(bad)?,
            )?;
        }
        Ok(())
    }
}
impl Drop for CompiledHandoff {
    fn drop(&mut self) {
        self.workflow.view().stop();
    }
}

fn bind_nodes(
    plan: &Value,
    checkpoint: &Value,
    split: &Value,
    files: &mut BTreeMap<String, File>,
) -> Result<BTreeMap<String, NodeBinding>> {
    let c = &plan["controller"];
    let offers = array(&c["membership_snapshot"]["assignment_offers"])?;
    let peers = array(&c["peers"])?;
    let checkpoints = array(&checkpoint["assignments"])?;
    let stages = array(&split["stages"])?;
    let gossip = array(&plan["verification_keys"]["gossip"])?;
    let load_keys = array(&plan["verification_keys"]["load_proof"])?;
    for key in gossip.iter().chain(load_keys) {
        protocol::identity::key_record_digest(key)
            .map_err(|_| WorkflowError::refused("compiled_handoff_key_invalid"))?;
    }
    let mut result = BTreeMap::new();
    for node in array(&c["run_plan"]["nodes"])? {
        let id = text(&node["node_id"])?;
        let unique = |rows: &Vec<Value>, key: &str| -> Result<Value> {
            let matches: Vec<_> = rows.iter().filter(|r| r[key] == id).collect();
            if matches.len() != 1 {
                return Err(bad());
            }
            Ok(matches[0].clone())
        };
        let peer = unique(peers, "node_id")?;
        if peer["process_transport"] != "managed_https" {
            return Err(bad());
        }
        text(&peer["host_id"])?;
        text(&peer["boot_id"])?;
        let checkpoint = unique(checkpoints, "node_id")?;
        let stage = unique(stages, "node_id")?;
        let matches: Vec<_> = offers
            .iter()
            .filter(|o| o["message"]["recipient_node_id"] == id)
            .collect();
        if matches.len() != 1 {
            return Err(bad());
        }
        let offer = matches[0];
        let message = &offer["message"];
        // Assignment offers belong to the membership snapshot's seed, not
        // the distinct gossip evidence keyring. Both are pinned by the plan.
        let key = &offer["verification_key"];
        equal(
            &key["verification_key_digest"],
            &c["membership_snapshot"]["seed_key_digest"],
        )?;
        equal(
            &offer["signature"]["verification_key_digest"],
            &c["membership_snapshot"]["seed_key_digest"],
        )?;
        protocol::identity::verify(message, &offer["signature"], key)
            .map_err(|_| WorkflowError::refused("compiled_handoff_signature_invalid"))?;
        equal(&message["deployment_id"], &c["run_plan"]["deployment_id"])?;
        equal(
            &message["deployment_epoch"],
            &c["membership_snapshot"]["deployment_epoch"],
        )?;
        let configure = &node["configure"];
        let mut read = |name: &str| -> Result<Value> {
            relative(Path::new(name))?;
            document(
                files
                    .get_mut(&format!("compiled/transfer-bundle/{name}"))
                    .ok_or_else(bad)?,
            )
        };
        let assignment = read(text(&configure["assignment_file"])?)?;
        let pack = read(text(&configure["stage_pack_file"])?)?;
        let proof_name = c["transfer_manifest"]["files"]
            .as_array()
            .ok_or_else(bad)?
            .iter()
            .find(|r| r["content_digest"] == checkpoint["load_proof_digest"])
            .ok_or_else(bad)?;
        let proof = read(text(&proof_name["path"])?)?;
        protocol::assignment::validate_identity(&assignment)
            .map_err(|_| WorkflowError::refused("compiled_handoff_assignment_invalid"))?;
        for (a, b) in [
            ("node_id", "node_id"),
            ("assignment_id", "assignment_id"),
            ("deployment_id", "deployment_id"),
            ("deployment_epoch", "deployment_epoch"),
            ("model_id", "model_id"),
            ("resolved_commit", "resolved_commit"),
            ("manifest_digest", "manifest_digest"),
            ("range", "loaded_range"),
        ] {
            equal(&assignment[a], &proof[b])?;
            equal(&assignment[a], &pack[a])?;
        }
        equal(&assignment["node_id"], &node["node_id"])?;
        equal(&assignment["assignment_id"], &message["assignment_id"])?;
        equal(
            &assignment["deployment_epoch"],
            &message["deployment_epoch"],
        )?;
        equal(&assignment["deployment_id"], &message["deployment_id"])?;
        equal(&assignment["range"]["start_layer"], &stage["start"])?;
        equal(&assignment["range"]["end_layer_exclusive"], &stage["end"])?;
        equal(&proof["load_generation"], &configure["load_generation"])?;
        equal(&proof["load_generation"], &message["load_generation"])?;
        equal(&pack["stage_pack_digest"], &proof["stage_pack_digest"])?;
        equal(&pack["stage_pack_digest"], &message["stage_pack_digest"])?;
        equal(
            &checkpoint["assignment_digest"],
            &message["assignment_digest"],
        )?;
        if protocol::canonical::digest(&assignment).map_err(|_| bad())?
            != text(&message["assignment_digest"])?
            || protocol::canonical::digest(&configure["graph"]).map_err(|_| bad())?
                != text(&message["graph_digest"])?
        {
            return Err(bad());
        }
        let transfer = &c["node_transfer_manifests"]["manifests"][id];
        if transfer != &c["transfer_manifest"] {
            return Err(bad());
        }
        if result
            .insert(
                id.into(),
                NodeBinding {
                    node: node.clone(),
                    peer,
                    assignment,
                    offer: offer.clone(),
                    stage_pack: pack,
                    load_proof: proof,
                    transfer_manifest: transfer.clone(),
                },
            )
            .is_some()
        {
            return Err(bad());
        }
    }
    let ids: BTreeSet<_> = result.keys().collect();
    if ids.len() != peers.len()
        || ids.len() != offers.len()
        || ids.len() != checkpoints.len()
        || ids.len() != stages.len()
    {
        return Err(bad());
    }
    Ok(result)
}
