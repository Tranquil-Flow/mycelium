// SPDX-License-Identifier: AGPL-3.0-only
//! The phone's stage-only node replays the Python Router's behaviour scripts
//! (`contracts/phone-stage-node-golden`): every registration, admission,
//! refusal, cached retry, cancellation, tombstone, transport acknowledgement
//! and every byte sent must match what the real Python Router did as the same
//! middle placement.

use mycelium_mobile_core::stage_node::{Bounds, PHONE_BOUNDS, StageNode};
use mycelium_mobile_core::wire::{self, Activation};
use serde_json::Value;
use sha2::{Digest, Sha256};

/// `MYCELIUM_PHONE_NODE_GOLDEN` points a cross-compiled binary at a pushed copy.
fn dir() -> std::path::PathBuf {
    std::env::var_os("MYCELIUM_PHONE_NODE_GOLDEN")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../contracts/phone-stage-node-golden"))
}

fn bounds(v: &Value) -> Bounds {
    let n = |k: &str| v[k].as_u64().unwrap() as usize;
    Bounds {
        quota_per_creator: n("quota_per_creator"),
        max_creators: n("max_creators"),
        pending_per_source: n("pending_per_source"),
        pending_total: n("pending_total"),
        max_paths: n("max_paths"),
    }
}

/// The golden runtime's output: `0.5 * ((i % 17) - 8) / 8` as f32, shape (1, seq, 8).
fn stage_output(seq: usize) -> Vec<u8> {
    let values: Vec<f32> = (0..seq * 8).map(|i| 0.5 * (((i % 17) as f32) - 8.0) / 8.0).collect();
    Activation::from_flat_f32(seq, 8, &values).encode().unwrap()
}

#[test]
fn every_python_router_decision_and_sent_byte_is_reproduced() {
    let index: Value = serde_json::from_str(&std::fs::read_to_string(dir().join("index.json")).unwrap()).unwrap();
    for (name, digest) in index["frames"].as_object().unwrap() {
        assert_eq!(hex::encode(Sha256::digest(std::fs::read(dir().join(name)).unwrap())), digest.as_str().unwrap());
    }
    let local = index["local_node_id"].as_str().unwrap();
    // The scripts were generated with the phone's own bounds.
    assert_eq!(bounds(&index["phone_bounds"]), PHONE_BOUNDS);
    let mut steps_checked = 0;
    for scenario in index["scenarios"].as_array().unwrap() {
        let name = scenario["name"].as_str().unwrap();
        let mut node = StageNode::with_bounds(local, bounds(&scenario["bounds"]));
        for (i, expected) in scenario["steps"].as_array().unwrap().iter().enumerate() {
            let bytes = std::fs::read(dir().join(expected["frame"].as_str().unwrap())).unwrap();
            let frame = wire::decode_frame(&bytes).unwrap();
            // The injected stage failure of this step, if Python's runtime failed.
            let failure = (expected["disposition"] == "FAILED" && expected["executed"] == 1).then(|| expected["reason"].as_str().unwrap().to_owned());
            let mut ran = 0;
            let at = expected["at"].as_f64().unwrap();
            let step = node.on_frame_at(at, expected["from"].as_str().unwrap(), &frame, &mut |path, hop, input| {
                ran += 1;
                assert_eq!(Activation::decode(input).unwrap().shape[2] as usize, path.hidden_size);
                match &failure {
                    Some(reason) => Err(reason.clone()),
                    None => Ok(stage_output(if hop.phase == "PREFILL" { path.prompt_length } else { hop.position + 1 })),
                }
            });
            let at = format!("{name}[{i}] t={at}");
            assert_eq!(step.ack, expected["transport_ack"].as_bool().unwrap(), "{at} ack");
            if let Some(accepted) = expected.get("accepted") {
                assert_eq!(step.accepted, accepted.as_bool(), "{at} accepted");
            }
            if let Some(disposition) = expected.get("disposition") {
                assert_eq!(step.disposition, disposition.as_str(), "{at} disposition");
                assert_eq!(step.reason, expected["reason"].as_str().unwrap(), "{at} reason");
                assert_eq!(ran, expected["executed"].as_u64().unwrap(), "{at} executed");
                assert_eq!(step.executed, ran == 1, "{at} executed flag");
            }
            let sends = expected["sends"].as_array().unwrap();
            assert_eq!(step.sends.len(), sends.len(), "{at} sends");
            for (got, want) in step.sends.iter().zip(sends) {
                assert_eq!(got.kind, want["kind"].as_str().unwrap(), "{at}");
                assert_eq!(got.to, want["to"].as_str().unwrap(), "{at}");
                let bytes = std::fs::read(dir().join(want["frame"].as_str().unwrap())).unwrap();
                assert_eq!(got.frame, bytes, "{at}: sent frame differs from the Python Router's");
            }
            // A cancellation of a registered path releases its replay results.
            if frame.message_type == "PathCancellation"
                && let Some((_, path_id)) = &step.released
            {
                assert_eq!(step.accepted, Some(true), "{at}");
                assert_eq!(path_id, frame.body["path_id"].as_str().unwrap(), "{at}");
                assert!(node.path(path_id).is_none(), "{at}");
            }
            steps_checked += 1;
        }
    }
    assert!(steps_checked >= 45, "{steps_checked}");
}
