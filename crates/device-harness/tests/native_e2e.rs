// SPDX-License-Identifier: AGPL-3.0-only
//! Whole native stack on this machine: real private files, real loopback sockets, the real
//! `NativeIo`/`Adapter` session logic, a local fixture standing in for the app (it writes the
//! launch form, mints the cookie and answers `/api/status`). Only the process/listener probe
//! is faked (the real one is exercised in native_os.rs). This is NOT product acceptance:
//! no packaged candidate, no device, no release receipt.
mod support;
use budgets::{BudgetId, Bytes, Entries, Span};
use device_harness::{
    Result,
    evidence_inputs::{Content, Inputs, Pins},
    native::RealClock,
    native_files::{Admitted, CONTROL, LAUNCH, NativeFiles, SESSION},
    native_io::{FileLimits, NativeIo},
    native_os::Probe,
    native_session::{Adapter, Binding},
    native_wire::NativeWire,
    preflight::ProcessIdentity,
    stranger::Candidate,
};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    os::unix::fs::{MetadataExt, PermissionsExt},
    sync::{
        Arc, Mutex,
        atomic::{AtomicBool, AtomicUsize, Ordering},
    },
    time::Duration,
};
use support::PrivateRoot;

fn inputs() -> Inputs {
    let reference = |name: &str| json!({"path":format!("/fixture/{name}"),"path_base":"absolute","size_bytes":1,"sha256":"a".repeat(64)});
    let n=serde_json::to_vec(&json!({"schema":"mycelium.foundation.native-input-evidence.v1","rust_candidate":{"source_commit":"b".repeat(40)},"native_inputs":[reference("libmlxc.dylib"),reference("libmlx.dylib"),reference("libjaccl.dylib"),reference("mlx.metallib")]})).unwrap();
    let o=serde_json::to_vec(&json!({"schema":"mycelium.foundation.oracle-evidence.v1","rust_candidate_source":"b".repeat(40),"accepted_source":{"commit":"c".repeat(40)},"certified_executable":reference("oracle"),"published_zip":reference("oracle.zip")})).unwrap();
    let id = |b: &[u8]| Content {
        size_bytes: b.len() as u64,
        sha256: format!("{:x}", Sha256::digest(b)),
    };
    Inputs::parse(
        &n,
        &o,
        Pins {
            native: id(&n),
            oracle: id(&o),
        },
    )
    .unwrap()
}
struct Fixed(u16);
impl Probe for Fixed {
    fn process(&mut self, pid: u32, _: Span) -> Result<ProcessIdentity> {
        Ok(ProcessIdentity {
            pid,
            birth: "Wed 28 Oct 05:19:01 2026".into(),
            executable: "/fixture/Mycelium.app/Contents/MacOS/mycelium".into(),
        })
    }
    fn listener_port(&mut self, _: u32, _: Span) -> Result<u16> {
        Ok(self.0)
    }
}
#[derive(Default)]
struct App {
    renewals: AtomicUsize,
    status: AtomicUsize,
    expire: AtomicBool,
    stop: AtomicBool,
    log: Mutex<Vec<String>>,
}
fn serve(listener: TcpListener, app: Arc<App>, root: std::path::PathBuf, port: u16) {
    for conn in listener.incoming() {
        let Ok(mut conn) = conn else { return };
        if app.stop.load(Ordering::SeqCst) {
            return;
        }
        conn.set_read_timeout(Some(Duration::from_millis(
            BudgetId::WorkerStartup
                .span(Entries::ZERO)
                .unwrap()
                .millis(),
        )))
        .unwrap();
        let mut raw = vec![];
        let mut buf = [0u8; 4096];
        loop {
            let n = conn.read(&mut buf).unwrap_or(0);
            raw.extend_from_slice(&buf[..n]);
            let text = String::from_utf8_lossy(&raw);
            if n == 0 || text.contains("\r\n\r\n") && body_done(&text) {
                break;
            }
        }
        let text = String::from_utf8_lossy(&raw).to_string();
        let line = text.lines().next().unwrap_or("").to_string();
        app.log.lock().unwrap().push(line.clone());
        let reply = |status: &str, headers: &str, body: &str| {
            format!(
                "HTTP/1.1 {status}\r\n{headers}Content-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            )
        };
        let out = if line.starts_with("POST /native-session") {
            app.renewals.fetch_add(1, Ordering::SeqCst);
            if !text.contains("X-Native-Authorization: native-fixture\r\n") {
                reply("403 Forbidden", "", r#"{"error":"native_refused"}"#)
            } else {
                let html = format!(
                    "<!doctype html><form method=\"post\" action=\"http://127.0.0.1:{port}/session\"><input type=\"hidden\" name=\"launch\" value=\"launch-fixture\"><button type=\"submit\">Open private demo</button></form>"
                );
                let path = root.join(LAUNCH);
                std::fs::write(&path, html).unwrap();
                std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
                app.expire.store(false, Ordering::SeqCst);
                reply("200 OK", "", r#"{"launch_file_ready":true}"#)
            }
        } else if line.starts_with("POST /session") {
            if text.ends_with("launch=launch-fixture") {
                reply(
                    "303 See Other",
                    "Location: /\r\nSet-Cookie: a13session=session-fixture; HttpOnly; SameSite=Strict\r\n",
                    "",
                )
            } else {
                reply("403 Forbidden", "", r#"{"error":"launch_expired"}"#)
            }
        } else if line.starts_with("GET /api/status") {
            app.status.fetch_add(1, Ordering::SeqCst);
            if app.expire.load(Ordering::SeqCst)
                || !text.contains("Cookie: a13session=session-fixture\r\n")
            {
                reply(
                    "401 Unauthorized",
                    "",
                    r#"{"error":"open_from_native_launcher"}"#,
                )
            } else {
                reply(
                    "200 OK",
                    "Content-Type: application/json\r\n",
                    r#"{"authority":{"membership_state":"current"}}"#,
                )
            }
        } else {
            reply("404 Not Found", "", "{}")
        };
        if let Err(e) = conn.write_all(out.as_bytes()) {
            app.log.lock().unwrap().push(format!("write failed: {e}"));
        }
    }
}
fn body_done(text: &str) -> bool {
    let (head, body) = text.split_once("\r\n\r\n").unwrap();
    let want = head
        .lines()
        .find_map(|l| l.strip_prefix("Content-Length: "))
        .and_then(|v| v.parse::<usize>().ok())
        .unwrap_or(0);
    body.len() >= want
}

#[test]
fn native_files_wire_and_session_renew_cache_and_expire_against_a_local_fixture() {
    let app_root = PrivateRoot::new("e2e-app");
    let sess_root = PrivateRoot::new("e2e-sess");
    let uid = app_root.uid();
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = listener.local_addr().unwrap().port();
    let epoch = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
        - 1_000;
    let control = app_root.join(CONTROL);
    std::fs::write(
        &control,
        format!(r#"{{"port":{port},"credential":"native-fixture"}}"#),
    )
    .unwrap();
    std::fs::set_permissions(&control, std::fs::Permissions::from_mode(0o600)).unwrap();
    let state = Arc::new(App::default());
    let (s2, r2) = (state.clone(), app_root.to_path_buf());
    let server = std::thread::spawn(move || serve(listener, s2, r2, port));

    let native_inputs = inputs().native_identity().sha256.clone();
    let pid = std::process::id();
    let admitted = Admitted {
        candidate: Candidate {
            version: "0.2.0".into(),
            source_sha: "b".repeat(40),
            zip_sha256: "c".repeat(64),
            exe_sha256: "d".repeat(64),
        },
        pid,
        launch_epoch_ms: epoch,
        desktop_manifest_sha256: "e".repeat(64),
        native_inputs_sha256: native_inputs,
    };
    let binding = Binding {
        candidate: admitted.candidate.clone(),
        process: ProcessIdentity {
            pid,
            birth: "Wed 28 Oct 05:19:01 2026".into(),
            executable: "/fixture/Mycelium.app/Contents/MacOS/mycelium".into(),
        },
        launch_epoch_ms: epoch,
        desktop_manifest_sha256: admitted.desktop_manifest_sha256.clone(),
        native_inputs_sha256: admitted.native_inputs_sha256.clone(),
    };
    let files = NativeFiles::new(&app_root, &sess_root, uid, admitted, Fixed(port)).unwrap();
    let io = NativeIo::new(
        files,
        NativeWire::new(Bytes::new(65_536)).unwrap(),
        uid,
        binding.clone(),
        &inputs(),
        RealClock::default(),
        // Synthetic bounded provider configuration for this local fixture, not operational
        // defaults: the registry ids for launch/session are still pending (S25).
        FileLimits {
            control: Bytes::new(4096),
            launch: Bytes::new(16_384),
            session: Bytes::new(16_384),
        },
    )
    .unwrap();
    let mut adapter = Adapter::new(io, RealClock::default(), binding).unwrap();
    let total = Span::from_millis(20_000);

    // 1. No cached session: exactly one renewal, then status.
    let first = adapter.check(total).unwrap();
    assert_eq!(
        (first.receipt.status_http, first.receipt.renewals),
        (200, 1)
    );
    assert!(!first.receipt.release_qualified);
    assert_eq!(state.renewals.load(Ordering::SeqCst), 1);
    let session = sess_root.join(SESSION);
    let m = std::fs::metadata(&session).unwrap();
    assert_eq!(
        (m.permissions().mode() & 0o777, m.nlink(), m.uid()),
        (0o600, 1, uid)
    );
    // 2. Cached session: status only, no renewal.
    let second = adapter.check(total).unwrap();
    assert_eq!(second.receipt.renewals, 0);
    assert_eq!(state.renewals.load(Ordering::SeqCst), 1);
    // 3. Expiry: exactly one renewal, session file atomically replaced.
    let before = std::fs::metadata(&session).unwrap().ino();
    state.expire.store(true, Ordering::SeqCst);
    let third = adapter.check(total).unwrap();
    assert_eq!(third.receipt.renewals, 1);
    assert_eq!(state.renewals.load(Ordering::SeqCst), 2);
    assert_ne!(std::fs::metadata(&session).unwrap().ino(), before);
    // 4. A control file whose port no longer matches the observed listener is refused.
    std::fs::remove_file(&control).unwrap();
    std::fs::write(
        &control,
        format!(
            r#"{{"port":{},"credential":"native-fixture"}}"#,
            port.wrapping_add(1)
        ),
    )
    .unwrap();
    std::fs::set_permissions(&control, std::fs::Permissions::from_mode(0o600)).unwrap();
    let refused = adapter.check(total).err().unwrap();
    assert_eq!(refused.code, "native_observation_mismatch");
    // Nothing but the fixture's own loopback listener was contacted.
    let log = state.log.lock().unwrap().clone();
    assert!(
        log.iter().all(|l| l.starts_with("POST /native-session")
            || l.starts_with("POST /session")
            || l.starts_with("GET /api/status")),
        "{log:?}"
    );
    state.stop.store(true, Ordering::SeqCst);
    TcpStream::connect(("127.0.0.1", port)).expect("wake the fixture server");
    server.join().expect("fixture server thread");
}
