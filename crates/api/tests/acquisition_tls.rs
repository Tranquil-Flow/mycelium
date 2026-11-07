// SPDX-License-Identifier: AGPL-3.0-only
//! The acquisition's HTTPS path (rustls, certificate verification, the 302 hop to a signed CDN url, identity
//! `Content-Length` bodies and a chunked metadata document) against a loopback TLS server that presents the
//! recorded `r12 loopback seed` certificate (IP SAN 127.0.0.1, valid to 2036), with that recorded CA as the only
//! root. Production trusts the system roots; nothing here reaches the internet. Expected bytes: the same
//! deterministic stream the executed-oracle scenarios use (`oracle_acquisition.json` `happy_path` tree).
use api::{
    acquisition::{Acquisition, HttpTransport, Options, PrepareError, Preparer},
    model_catalog,
};
use rustls::{ServerConfig, ServerConnection, StreamOwned};
use rustls_pki_types::{CertificateDer, PrivateKeyDer, pem::PemObject};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    fs,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::{DirBuilderExt, PermissionsExt},
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    thread::JoinHandle,
    time::Duration,
};

const TLS_FIXTURE: &str = include_str!("fixtures/oracle_seed_http.json");

fn poll() -> Duration {
    Duration::from_millis(
        budgets::BudgetId::OwnerPoll
            .span(budgets::Entries::new(0))
            .unwrap()
            .millis(),
    )
}
fn stream_bytes(path: &str, n: usize) -> Vec<u8> {
    let mut out = Vec::new();
    let mut i = 0;
    while out.len() < n {
        out.extend_from_slice(&Sha256::digest(format!("{path}/{i}").as_bytes()));
        i += 1;
    }
    out.truncate(n);
    out
}
struct Accept;
impl Preparer for Accept {
    fn prepare(&self, _: &Path, _: &model_catalog::Model) -> Result<(), PrepareError> {
        Ok(())
    }
}
fn serve(
    stream: TcpStream,
    config: Arc<ServerConfig>,
    model: &model_catalog::Model,
    files: &[(&str, usize, bool)],
) {
    let mut tls = StreamOwned::new(ServerConnection::new(config).unwrap(), stream);
    let mut head = Vec::new();
    let mut byte = [0u8; 1];
    while !head.ends_with(b"\r\n\r\n") {
        match tls.read(&mut byte) {
            Ok(1) => head.push(byte[0]),
            _ => return,
        }
    }
    let text = String::from_utf8_lossy(&head).into_owned();
    let target = text.split(' ').nth(1).unwrap().to_owned();
    let meta = format!(
        "/api/models/{}/revision/{}?blobs=true",
        model.model_id, model.resolved_commit
    );
    let reply: Vec<u8> = if target == meta {
        let siblings: Vec<Value> = files
            .iter()
            .map(|(path, n, lfs)| {
                let body = stream_bytes(path, *n);
                if *lfs {
                    json!({"rfilename": path, "size": n,
                        "lfs": {"size": n, "sha256": format!("{:x}", Sha256::digest(&body))}})
                } else {
                    let mut h = sha1::Sha1::new();
                    sha1::Digest::update(&mut h, format!("blob {}\0", body.len()).as_bytes());
                    sha1::Digest::update(&mut h, &body);
                    let id: String = sha1::Digest::finalize(h)
                        .iter()
                        .map(|b| format!("{b:02x}"))
                        .collect();
                    json!({"rfilename": path, "size": n, "blobId": id})
                }
            })
            .collect();
        let doc =
            json!({"id": model.model_id, "modelId": model.model_id, "sha": model.resolved_commit,
            "private": false, "siblings": siblings})
            .to_string();
        // Chunked, 11 bytes at a time: the decoder must reassemble it.
        let mut out = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n".to_vec();
        for piece in doc.as_bytes().chunks(11) {
            out.extend(format!("{:x}\r\n", piece.len()).into_bytes());
            out.extend_from_slice(piece);
            out.extend_from_slice(b"\r\n");
        }
        out.extend_from_slice(b"0\r\n\r\n");
        out
    } else if let Some(rest) = target.strip_prefix(&format!(
        "/{}/resolve/{}/",
        model.model_id, model.resolved_commit
    )) {
        // The hub answers a file with a redirect to a signed CDN url; the redirected path serves the bytes.
        let rel = rest.split('?').next().unwrap();
        format!("HTTP/1.1 302 Found\r\nLocation: /cdn/{rel}?sig=1\r\nContent-Length: 0\r\n\r\n")
            .into_bytes()
    } else if let Some(rest) = target.strip_prefix("/cdn/") {
        let rel = rest.split('?').next().unwrap();
        let (_, n, _) = files.iter().find(|(p, _, _)| *p == rel).unwrap();
        let mut out = format!("HTTP/1.1 200 OK\r\nContent-Length: {n}\r\n\r\n").into_bytes();
        out.extend(stream_bytes(rel, *n));
        out
    } else {
        b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n".to_vec()
    };
    tls.write_all(&reply).unwrap_or_default();
    tls.flush().unwrap_or_default();
    tls.conn.send_close_notify();
    tls.flush().unwrap_or_default();
}

