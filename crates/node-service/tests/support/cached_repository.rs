// SPDX-License-Identifier: AGPL-3.0-only
//! A loopback model repository over the locally CACHED Hugging Face snapshot of the run model
//! (Qwen/Qwen3-0.6B @ c1899de2): the hub's revision document (`siblings` with each file's
//! size and its LFS sha256 or git blob id, read from the cache's content-addressed blob names)
//! and `resolve` file bytes, streamed from the cache with `Range` support. Only the ORIGIN is a
//! test's: the packaged acquisition downloads, hashes and verifies every byte as it does from
//! huggingface.co (`HttpTransport::loopback_for_tests`, the oracle's test-only constructor seam).
//! Nothing is downloaded from the internet; a cache file that is missing fails the test by name.
#![allow(dead_code)] // each test binary uses its own part of these helpers
use api::acquisition::{HttpTransport, Transport};
use serde_json::{Value, json};
use std::{
    io::{Read, Seek, SeekFrom, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, AtomicU64, Ordering},
    },
    thread::JoinHandle,
    time::Duration,
};

pub const MODEL_ID: &str = "Qwen/Qwen3-0.6B";
pub const COMMIT: &str = "c1899de289a04d12100db370d81485cdf75e47ca";

/// One file of the cached snapshot: its repository path, size, and where its bytes are.
#[derive(Clone, Debug)]
pub struct CachedFile {
    pub path: String,
    pub size: u64,
    /// `sha256` (LFS) or `git-sha1` (a plain git blob), from the cache's blob name.
    pub algorithm: &'static str,
    pub digest: String,
    pub blob: PathBuf,
}

/// The cached snapshot directory of the run model (`~/.cache/huggingface/hub/...`).
pub fn snapshot_dir() -> PathBuf {
    let home = std::env::var_os("HOME").expect("HOME");
    PathBuf::from(home)
        .join(".cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots")
        .join(COMMIT)
}

/// Every file of the cached snapshot, sorted by path.
pub fn cached_files() -> Vec<CachedFile> {
    let dir = snapshot_dir();
    let mut files = Vec::new();
    for entry in std::fs::read_dir(&dir)
        .unwrap_or_else(|e| panic!("the cached snapshot {} is missing: {e}", dir.display()))
    {
        let entry = entry.unwrap();
        let path = entry.file_name().into_string().unwrap();
        let blob = std::fs::canonicalize(entry.path())
            .unwrap_or_else(|e| panic!("cached file {path} has no blob: {e}"));
        let digest = blob.file_name().unwrap().to_str().unwrap().to_owned();
        let algorithm = match digest.len() {
            64 => "sha256",
            40 => "git-sha1",
            _ => panic!("cached blob name of {path} is not a digest: {digest}"),
        };
        let size = std::fs::metadata(&blob).unwrap().len();
        files.push(CachedFile {
            path,
            size,
            algorithm,
            digest,
            blob,
        });
    }
    files.sort_by(|a, b| a.path.cmp(&b.path));
    for required in ["config.json", "model.safetensors", "tokenizer.json"] {
        assert!(
            files.iter().any(|f| f.path == required),
            "the cached snapshot lacks {required}"
        );
    }
    files
}

/// A loopback repository serving the cached snapshot, one thread per connection.
pub struct CachedRepository {
    origin: String,
    stop: Arc<AtomicBool>,
    served: Arc<AtomicU64>,
    server: Option<JoinHandle<()>>,
}
impl CachedRepository {
    pub fn start() -> Self {
        let files = Arc::new(cached_files());
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://127.0.0.1:{}", listener.local_addr().unwrap().port());
        let stop = Arc::new(AtomicBool::new(false));
        let served = Arc::new(AtomicU64::new(0));
        let (stopping, counting) = (stop.clone(), served.clone());
        let server = std::thread::spawn(move || {
            let mut connections = Vec::new();
            while !stopping.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        let (files, counting) = (files.clone(), counting.clone());
                        connections
                            .push(std::thread::spawn(move || serve(stream, &files, &counting)));
                    }
                    // Readiness poll of a nonblocking listener (no connection yet).
                    Err(_) => std::thread::sleep(Duration::from_millis(10)),
                }
                connections.retain(|c: &JoinHandle<()>| !c.is_finished());
            }
            for connection in connections {
                connection.join().unwrap_or_default();
            }
        });
        Self {
            origin,
            stop,
            served,
            server: Some(server),
        }
    }
    pub fn transport(&self) -> Arc<dyn Transport> {
        Arc::new(HttpTransport::loopback_for_tests(&self.origin))
    }
    /// Body bytes served so far (both Macs).
    pub fn served_bytes(&self) -> u64 {
        self.served.load(Ordering::SeqCst)
    }
}
impl Drop for CachedRepository {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(server) = self.server.take() {
            server.join().unwrap_or_default();
        }
    }
}

fn serve(mut stream: TcpStream, files: &[CachedFile], served: &AtomicU64) {
    stream.set_nonblocking(false).unwrap_or_default();
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        match stream.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => return,
        }
    }
    let text = String::from_utf8_lossy(&head).into_owned();
    let target = text.split(' ').nth(1).unwrap_or_default().to_owned();
    let range_start = text
        .lines()
        .find_map(|l| {
            let (name, value) = l.split_once(':')?;
            name.trim()
                .eq_ignore_ascii_case("range")
                .then(|| value.trim().to_owned())
        })
        .and_then(|v| {
            v.strip_prefix("bytes=")?
                .split('-')
                .next()?
                .parse::<u64>()
                .ok()
        });
    if target == format!("/api/models/{MODEL_ID}/revision/{COMMIT}?blobs=true") {
        let siblings: Vec<Value> = files
            .iter()
            .map(|f| match f.algorithm {
                "sha256" => json!({"rfilename": f.path, "size": f.size,
                    "lfs": {"size": f.size, "sha256": f.digest}}),
                _ => json!({"rfilename": f.path, "size": f.size, "blobId": f.digest}),
            })
            .collect();
        let body = json!({"id": MODEL_ID, "modelId": MODEL_ID, "sha": COMMIT,
            "private": false, "siblings": siblings})
        .to_string();
        let reply = format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        stream.write_all(reply.as_bytes()).unwrap_or_default();
        return;
    }
    let file = target
        .strip_prefix(&format!("/{MODEL_ID}/resolve/{COMMIT}/"))
        .map(|rest| rest.split('?').next().unwrap_or_default())
        .and_then(|path| files.iter().find(|f| f.path == path));
    let Some(file) = file else {
        stream
            .write_all(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            .unwrap_or_default();
        return;
    };
    let start = range_start.unwrap_or(0).min(file.size);
    let head = match range_start {
        Some(_) => format!(
            "HTTP/1.1 206 Partial Content\r\nContent-Length: {}\r\nContent-Range: bytes {start}-{}/{}\r\nConnection: close\r\n\r\n",
            file.size - start,
            file.size.saturating_sub(1),
            file.size
        ),
        None => format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            file.size
        ),
    };
    if stream.write_all(head.as_bytes()).is_err() {
        return;
    }
    let mut source = std::fs::File::open(&file.blob).unwrap();
    source.seek(SeekFrom::Start(start)).unwrap();
    let mut buffer = vec![0u8; 1 << 20];
    loop {
        match source.read(&mut buffer) {
            Ok(0) | Err(_) => return,
            Ok(n) => {
                if stream.write_all(&buffer[..n]).is_err() {
                    return;
                }
                served.fetch_add(n as u64, Ordering::SeqCst);
            }
        }
    }
}
