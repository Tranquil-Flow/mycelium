// SPDX-License-Identifier: AGPL-3.0-only
//! Static reachability contract only: native entrypoints are compiled, never
//! executed in the CPU qualification. Behavioral selector tests live separately.
#[test]
fn actual_native_entrypoints_use_the_cpu_selector_before_context() {
    let stage = include_str!("../src/native/stage.rs");
    assert!(
        stage.contains("native_selection::select("),
        "NativeStage must call the CPU selector, not hardcode Qwen2"
    );
    let selected = stage.find("native_selection::select(").unwrap();
    let verified = stage
        .find("verify_source(")
        .expect("source verified before Context");
    let context = stage.find("Context::new(").unwrap();
    assert!(selected < verified && verified < context);
    let agent = include_str!("../src/native/agent_worker.rs");
    let worker = include_str!("../src/native/worker_main.rs");
    let child = include_str!("../src/native/child.rs");
    assert!(agent.contains("start_assigned(") && agent.contains("stage_image"));
    assert!(worker.contains("prepare_assigned(") && worker.contains("stage_image"));
    assert!(
        child.contains("AgentWorker::start_assigned(")
            && child.contains("native_selection::legacy_plan_image(&plan, owner_leaf)?")
    );
    assert!(
        !child.contains("plan.get(\"stage_image\")"),
        "private metadata must not extend the external signed plan"
    );
    assert!(
        worker.contains("native_selection::assigned_image("),
        "private decoder must validate before dispatch"
    );
}
