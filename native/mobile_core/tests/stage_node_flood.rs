// SPDX-License-Identifier: AGPL-3.0-only
//! Per-frame cost of the stage node's path tables at the phone's bounds.
//!
//! A configured route peer may send frames as fast as the transport admits
//! them. With every table full (4096 pending cancellations from 16 sources,
//! 16 creator slots at their record quota, every live path held), each
//! refused or displacing frame must cost a bounded number of table
//! operations, not a scan of the tables. The flood runs 20 000 frames of each
//! kind and must finish well inside a budget that a linear scan per frame
//! exceeds by orders of magnitude.

use mycelium_mobile_core::stage_node::{PHONE_BOUNDS, StageNode};
use mycelium_mobile_core::wire::{self, Frame};
use std::time::{Duration, Instant};

fn golden_frame(name: &str) -> Frame {
    let path = format!("{}/../../contracts/phone-stage-wire-golden/{name}", env!("CARGO_MANIFEST_DIR"));
    wire::decode_frame(&std::fs::read(path).unwrap()).unwrap()
}

fn locked_for(template: &Frame, path_id: &str, request_id: &str, attempt: i64, entry: &str) -> Frame {
    let mut frame = template.clone();
    let b = &mut frame.body;
    for (k, v) in [("path_id", serde_json::json!(path_id)), ("request_id", serde_json::json!(request_id)), ("path_attempt", serde_json::json!(attempt))] {
        b[k] = v.clone();
        b["manifest"][k] = v.clone();
        if k == "request_id" {
            b["build"]["request"][k] = v;
        } else {
            b["build"][k] = v;
        }
    }
    for stage in b["build"]["graph"]["stages"].as_array_mut().unwrap() {
        for p in stage["placements"].as_array_mut().unwrap() {
            if p["placement_id"] == "placement-entry" {
                p["node_id"] = serde_json::json!(entry);
            }
        }
    }
    frame
}

fn cancel_for(template: &Frame, path_id: &str, request_id: &str, attempt: i64) -> Frame {
    let mut frame = template.clone();
    frame.body["path_id"] = serde_json::json!(path_id);
    frame.body["request_id"] = serde_json::json!(request_id);
    frame.body["path_attempt"] = serde_json::json!(attempt);
    frame
}

#[test]
fn a_flood_against_full_tables_costs_bounded_work_per_frame() {
    let locked = golden_frame("01-manifest-locked.bin");
    let cancel = golden_frame("08-path-cancellation.bin");
    let mut never = |_: &_, _: &_, _: &[u8]| -> Result<Vec<u8>, String> { unreachable!() };
    let b = PHONE_BOUNDS;
    let mut node = StageNode::new("node-phone");
    // Fill: 16 creators, each at its record quota (young records), and the
    // pending table at its total bound.
    for c in 0..b.max_creators {
        let entry = format!("node-{c}");
        for i in 0..b.quota_per_creator {
            let (p, r) = (format!("p-{c}-{i}"), format!("r-{c}-{i}"));
            assert_eq!(node.on_frame_at(1.0, &entry, &locked_for(&locked, &p, &r, 1, &entry), &mut never).accepted, Some(true));
            assert_eq!(node.on_frame_at(1.0, &entry, &cancel_for(&cancel, &p, &r, 1), &mut never).accepted, Some(true));
        }
    }
    for s in 0..(b.pending_total / b.pending_per_source) {
        for i in 0..b.pending_per_source {
            node.on_frame_at(1.0, &format!("node-src-{s}"), &cancel_for(&cancel, &format!("q-{s}-{i}"), "r", 1), &mut never);
        }
    }
    // Pre-encode, so the timing measures the node, not the frame builder.
    let floods: Vec<(String, Frame)> = (0..20_000)
        .flat_map(|i| {
            let newcomer = format!("node-new-{}", i % 64);
            [
                // A new creator past the cap: refused after the slot check.
                (newcomer.clone(), locked_for(&locked, &format!("n-{i}"), &format!("rn-{i}"), 1, &newcomer)),
                // A source at its pending quota: displaces its own oldest.
                ("node-src-0".to_owned(), cancel_for(&cancel, &format!("f-{i}"), "flood", 1)),
                // A manifest whose path id has pending cancellations.
                ("node-0".to_owned(), locked_for(&locked, &format!("q-1-{}", i % 256), "r", 1, "node-0")),
            ]
        })
        .collect();
    let started = Instant::now();
    for (from, frame) in &floods {
        node.on_frame_at(2.0, from, frame, &mut never);
    }
    let elapsed = started.elapsed();
    eprintln!("flood of {} frames against full tables: {elapsed:?}", floods.len());
    assert!(elapsed < Duration::from_secs(10), "{elapsed:?}");
}
