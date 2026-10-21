// SPDX-License-Identifier: AGPL-3.0-only
//! wp2-6 runtime check on the real sidecar binary: what the `ready` event says.
//!
//! The unit-level gate is exercised in `tests/ready_address.rs`; this target
//! runs the built binary in `--local-only` mode (relays disabled, loopback
//! only - no relay, no public hostname, nothing off-host) and pins the wire
//! contract the launcher and `scripts/a8_endpoint_mismatch_probe.py` rely on:
//!
//! * the ready record keeps exactly `{event, endpoint_id, endpoint_addr, alpn}`;
//! * its address set is not empty;
//! * the sidecar **records its address-readiness decision** instead of
//!   publishing an unexamined snapshot (defect 1: the address was captured
//!   right after `bind()` and never revisited).

use std::fs;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, ChildStderr, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::{Duration, SystemTime};

const BINARY: &str = env!("CARGO_BIN_EXE_mycelium-iroh-sidecar");
const ALPN: &str = "mycelium.iroh.sidecar.v1";
const READY_WAIT: Duration = Duration::from_secs(20);
const SECRET: [u8; 32] = [7_u8; 32];

struct RunningSidecar {
    child: Child,
    lines: mpsc::Receiver<String>,
    stderr: Option<ChildStderr>,
    root: PathBuf,
}

impl RunningSidecar {
    fn start() -> Self {
        let nonce = SystemTime::now()
            .duration_since(SystemTime::UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        let root =
            PathBuf::from("/tmp").join(format!("mycelium-wp2-6-{}-{nonce}", std::process::id()));
        fs::create_dir_all(root.join("run")).expect("create socket root");
        let socket = root.join("run").join("sidecar.sock");

        let mut child = Command::new(BINARY)
            .arg("--uds")
            .arg(&socket)
            // fd 0 is a pipe carrying the bootstrap secret; the sidecar reads
            // exactly 32 bytes from it and never echoes them.
            .arg("--bootstrap-fd")
            .arg("0")
            .arg("--local-only")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn the sidecar binary");
        child
            .stdin
            .take()
            .expect("stdin pipe")
            .write_all(&SECRET)
            .expect("hand the bootstrap secret to the sidecar");

        let stdout = child.stdout.take().expect("stdout pipe");
        let (sender, lines) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                match line {
                    Ok(line) => {
                        if sender.send(line).is_err() {
                            return;
                        }
                    }
                    Err(_) => return,
                }
            }
        });

        Self {
            stderr: child.stderr.take(),
            child,
            lines,
            root,
        }
    }

    fn next_line(&self) -> String {
        self.lines
            .recv_timeout(READY_WAIT)
            .expect("the sidecar must publish its ready record")
    }

    fn next_unsolicited_line(&mut self) -> Option<String> {
        self.lines.try_recv().ok()
    }

    /// Stop the child and return everything it wrote to stderr.
    fn stop(mut self) -> String {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let mut stderr = String::new();
        if let Some(mut pipe) = self.stderr.take() {
            let _ = pipe.read_to_string(&mut stderr);
        }
        let _ = fs::remove_dir_all(&self.root);
        stderr
    }
}

fn ready_document(sidecar: &mut RunningSidecar) -> serde_json::Value {
    let line = sidecar.next_line();
    serde_json::from_str(&line).expect("the ready record is canonical JSON")
}

#[test]
fn local_only_ready_record_keeps_its_shape_and_a_non_empty_address_set() {
    let mut sidecar = RunningSidecar::start();
    let ready = ready_document(&mut sidecar);

    // The launcher and scripts/a8_endpoint_mismatch_probe.py compare this exact
    // key set, so the readiness work must not add fields to the ready record.
    let mut keys = ready
        .as_object()
        .expect("ready is an object")
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    keys.sort();
    assert_eq!(keys, ["alpn", "endpoint_addr", "endpoint_id", "event"]);
    assert_eq!(ready["event"], "ready");
    assert_eq!(ready["alpn"], ALPN);

    let endpoint_id = ready["endpoint_id"].as_str().expect("endpoint id");
    let address = ready["endpoint_addr"].as_object().expect("endpoint addr");
    assert_eq!(address["id"], endpoint_id);
    assert_eq!(
        address["id"], ready["endpoint_addr"]["id"],
        "the advertised identity must be the endpoint's own"
    );

    let addrs = address["addrs"].as_array().expect("address list");
    assert!(
        !addrs.is_empty(),
        "the ready event must not advertise an empty address set: {ready}"
    );
    // `--local-only` disables relays and clears IP transports except the
    // loopback bind, so every advertised entry is a direct IP address.
    for entry in addrs {
        let object = entry.as_object().expect("transport address object");
        assert!(
            object.contains_key("Ip"),
            "local-only endpoints advertise IP transports only: {entry}"
        );
    }

    // Nothing else is written before the launcher reads the ready record: the
    // address is not re-advertised unless it actually changes.
    assert_eq!(
        sidecar.next_unsolicited_line(),
        None,
        "an unchanged address must not be re-advertised"
    );

    let stderr = sidecar.stop();
    assert!(
        stderr.contains("address_ready"),
        "the sidecar must record the address-readiness decision it made before \
         publishing ready; stderr was: {stderr:?}"
    );
    assert!(
        !stderr.contains("ready_address_incomplete"),
        "a local-only endpoint has a complete address set; stderr was: {stderr:?}"
    );
}