#[test]
fn a_model_snapshot_downloads_over_verified_tls_with_a_redirect_and_a_chunked_manifest() {
    let doc: Value = serde_json::from_str(TLS_FIXTURE).unwrap();
    let certs: Vec<CertificateDer<'static>> =
        CertificateDer::pem_slice_iter(doc["server_cert_pem"].as_str().unwrap().as_bytes())
            .map(Result::unwrap)
            .collect();
    let key =
        PrivateKeyDer::from_pem_slice(doc["server_key_pem"].as_str().unwrap().as_bytes()).unwrap();
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = Arc::new(
        ServerConfig::builder_with_provider(provider)
            .with_safe_default_protocol_versions()
            .unwrap()
            .with_no_client_auth()
            .with_single_cert(certs, key)
            .unwrap(),
    );
    let model = &model_catalog::registered()[1];
    let files: Vec<(&str, usize, bool)> = vec![
        ("config.json", 300, false),
        ("model.safetensors", 70_000, true),
        ("sub/tokenizer.json", 1_500, false),
    ];
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let stop = Arc::new(AtomicBool::new(false));
    let server: JoinHandle<()> = {
        let (config, stop, files) = (config.clone(), stop.clone(), files.clone());
        std::thread::spawn(move || {
            let mut workers: Vec<JoinHandle<()>> = Vec::new();
            while !stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        let (config, files) = (config.clone(), files.clone());
                        workers.push(std::thread::spawn(move || {
                            serve(stream, config, &model_catalog::registered()[1], &files)
                        }));
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(poll())
                    }
                    Err(_) => break,
                }
            }
            for worker in workers {
                worker.join().unwrap();
            }
        })
    };
    let root = fs::canonicalize(std::env::temp_dir())
        .unwrap()
        .join(format!("l10-acq-tls-{}", std::process::id()));
    fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
    fs::set_permissions(&root, fs::Permissions::from_mode(0o700)).unwrap();
    let transport = HttpTransport::loopback_for_tests(&format!("https://127.0.0.1:{port}"))
        .with_tls_roots_pem(doc["ca_pem"].as_str().unwrap().as_bytes());
    let acquisition = Acquisition::open(
        &root,
        model,
        Options {
            transport: Arc::new(transport),
            preparer: Arc::new(Accept),
            disk_free: Box::new(api::acquisition::disk_free_bytes),
            clock_unix_ms: Box::new(api::acquisition::system_clock_ms),
            observer: None,
        },
    )
    .unwrap();
    let grant = json!({"download_acknowledged": true, "max_download_bytes": 10_000_000, "storage_limit_bytes": 10_000_000});
    let status = acquisition.run_blocking(&grant).unwrap();
    assert_eq!(status.state, "prepared", "{status:?}");
    assert_eq!(
        (status.downloaded_bytes, status.total_bytes),
        (71_800, 71_800)
    );
    let snapshot = root
        .join("models--Qwen--Qwen2.5-0.5B-Instruct/snapshots")
        .join(model.resolved_commit);
    for (path, n, _) in &files {
        assert_eq!(
            fs::read(snapshot.join(path)).unwrap(),
            stream_bytes(path, *n),
            "{path}"
        );
    }
    acquisition.close().unwrap();
    stop.store(true, Ordering::SeqCst);
    server.join().unwrap();

    // The same server with a client that trusts no root: the handshake is refused, nothing is stored.
    let strict_root = root.with_extension("strict");
    fs::DirBuilder::new()
        .mode(0o700)
        .create(&strict_root)
        .unwrap();
    fs::set_permissions(&strict_root, fs::Permissions::from_mode(0o700)).unwrap();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let port = listener.local_addr().unwrap().port();
    let stop = Arc::new(AtomicBool::new(false));
    let server: JoinHandle<()> = {
        let (config, stop, files) = (config.clone(), stop.clone(), files.clone());
        std::thread::spawn(move || {
            while !stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((stream, _)) => {
                        stream.set_nonblocking(false).unwrap();
                        serve(
                            stream,
                            config.clone(),
                            &model_catalog::registered()[1],
                            &files,
                        );
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {
                        std::thread::sleep(poll())
                    }
                    Err(_) => break,
                }
            }
        })
    };
    let untrusting = HttpTransport::loopback_for_tests(&format!("https://127.0.0.1:{port}"))
        .with_tls_roots_pem(b"");
    let refused = Acquisition::open(
        &strict_root,
        model,
        Options {
            transport: Arc::new(untrusting),
            preparer: Arc::new(Accept),
            disk_free: Box::new(api::acquisition::disk_free_bytes),
            clock_unix_ms: Box::new(api::acquisition::system_clock_ms),
            observer: None,
        },
    )
    .unwrap();
    let status = refused.run_blocking(&grant).unwrap();
    assert_eq!(
        (status.state.as_str(), status.error_code.as_deref()),
        ("failed", Some("network_error"))
    );
    assert!(
        !strict_root
            .join("models--Qwen--Qwen2.5-0.5B-Instruct/snapshots")
            .join(model.resolved_commit)
            .exists()
    );
    refused.close().unwrap();
    stop.store(true, Ordering::SeqCst);
    server.join().unwrap();
    fs::remove_dir_all(&root).unwrap();
    fs::remove_dir_all(&strict_root).unwrap();
}
