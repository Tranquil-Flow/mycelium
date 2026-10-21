// SPDX-License-Identifier: AGPL-3.0-only
//! The phone's control channel reproduces, byte for byte, every response and
//! signed observation the Python signer produced for the same key and session
//! (`contracts/phone-node-control-golden`, generated and checked against the
//! controller's acceptance rules by `scripts/generate_phone_node_control_golden.py`).

use mycelium_mobile_core::identity::Identity;
use mycelium_mobile_core::node_control::{ControlEnv, ControlIdentity, ControlNode, PeerSpec, StageView};
use serde_json::Value;

struct Env {
    golden: Value,
}

impl ControlEnv for Env {
    fn endpoint_addr(&self) -> Value {
        self.golden["endpoint_addr"].clone()
    }
    fn stage(&self) -> Option<StageView> {
        let s = &self.golden["stage"];
        let t = |k: &str| s[k].as_str().unwrap().to_owned();
        Some(StageView { assignment_id: t("assignment_id"), stage_id: t("stage_id"), pack_digest: t("pack_digest"), backend: t("backend"), runtime_build_digest: t("runtime_build_digest"), golden_output_digest: t("golden_output_digest"), load_generation: s["load_generation"].as_u64().unwrap() })
    }
    fn install_peers(&mut self, _: &[PeerSpec], _: u64) -> Result<(), String> {
        Ok(())
    }
    fn health(&self) -> Value {
        Value::Object(Default::default())
    }
    fn qualification_snapshot(&self) -> Value { serde_json::json!({}) }
    fn host_resources(&self) -> Value {
        Value::Object(Default::default())
    }
    fn monotonic_ns(&self) -> u64 {
        self.golden["monotonic_ns"].as_u64().unwrap()
    }
    fn stop(&mut self) {}
    fn cancel_wait(&mut self, _: &mycelium_mobile_core::node_cancel::CancelSubject) -> mycelium_mobile_core::node_cancel::Residue {
        Default::default()
    }
}

#[test]
fn every_response_and_signed_observation_matches_the_python_signer() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/readiness_control_golden.json");
    check_golden(&path);
}

#[test]
fn signed_infer_cancel_wait_and_duplicate_match_the_python_signer() {
    let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("tests/cancel_control_golden.json");
    check_golden(&path);
}

fn check_golden(path: &std::path::Path) {
    let golden: Value = serde_json::from_str(&std::fs::read_to_string(path).unwrap()).unwrap();
    let seed: [u8; 32] = hex::decode(golden["seed_hex"].as_str().unwrap()).unwrap().try_into().unwrap();
    let id = &golden["identity"];
    let t = |k: &str| id[k].as_str().unwrap().to_owned();
    let signer = Identity::from_secret(&t("endpoint_id"), &seed).unwrap();
    let mut node = ControlNode::new(ControlIdentity {
        node_id: t("node_id"),
        host_id: t("host_id"),
        process_id: t("process_id"),
        endpoint_id: t("endpoint_id"),
        boot_id: t("boot_id"),
    });
    let mut env = Env { golden: golden.clone() };
    let steps = golden["steps"].as_array().unwrap();
    assert!(steps.len() >= 6);
    for step in steps {
        let command = serde_json::to_vec(&step["command"]).unwrap();
        let response = node.handle("controller-golden", &command, &signer, &mut env);
        if step["command"]["command"] == "bind_request_control" && step["command"]["payload"]["control"]["publisher_generation"] == 0 {
            // The archived legacy golden predates the Python validator's
            // positive generations/digest contract. It must now fail closed.
            let refusal: Value = serde_json::from_slice(&response).unwrap();
            assert_eq!(refusal["error"]["code"], "invalid_bind_request_control");
        } else {
            assert_eq!(String::from_utf8(response).unwrap(), step["response_canonical"].as_str().unwrap(), "{}", step["command"]["command"]);
        }
    }
}
