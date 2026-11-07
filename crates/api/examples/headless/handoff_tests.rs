// SPDX-License-Identifier: AGPL-3.0-only
//! External producer-consumption qualification, not a synthetic controller.
use super::*;
use api::{
    compiled_handoff::{CompiledHandoff, Pins},
    desktop::Service,
};
use runtime::managed_workflow::{Controller, Launch};

#[test]
#[ignore = "requires an explicitly admitted read-only producer packet"]
fn r35_genuine_handoff_public_host_ownership_failure_and_stop() {
    let admission_path = std::env::var("MYCELIUM_COMPILED_HANDOFF_ADMISSION")
        .expect("explicit parent-admitted source required");
    let admission: Value = serde_json::from_slice(&fs::read(admission_path).unwrap()).unwrap();
    let source = PathBuf::from(admission["path"].as_str().unwrap());
    let pins: Pins = serde_json::from_value(admission["pins"].clone()).unwrap();
    // Independent expectations: parse the pinned source, never consumer output.
    let read = |name: &str| -> Value {
        serde_json::from_slice(&fs::read(source.join(name)).unwrap()).unwrap()
    };
    let plan = read("compiled/operator-plan.json");
    let complete = read("compile-complete.private.json");
    let report = read("compiled/build-report.json");
    let checkpoint = read("compiled/local-challenge-checkpoint.json");
    let mut handoff = Some(CompiledHandoff::open(&source, &pins).expect("genuine producer input"));
    let input = handoff.as_ref().unwrap();
    assert_eq!(input.plan(), &plan);
    assert_eq!(input.progress_identity(), &complete);
    assert_eq!(input.launch().checkpoint(), &checkpoint);
    assert_eq!(
        input.launch().verification_keys(),
        &plan["verification_keys"]
    );
    assert_eq!(
        input.controller().run_identity().unwrap(),
        (
            plan["run_id"].as_str().unwrap().into(),
            complete["deployment_id"].as_str().unwrap().into()
        )
    );
    let original_plan = Path::new(report["operator_plan"].as_str().unwrap());
    let original_root = original_plan.parent().unwrap().parent().unwrap();
    for (name, path) in plan["paths"].as_object().unwrap() {
        let expected = source.join(
            Path::new(path.as_str().unwrap())
                .strip_prefix(original_root)
                .unwrap(),
        );
        assert_eq!(input.resolved_paths()[name], expected);
        assert!(
            !expected.exists(),
            "retained output paths must remain absent/read-only"
        );
    }
    let nodes = plan["controller"]["run_plan"]["nodes"].as_array().unwrap();
    assert_eq!(input.nodes().len(), nodes.len());
    for node in nodes {
        let id = node["node_id"].as_str().unwrap();
        let actual = &input.nodes()[id];
        assert_eq!(&actual.node, node);
        assert_eq!(
            actual.assignment,
            read(&format!(
                "compiled/transfer-bundle/{}",
                node["configure"]["assignment_file"].as_str().unwrap()
            ))
        );
        assert_eq!(
            actual.stage_pack,
            read(&format!(
                "compiled/transfer-bundle/{}",
                node["configure"]["stage_pack_file"].as_str().unwrap()
            ))
        );
        let expected = checkpoint["assignments"]
            .as_array()
            .unwrap()
            .iter()
            .find(|a| a["node_id"] == id)
            .unwrap();
        let proof = plan["controller"]["transfer_manifest"]["files"]
            .as_array()
            .unwrap()
            .iter()
            .find(|f| f["content_digest"] == expected["load_proof_digest"])
            .unwrap();
        assert_eq!(
            actual.load_proof,
            read(&format!(
                "compiled/transfer-bundle/{}",
                proof["path"].as_str().unwrap()
            ))
        );
        assert_eq!(
            actual.offer,
            *plan["controller"]["membership_snapshot"]["assignment_offers"]
                .as_array()
                .unwrap()
                .iter()
                .find(|o| o["message"]["recipient_node_id"] == id)
                .unwrap()
        );
        assert_eq!(
            actual.peer,
            *plan["controller"]["peers"]
                .as_array()
                .unwrap()
                .iter()
                .find(|p| p["node_id"] == id)
                .unwrap()
        );
        assert_eq!(
            actual.transfer_manifest,
            plan["controller"]["node_transfer_manifests"]["manifests"][id]
        );
    }
    // No approval can turn retained evidence into current execution authority.
    assert!(input.launch().validate_approval(&Value::Null).is_err());
    let custody = input.custody();
    let view = input.view();
    let state = std::env::temp_dir().join(format!("l10-r35-consumption-{}", std::process::id()));
    assert!(!state.exists());
    let core = Arc::new(Mutex::new(load_core(state.clone()).unwrap()));
    let mut host = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    // Behavioral RED is here: the real public Host still refuses installation.
    host.install_compiled_handoff(&mut handoff)
        .expect("public Host must consume genuine compile handoff");
    assert!(handoff.is_none(), "success transfers sole ownership");
    assert!(custody.retained());
    assert!(!view.is_stopped());
    let backend = PersistentBackend::new(
        state.clone(),
        host,
        Rest {
            core: core.clone(),
            published: Arc::new(PublishedStatus::new()),
        },
    );
    let control = Control::new(SignedBackend::new(backend, now));
    let status = control.action("/api/serving/status", &Map::new()).unwrap();
    assert_eq!(status["serving"]["state"], "prepared");
    assert_eq!(status["compilation"]["state"], complete["state"]);
    assert_eq!(status["route_ready"], report["route_ready"]);
    let stopped = control.action("/api/serving/stop", &Map::new()).unwrap();
    assert!(view.is_stopped());
    assert!(
        !custody.retained(),
        "Stop releases both controller and launch source custody"
    );
    assert!(stopped["serving"].is_null());
    assert_eq!(stopped["route_ready"], report["route_ready"]);
    assert_eq!(
        control.action("/api/serving/stop", &Map::new()).unwrap(),
        stopped
    );
    drop(control);
    let mut rejected = Some(CompiledHandoff::open(&source, &pins).unwrap());
    let rejected_view = rejected.as_ref().unwrap().view();
    let rejected_custody = rejected.as_ref().unwrap().custody();
    let mut host = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    core.lock().unwrap().closed = true;
    assert!(host.install_compiled_handoff(&mut rejected).is_err());
    assert!(
        rejected.is_some(),
        "failed transfer must retain caller cleanup owner"
    );
    assert!(rejected_custody.retained());
    drop(rejected);
    assert!(rejected_view.is_stopped());
    assert!(!rejected_custody.retained());
    drop(host);
    drop(core);
    fs::remove_dir_all(state).unwrap();
}

