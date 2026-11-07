// SPDX-License-Identifier: AGPL-3.0-only
// Mounted r21: original paired class traces, borrowed launch only.
// Paired original f4 class fixture; trusted launch is scripted, never native proof.
use runtime::managed_workflow::*;
use serde_json::{Value, json};

#[test]
fn paired_original_class_fixture_provenance_is_exact() {
    let rows: Value =
        serde_json::from_str(include_str!("managed_workflow_progress_provenance.json")).unwrap();
    for (row, bytes) in rows.as_array().unwrap().iter().zip([
        include_str!("managed_workflow_progress_oracle.json"),
        include_str!("managed_workflow_numeric_oracle.json"),
    ]) {
        let fixture: Value = serde_json::from_str(bytes).unwrap();
        assert_eq!(
            protocol::canonical::sha256_prefixed(bytes.as_bytes()),
            format!("sha256:{}", row["fixture_sha256"].as_str().unwrap())
        );
        assert_eq!(fixture["oracle"], row["oracle_sha"]);
        assert_eq!(
            fixture["cases"].as_object().unwrap().len() as u64,
            row["case_count"].as_u64().unwrap()
        );
    }
    assert_eq!(rows.as_array().unwrap().len(), 2);
}
struct ControllerFixture;
impl Controller for ControllerFixture {
    fn run_identity(&self) -> Result<(String, String)> {
        Ok(("run-1".into(), "dep-1".into()))
    }
    fn prepare(&mut self, _: &mut dyn Authority, _: &[Value], _: &WorkflowView) -> Result<()> {
        Ok(())
    }
}
struct AuthorityFixture;
impl Authority for AuthorityFixture {
    fn publish(
        &mut self,
        _: &mut dyn Controller,
        _: &str,
        _: &str,
        _: &WorkflowView,
    ) -> Result<Vec<Value>> {
        Ok(vec![json!({"offer_id":"offer","expires_at":1300.0})])
    }
    fn request_cleanup(&mut self, _: &Value) -> Result<()> {
        Ok(())
    }
}
struct LaunchFixture<'a> {
    ops: &'a Value,
    states: Vec<Value>,
}
impl Launch for LaunchFixture<'_> {
    fn validate_approval(&self, _: &Value) -> Result<()> {
        Ok(())
    }
    fn validate_port(&self, _: u16) -> Result<()> {
        Ok(())
    }
    fn run(
        &mut self,
        _: &Value,
        _: u16,
        view: &WorkflowView,
        hooks: &mut LaunchHooks<'_>,
    ) -> Result<Value> {
        for op in self.ops.as_array().unwrap() {
            match op[0].as_str().unwrap() {
                "event" => hooks.verified_challenge_event(
                    op[1].as_str().unwrap(),
                    op[2].as_str().unwrap(),
                    op[3].as_str().unwrap(),
                    &op[4],
                    &op[5],
                ),
                "stop" => view.stop(),
                "failure" => {
                    hooks.startup_failure(op[1].as_str().unwrap(), op[2].as_str().unwrap())
                }
                "started" => hooks.started(json!({"registry_origin":"http://127.0.0.1:50000"}))?,
                _ => panic!("unknown source operation"),
            }
            self.states.push(view.status());
        }
        Ok(json!({"outcome":"stopped"}))
    }
}
fn source_sequence(name: &str) {
    let mut fixture: Value =
        serde_json::from_str(include_str!("managed_workflow_progress_oracle.json")).unwrap();
    if fixture["cases"].get(name).is_none() {
        fixture =
            serde_json::from_str(include_str!("managed_workflow_numeric_oracle.json")).unwrap();
    }
    let row = &fixture["cases"][name];
    {
        let w = ManagedServingWorkflow::new();
        let view = w.view();
        if name != "unbound" {
            w.bind_progress_identity("run-1", "dep-1", 0).unwrap();
        }
        assert_eq!(view.status(), row["before"], "{name}:before");
        let (mut authority, mut controller, mut launch) = (
            AuthorityFixture,
            ControllerFixture,
            LaunchFixture {
                ops: &row["ops"],
                states: vec![],
            },
        );
        let result = w
            .run(
                WorkflowPorts {
                    authority: &mut authority,
                    controller: &mut controller,
                    launch: &mut launch,
                },
                &json!({}),
                50000,
                None,
                None,
            )
            .unwrap();
        assert_eq!(result, row["result"], "{name}:result");
        assert_eq!(json!(launch.states), row["states"], "{name}:states");
        assert_eq!(view.status(), row["status"], "{name}:status");
    }
}
#[test]
fn source_sequential() {
    source_sequence("sequential")
}
#[test]
fn source_gaps_duplicates() {
    source_sequence("gaps_duplicates")
}
#[test]
fn source_identity_request() {
    source_sequence("identity_request")
}
#[test]
fn source_limit() {
    source_sequence("limit")
}
#[test]
fn source_begin_kind() {
    source_sequence("begin_kind")
}
#[test]
fn source_stop() {
    source_sequence("stop")
}
#[test]
fn source_failure() {
    source_sequence("failure")
}
#[test]
fn source_started() {
    source_sequence("started")
}
#[test]
fn source_unbound() {
    source_sequence("unbound")
}
#[test]
fn source_begin_float() {
    source_sequence("begin_float")
}
#[test]
fn source_begin_float_near() {
    source_sequence("begin_float_near")
}
#[test]
fn source_token_exact_int() {
    source_sequence("token_exact_int")
}
#[test]
fn source_limit_exact_int() {
    source_sequence("limit_exact_int")
}
#[test]
fn source_begin_bool() {
    source_sequence("begin_bool")
}
#[test]
fn source_json_domain_refusals() {
    source_sequence("json_domain_refusals")
}
