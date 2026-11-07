// SPDX-License-Identifier: AGPL-3.0-only
//! Controlled model-free repository, shared unchanged oracle layout.
use super::*;
const MANIFEST_FIXTURE: &str = include_str!("../fixtures/oracle_manifest.json");

pub(super) fn tiny_snapshot() -> (Vec<(String, Vec<u8>, bool)>, Value) {
    // The accepted `qwen2_monolithic` layout of the executed manifest oracle: the real tensor names, BF16 [1].
    let doc: Value = serde_json::from_str(MANIFEST_FIXTURE).unwrap();
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
    let (mut off, mut data) = (0usize, Vec::new());
    for (i, t) in tensors.iter().enumerate() {
        header.insert(
            t[0].as_str().unwrap().to_owned(),
            json!({"dtype": "BF16", "shape": [1], "data_offsets": [off, off + 2]}),
        );
        data.extend((0..2).map(|j| ((seed + i + j) % 251) as u8));
        off += 2;
    }
    let header = serde_json::to_vec(&Value::Object(header)).unwrap();
    let mut weights = (header.len() as u64).to_le_bytes().to_vec();
    weights.extend(header);
    weights.extend(data);
    (
        vec![
            ("config.json".to_owned(), config.into_bytes(), false),
            ("model.safetensors".to_owned(), weights, true),
        ],
        doc["cases"][0]["files"]["config.json"].clone(),
    )
}
/// A one-thread HTTP repository: the revision document and the `resolve` files of the registered model.
pub(super) fn serve_repository(
    files: Vec<(String, Vec<u8>, bool)>,
) -> (
    u16,
    std::sync::Arc<std::sync::atomic::AtomicBool>,
    std::thread::JoinHandle<()>,
) {
    use sha2::{Digest, Sha256};
    use std::sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    };
    let model = &api::model_catalog::registered()[1];
    let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let stop = Arc::new(AtomicBool::new(false));
    let flag = stop.clone();
    let (id, commit) = (model.model_id.to_owned(), model.resolved_commit.to_owned());
    let handle = std::thread::spawn(move || {
        while !flag.load(Ordering::SeqCst) {
            let Ok((mut stream, _)) = listener.accept() else {
                std::thread::sleep(budget(BudgetId::OwnerPoll) / 50);
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
            let meta = format!("/api/models/{id}/revision/{commit}?blobs=true");
            let reply: Vec<u8> = if target == meta {
                let siblings: Vec<Value> = files
                    .iter()
                    .map(|(path, body, lfs)| {
                        if *lfs {
                            json!({"rfilename": path, "size": body.len(),
                                "lfs": {"size": body.len(), "sha256": format!("{:x}", Sha256::digest(body))}})
                        } else {
                            let mut h = sha1::Sha1::new();
                            sha1::Digest::update(&mut h, format!("blob {}\0", body.len()).as_bytes());
                            sha1::Digest::update(&mut h, body);
                            let blob: String = sha1::Digest::finalize(h).iter().map(|b| format!("{b:02x}")).collect();
                            json!({"rfilename": path, "size": body.len(), "blobId": blob})
                        }
                    })
                    .collect();
                let doc = json!({"id": id, "modelId": id, "sha": commit, "private": false, "siblings": siblings}).to_string();
                format!("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{doc}", doc.len()).into_bytes()
            } else if let Some(rel) = target.strip_prefix(&format!("/{id}/resolve/{commit}/")) {
                match files
                    .iter()
                    .find(|(p, _, _)| p == rel.split('?').next().unwrap_or_default())
                {
                    Some((_, body, _)) => {
                        let mut out = format!(
                            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                            body.len()
                        )
                        .into_bytes();
                        out.extend_from_slice(body);
                        out
                    }
                    None => {
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                            .to_vec()
                    }
                }
            } else {
                b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".to_vec()
            };
            stream.write_all(&reply).unwrap_or_default();
        }
    });
    (port, stop, handle)
}
