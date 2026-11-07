// SPDX-License-Identifier: AGPL-3.0-only
// serving.py:739-752,864-898 + workflow.py:287-293 @ f4e366e9.
use runtime::managed_workflow::*;
use serde_json::{Value, json};
struct Fixture {
    root: std::path::PathBuf,
    launch: PreparedLaunch,
    approval: Value,
}
impl Fixture {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!("s20-approval-{name}-{}", std::process::id()));
        std::fs::create_dir(&root).unwrap();
        let plan = root.join("plan.json");
        let raw = b"{\"native\":\"source-fixture-only\"}";
        std::fs::write(&plan, raw).unwrap();
        let model = json!({"model_id":"fixture","resolved_commit":"commit","source_representation":"safetensors","runtime_dtype":"float32","runtime_quantization":"none"});
        let resource_proposal = json!({"execution_download_bytes":0,"additional_disk_bytes":10,"max_combined_rss_bytes":20,"max_wall_seconds":30,"run_count":1});
        let digest = protocol::canonical::sha256_prefixed(raw);
        let mut approval = model.clone();
        for(k,v)in json!({"max_execution_download_bytes":0,"max_additional_disk_bytes":10,"max_combined_rss_bytes":20,"max_wall_seconds":30,"run_count":1,"operator_plan_digest":digest,"physical_host_ids":["host-1"]}).as_object().unwrap(){approval[k]=v.clone();}
        Self {
            root,
            launch: PreparedLaunch {
                model,
                resource_proposal,
                operator_plan: plan,
                operator_plan_digest: digest,
                physical_host_ids: vec!["host-1".into()],
            },
            approval,
        }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        std::fs::remove_dir_all(&self.root).unwrap();
    }
}
#[test]
fn exact_model_resources_and_separate_plan_approval_are_checked() {
    let f = Fixture::new("fields");
    assert!(f.launch.validate_approval(&f.approval).is_ok());
    assert_eq!(
        f.launch.validate_approval(&Value::Null).unwrap_err().code,
        "model_execution_approval_required"
    );
    for field in [
        "model_id",
        "resolved_commit",
        "source_representation",
        "runtime_dtype",
        "runtime_quantization",
    ] {
        let mut a = f.approval.clone();
        a[field] = "wrong".into();
        assert_eq!(
            f.launch.validate_approval(&a).unwrap_err().code,
            "model_execution_approval_mismatch",
            "{field}"
        );
    }
    for (field, value) in [
        ("max_execution_download_bytes", 1),
        ("max_additional_disk_bytes", 9),
        ("max_combined_rss_bytes", 19),
        ("max_wall_seconds", 29),
        ("run_count", 2),
    ] {
        let mut a = f.approval.clone();
        a[field] = value.into();
        assert_eq!(
            f.launch.validate_approval(&a).unwrap_err().code,
            "model_execution_approval_mismatch",
            "{field}"
        );
    }
    let mut larger = f.approval.clone();
    larger["max_wall_seconds"] = 31.into();
    assert!(f.launch.validate_approval(&larger).is_ok());
    for field in ["operator_plan_digest", "physical_host_ids"] {
        let mut a = f.approval.clone();
        a[field] = json!("wrong");
        assert_eq!(
            f.launch.validate_approval(&a).unwrap_err().code,
            "managed_workflow_approval_mismatch"
        );
    }
}
#[test]
fn plan_bytes_are_reread_and_symlinks_empty_and_nonfiles_refused() {
    let mut f = Fixture::new("plan");
    assert!(f.launch.validate_approval(&f.approval).is_ok());
    std::fs::write(&f.launch.operator_plan, b"changed").unwrap();
    assert_eq!(
        f.launch.validate_approval(&f.approval).unwrap_err().code,
        "managed_workflow_approval_mismatch"
    );
    std::fs::write(&f.launch.operator_plan, b"").unwrap();
    assert_eq!(
        f.launch.validate_approval(&f.approval).unwrap_err().code,
        "operator_plan_invalid"
    );
    let symlink = f.root.join("link");
    std::os::unix::fs::symlink(&f.launch.operator_plan, &symlink).unwrap();
    f.launch.operator_plan = symlink;
    assert_eq!(
        f.launch.validate_approval(&f.approval).unwrap_err().code,
        "operator_plan_invalid"
    );
    f.launch.operator_plan = f.root.clone();
    assert_eq!(
        f.launch.validate_approval(&f.approval).unwrap_err().code,
        "operator_plan_invalid"
    );
}
#[test]
fn dynamic_port_probe_is_real_and_does_not_keep_listener() {
    for p in [8791, 8876, 8877, 4350, 4351, 4352] {
        assert_eq!(
            validate_dynamic_port(p).unwrap_err().code,
            "port_not_available_for_a13"
        );
    }
    for p in [0, 1024, 49151] {
        assert_eq!(
            validate_dynamic_port(p).unwrap_err().code,
            "dynamic_loopback_port_required"
        );
    }
    let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
    let port = listener.local_addr().unwrap().port();
    assert!(port >= 49152, "macOS dynamic port fixture");
    assert_eq!(
        validate_dynamic_port(port).unwrap_err().code,
        "dynamic_loopback_port_unavailable"
    );
    drop(listener);
    assert!(validate_dynamic_port(port).is_ok());
    let reopened = std::net::TcpListener::bind(("127.0.0.1", port)).unwrap();
    drop(reopened);
}
