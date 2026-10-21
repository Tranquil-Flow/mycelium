// SPDX-License-Identifier: AGPL-3.0-only
//! Route neighbour for the device checkpoint: sends one HopHeader + MYAC float32
//! activation to the phone over production iroh (n0 relays / hole punching, no
//! Tailscale), waits for the phone's forwarded output, and checks it at float32
//! against this crate's reference executor run on the same float32-rounded input
//! the wire carried (the fixture output was computed from float64 inputs, so it
//! cannot be matched exactly after the MYAC float32 hop).
//!
//! `cargo run --features net --example route_peer -- <phone_endpoint_id> <generation> <fixture.json>`

use std::time::{Duration, Instant};

use mycelium_mobile_core::runtime::Gpt2BlockStage;
use mycelium_mobile_core::transport::{NodeTransport, Reachability, parse_endpoint_id};
use mycelium_mobile_core::wire::{self, Activation};
use serde_json::{Value, json};
use tokio::sync::mpsc;

fn rows(v: &Value) -> Vec<Vec<f64>> {
    v.as_array().unwrap().iter().map(|r| r.as_array().unwrap().iter().map(|x| x.as_f64().unwrap()).collect()).collect()
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args[1] == "id" {
        let peer = NodeTransport::bind(&[0x5a; 32], Reachability::LocalOnly).await.unwrap();
        println!("{}", peer.endpoint_id());
        return;
    }
    let phone = parse_endpoint_id(&args[1]).expect("phone endpoint id");
    let generation: u64 = args[2].parse().unwrap();
    let fixture: Value = serde_json::from_str(&std::fs::read_to_string(&args[3]).unwrap()).unwrap();
    let peer = NodeTransport::bind(&[0x5a; 32], Reachability::Production).await.unwrap();
    println!("{}", json!({"event": "peer_ready", "endpoint_id": peer.endpoint_id(), "relay_online": peer.online(Duration::from_secs(20)).await}));
    peer.configure_peer(phone, generation).await;
    let (tx, mut rx) = mpsc::channel(4);
    let _accept = peer.spawn_accept(tx);

    let hidden = rows(&fixture["hidden"]);
    let fixture_output = rows(&fixture["output"]);
    let wire_hidden: Vec<Vec<f64>> = hidden.iter().map(|r| r.iter().map(|x| f64::from(*x as f32)).collect()).collect();
    let expected = Gpt2BlockStage::from_document(&fixture["pack"]).unwrap().execute("route-peer-reference", &wire_hidden).unwrap();
    let payload = Activation::from_rows_f32(&hidden).encode().unwrap();
    for attempt in 0..3u32 {
        let body = json!({"request_id": format!("device-checkpoint-{attempt}"), "hop_index": 1});
        let frame = wire::encode_frame("HopHeader", &body, &payload).unwrap();
        let started = Instant::now();
        let sent = peer.send(phone.into(), generation, &frame).await;
        println!("{}", json!({"event": "sent", "attempt": attempt, "result": format!("{sent:?}"), "ms": started.elapsed().as_millis()}));
        match tokio::time::timeout(Duration::from_secs(30), rx.recv()).await {
            Ok(Some(inbound)) => {
                let out = Activation::decode(&inbound.frame.payload).unwrap().to_rows_f64().unwrap();
                let exact = out.iter().flatten().zip(expected.iter().flatten()).all(|(a, b)| *a as f32 == *b as f32);
                let max_abs_diff_vs_fixture = out.iter().flatten().zip(fixture_output.iter().flatten()).map(|(a, b)| (a - b).abs()).fold(0.0f64, f64::max);
                println!("{}", json!({
                    "event": "forwarded_output",
                    "attempt": attempt,
                    "round_trip_ms": started.elapsed().as_millis(),
                    "hop_index": inbound.frame.body["hop_index"],
                    "float32_identical_to_reference_on_wire_input": exact,
                    "max_abs_diff_vs_float64_fixture": max_abs_diff_vs_fixture,
                }));
            }
            other => println!("{}", json!({"event": "no_output", "attempt": attempt, "detail": format!("{other:?}")})),
        }
    }
    peer.close().await;
}
