// SPDX-License-Identifier: AGPL-3.0-only
//! A loopback model repository serving a tiny snapshot of the pinned run model, and an owned
//! executable file standing for the installed native sidecar (never started): the model-free
//! fixtures of the packaged owner-run tests (the same as `packaged_owner_run.rs`'s).
#![allow(dead_code)] // each test binary uses its own part of these helpers
use crate::macs::millis;
use api::acquisition::{HttpTransport, Transport};
use budgets::BudgetId as B;
use serde_json::{Value, json};
use std::{
    io::{Read, Write},
    net::TcpListener,
    os::unix::fs::PermissionsExt,
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
};

pub const MANIFEST_ORACLE: &str = include_str!("../../../api/tests/fixtures/oracle_manifest.json");
/// v0.1.4's owner acquisition root of the pinned run model (`'model-source-' + resolved_commit[:12]`).
pub const SOURCE: &str = "model-source-7ae557604adf";

/// `config.json` + `model.safetensors` (BF16 [1] tensors) of the manifest oracle's accepted case.
pub fn tiny_snapshot() -> Vec<(String, Vec<u8>)> {
    let doc: Value = serde_json::from_str(MANIFEST_ORACLE).unwrap();
    let case = doc["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["label"] == "qwen2_monolithic")
        .unwrap();
    let config = case["files"]["config.json"]["text"]
        .as_str()
        .unwrap()
        .to_owned();
    let spec = &case["files"]["model.safetensors"];
    let mut tensors: Vec<&Value> = spec["tensors"].as_array().unwrap().iter().collect();
    tensors.sort_by(|a, b| a[0].as_str().cmp(&b[0].as_str()));
    let seed = spec["seed"].as_u64().unwrap() as usize;
    let mut header = serde_json::Map::new();
    let (mut offset, mut data) = (0usize, Vec::new());
    for (i, tensor) in tensors.iter().enumerate() {
        header.insert(
            tensor[0].as_str().unwrap().to_owned(),
            json!({"dtype": "BF16", "shape": [1], "data_offsets": [offset, offset + 2]}),
        );
        data.extend((0..2).map(|j| ((seed + i + j) % 251) as u8));
        offset += 2;
    }
    let header = serde_json::to_vec(&Value::Object(header)).unwrap();
    let mut weights = (header.len() as u64).to_le_bytes().to_vec();
    weights.extend(header);
    weights.extend(data);
    vec![
        ("config.json".into(), config.into_bytes()),
        ("model.safetensors".into(), weights),
    ]
}

/// A one-thread loopback repository of the pinned model (revision document + `resolve` files).
pub struct Repository {
    origin: String,
    stop: Arc<AtomicBool>,
    server: Option<JoinHandle<()>>,
}
impl Repository {
    pub fn start() -> Self {
        let files = tiny_snapshot();
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let stop = Arc::new(AtomicBool::new(false));
        let stopping = stop.clone();
        let server = std::thread::spawn(move || {
            let model = &api::model_catalog::registered()[1];
            let (id, commit) = (model.model_id, model.resolved_commit);
            while !stopping.load(Ordering::SeqCst) {
                let Ok((mut stream, _)) = listener.accept() else {
                    std::thread::sleep(millis(B::OwnerPoll) / 50);
                    continue;
                };
                stream.set_nonblocking(false).unwrap();
                let mut head = Vec::new();
                let mut byte = [0u8; 1];
                while !head.ends_with(b"\r\n\r\n") {
                    match stream.read(&mut byte) {
                        Ok(1) => head.push(byte[0]),
                        _ => break,
                    }
                }
                let text = String::from_utf8_lossy(&head).into_owned();
                let target = text.split(' ').nth(1).unwrap_or_default().to_owned();
                let body = if target == format!("/api/models/{id}/revision/{commit}?blobs=true") {
                    let siblings: Vec<Value> = files
                        .iter()
                        .map(|(path, bytes)| {
                            let sha = protocol::canonical::sha256_prefixed(bytes)[7..].to_owned();
                            json!({"rfilename": path, "size": bytes.len(),
                                "lfs": {"size": bytes.len(), "sha256": sha}})
                        })
                        .collect();
                    Some(
                        json!({"id": id, "modelId": id, "sha": commit, "private": false, "siblings": siblings})
                            .to_string()
                            .into_bytes(),
                    )
                } else if let Some(rest) = target.strip_prefix(&format!("/{id}/resolve/{commit}/"))
                {
                    let path = rest.split('?').next().unwrap_or_default();
                    files
                        .iter()
                        .find(|(p, _)| p == path)
                        .map(|(_, b)| b.clone())
                } else {
                    None
                };
                let reply = match body {
                    Some(body) => {
                        let mut out = format!(
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes();
                        out.extend(body);
                        out
                    }
                    None => {
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            .to_vec()
                    }
                };
                stream.write_all(&reply).unwrap_or_default();
            }
        });
        Self {
            origin,
            stop,
            server: Some(server),
        }
    }
    pub fn transport(&self) -> Arc<dyn Transport> {
        Arc::new(HttpTransport::loopback_for_tests(&self.origin))
    }
}
impl Drop for Repository {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(server) = self.server.take() {
            server.join().unwrap();
        }
    }
}

/// An owned, executable regular file standing for the installed native sidecar: the descriptor
/// names it; nothing here ever starts it (`installed_sidecar`, native_runtime.py:8-22).
pub fn sidecar_file(base: &Path) -> PathBuf {
    let path = base.join("mycelium-iroh-sidecar");
    std::fs::write(&path, b"#!/bin/sh\nexit 1\n").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    path
}