#[test]
#[ignore = "requires an explicitly admitted read-only producer packet"]
fn r36_genuine_handoff_temporary_host_preserves_custody_until_quit() {
    let admission: Value = serde_json::from_slice(
        &fs::read(std::env::var("MYCELIUM_COMPILED_HANDOFF_ADMISSION").unwrap()).unwrap(),
    )
    .unwrap();
    let source = PathBuf::from(admission["path"].as_str().unwrap());
    let pins: Pins = serde_json::from_value(admission["pins"].clone()).unwrap();
    let mut handoff = Some(CompiledHandoff::open(&source, &pins).unwrap());
    let view = handoff.as_ref().unwrap().view();
    let custody = handoff.as_ref().unwrap().custody();
    let state = std::env::temp_dir().join(format!("l10-r36-custody-{}", std::process::id()));
    assert!(!state.exists());
    let core = Arc::new(Mutex::new(load_core(state.clone()).unwrap()));
    let mut owner = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    owner.install_compiled_handoff(&mut handoff).unwrap();
    // The real contributor/reverification paths construct temporary Host handles.
    // Such a read-only handle is not the owner of the shared Core's workflow.
    let reader = HeadlessHost {
        core: core.clone(),
        runs: None,
    };
    assert_eq!(reader.serving_status(), owner.serving_status());
    drop(reader);
    assert!(
        custody.retained(),
        "temporary Host must not release Core workflow custody"
    );
    assert!(
        !view.is_stopped(),
        "temporary Host must not Stop the mounted workflow"
    );
    owner.quit().unwrap();
    assert!(view.is_stopped());
    assert!(
        !custody.retained(),
        "Quit releases actual source custody while Core readers remain"
    );
    drop(owner);
    drop(core);
    fs::remove_dir_all(state).unwrap();
}
