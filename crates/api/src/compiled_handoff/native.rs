// SPDX-License-Identifier: AGPL-3.0-only
//! Additive native-local reader; no changes to historical physical-runner bytes.
use super::*;
use serde_json::json;
use stage_contract::native_proof as contract;

pub(super) fn open_native(
    root: &Path,
    pins: &Pins,
    docs: &BTreeMap<&str, Value>,
    mut files: BTreeMap<String, File>,
) -> Result<CompiledHandoff> {
    let plan = &docs["compiled/operator-plan.json"];
    let checkpoint = &docs["compiled/local-challenge-checkpoint.json"];
    let complete = &docs["compile-complete.private.json"];
    let start = &docs["compile-start.private.json"];
    let report = &docs["compiled/build-report.json"];
    if plan.as_object().is_none_or(|p| {
        p.len() != contract::PLAN_FIELDS.len()
            || contract::PLAN_FIELDS.iter().any(|f| !p.contains_key(*f))
    }) || plan["protocol"] != contract::PLAN_PROTOCOL
        || plan["scope"] != "local_native_qualification"
        || plan["route_ready"] != false
        || complete["protocol"] != "mycelium.chain_compile_complete.v1"
        || complete["state"] != "compiled"
        || complete["route_ready"] != false
        || start["protocol"] != "mycelium.chain_compile_start.v1"
        || report["route_ready"] != false
    {
        return Err(bad());
    }
    for (name, protocol, expected) in [
        (
            "template.json",
            contract::TEMPLATE_PROTOCOL,
            contract::TEMPLATE_FIELDS,
        ),
        (
            "compiled/build-report.json",
            contract::REPORT_PROTOCOL,
            contract::REPORT_FIELDS,
        ),
        (
            "compiled/compilation-cache.json",
            contract::CACHE_PROTOCOL,
            contract::CACHE_FIELDS,
        ),
    ] {
        let doc = &docs[name];
        if doc["protocol"] != protocol
            || doc.as_object().is_none_or(|object| {
                object.len() != expected.len()
                    || expected.iter().any(|field| !object.contains_key(*field))
            })
        {
            return Err(bad());
        }
    }
    let mut statement = plan.clone();
    statement
        .as_object_mut()
        .ok_or_else(bad)?
        .remove("signature");
    protocol::identity::verify(&statement, &plan["signature"], &plan["signer"])
        .map_err(|_| bad())?;
    if !array(&plan["verification_keys"]["gossip"])?.contains(&plan["signer"]) {
        return Err(bad());
    }
    equal(
        &docs["template.json"]["verification_keys"],
        &plan["verification_keys"],
    )?;
    equal(&docs["template.json"]["members"], &plan["members"])?;
    let bound = plan["documents"].as_object().ok_or_else(bad)?;
    if bound.len() + 2 != docs.len() {
        return Err(bad());
    }
    for (name, value) in docs {
        if *name == "compiled/operator-plan.json" || *name == "compile-complete.private.json" {
            continue;
        }
        equal(
            &bound[*name],
            &json!(protocol::canonical::digest(value).map_err(|_| bad())?),
        )?;
    }
    equal(
        &complete["operator_plan_digest"],
        &json!(protocol::canonical::digest(plan).map_err(|_| bad())?),
    )?;
    for doc in [
        complete,
        start,
        checkpoint,
        report,
        &docs["observations.json"],
    ] {
        equal(&doc["run_id"], &plan["run_id"])?;
    }
    for doc in [complete, start, checkpoint, &docs["observations.json"]] {
        equal(&doc["deployment_id"], &plan["deployment_id"])?;
    }
    for doc in [complete, start] {
        equal(&doc["epoch"], &plan["epoch"])?;
    }
    equal(
        &report["challenge_output_token_ids"],
        &checkpoint["output_token_ids"],
    )?;
    let reference = crate::native_compiler::reference().map_err(|_| bad())?;
    crate::native_compiler::validate_evidence(plan, checkpoint, &reference)
        .map_err(|error| WorkflowError::refused(error.0))?;
    let mut all_pins = pins.clone();
    let mut paths = BTreeMap::new();
    for role in ["source", "binary"] {
        let path = Path::new(text(&plan[role]["path"])?);
        if !path.is_absolute() || path.canonicalize().map_err(|_| bad())? != path {
            return Err(bad());
        }
        let pin = Pin {
            bytes: plan[role]["bytes"].as_u64().ok_or_else(bad)?,
            sha256: text(&plan[role]["sha256"])?.into(),
        };
        let mut file = open(
            path.parent().ok_or_else(bad)?,
            path.file_name().and_then(|s| s.to_str()).ok_or_else(bad)?,
        )?;
        check(&mut file, &pin)?;
        files.insert(format!("@native-{role}"), file);
        all_pins.insert(format!("@native-{role}"), pin);
        paths.insert(role.into(), path.to_owned());
    }
    equal(
        &plan["source"]["sha256"],
        &reference["recorded"]["identity"]["checkpoint_sha256"],
    )?;
    equal(
        &plan["source"]["bytes"],
        &reference["recorded"]["identity"]["checkpoint_bytes"],
    )?;
    paths.insert(
        "operator_plan".into(),
        root.join("compiled/operator-plan.json"),
    );
    let stages = array(&plan["stages"])?;
    let proofs = array(&checkpoint["proofs"])?;
    let topology = array(&docs["topology.json"]["nodes"])?;
    if topology.len() != stages.len() {
        return Err(bad());
    }
    let mut nodes = BTreeMap::new();
    for ((stage, proof), node) in stages.iter().zip(proofs).zip(topology) {
        equal(&stage["node_id"], &node["node_id"])?;
        let id = text(&stage["node_id"])?;
        nodes.insert(id.to_owned(),NodeBinding {
            node:node.clone(),peer:json!({"transport":"native_local_socket","worker_pid":proof["worker_pid"],"host_id":proof["host_id"],"boot_id":proof["boot_id"]}),
            assignment:stage["assignment"].clone(),offer:Value::Null,stage_pack:Value::Null,
            load_proof:proof.clone(),transfer_manifest:Value::Null,
        });
    }
    let inputs = Arc::new(Inputs {
        plan: plan.clone(),
        checkpoint: checkpoint.clone(),
        complete: complete.clone(),
        nodes,
        resolved_paths: paths,
        files,
        pins: all_pins,
    });
    let workflow = ManagedServingWorkflow::new();
    workflow.bind_progress_identity(
        text(&plan["run_id"])?,
        text(&plan["deployment_id"])?,
        plan["epoch"].as_u64().ok_or_else(bad)?,
    )?;
    Ok(CompiledHandoff {
        controller: CompiledController(inputs.clone()),
        launch: CompiledLaunch(inputs),
        workflow,
    })
}
