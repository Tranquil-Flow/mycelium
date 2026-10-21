// SPDX-License-Identifier: AGPL-3.0-only
//! End-to-end check of the loopback channel on the real sidecar binary.
//!
//! Built only with `--features loopback-test`, which exposes the Windows
//! channel flags on this host. The sidecar runs `--local-only` (no relay,
//! nothing off-host) and every test dials its `127.0.0.1` listener:
//!
//! * a client owned by the allowed pid with the correct HMAC is served;
//! * a wrong HMAC gets no server hello;
//! * a correct HMAC from a process other than the allowed pid is refused
//!   before the handshake;
//! * the stdin bootstrap refuses anything but exactly 32 bytes then EOF.
#![cfg(feature = "loopback-test")]

use std::io::{BufRead, BufReader, Read, Write};
use std::net::{Ipv4Addr, SocketAddrV4, TcpStream};
use std::process::{Child, ChildStderr, Command, Stdio};
use std::sync::mpsc;
use std::thread;
use std::time::Duration;

use mycelium_iroh_transport::local::{
    ClientHello, LOCAL_MAX_RECORD_BYTES, MAX_HELLO_BYTES, RecordKind, SequenceGuard, ServerHello,
    decode_record, derive_session_keys, encode_record,
};

const BINARY: &str = env!("CARGO_BIN_EXE_mycelium-iroh-sidecar");
const READY_WAIT: Duration = Duration::from_secs(20);
const IO_WAIT: Duration = Duration::from_secs(15);
const SECRET: [u8; 32] = [0x3c; 32];

struct RunningSidecar {
    child: Child,
    stderr: Option<ChildStderr>,
    ready: serde_json::Value,
}

impl RunningSidecar {
    fn start(allowed_client_pid: u32) -> Self {
        let mut child = Command::new(BINARY)
            .args(["--loopback", "--bootstrap-stdin", "--local-only"])
            .arg("--allowed-client-pid")
            .arg(allowed_client_pid.to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn the sidecar binary");
        let mut stdin = child.stdin.take().expect("stdin pipe");
        stdin.write_all(&SECRET).expect("hand over the bootstrap");
        drop(stdin);

        let stdout = child.stdout.take().expect("stdout pipe");
        let (sender, lines) = mpsc::channel();
        thread::spawn(move || {
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if sender.send(line).is_err() {
                    return;
                }
            }
        });
        let line = lines
            .recv_timeout(READY_WAIT)
            .expect("the sidecar must publish its ready record");
        let ready = serde_json::from_str(&line).expect("the ready record is JSON");
        Self {
            stderr: child.stderr.take(),
            child,
            ready,
        }
    }

    fn port(&self) -> u16 {
        u16::try_from(self.ready["local_port"].as_u64().expect("local_port"))
            .expect("port fits u16")
    }

    fn connect(&self) -> TcpStream {
        let stream = TcpStream::connect(SocketAddrV4::new(Ipv4Addr::LOCALHOST, self.port()))
            .expect("dial the loopback listener");
        stream.set_read_timeout(Some(IO_WAIT)).unwrap();
        stream.set_write_timeout(Some(IO_WAIT)).unwrap();
        stream.set_nodelay(true).unwrap();
        stream
    }

    fn stop(mut self) -> String {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let mut stderr = String::new();
        if let Some(mut pipe) = self.stderr.take() {
            let _ = pipe.read_to_string(&mut stderr);
        }
        stderr
    }
}

fn write_frame(stream: &mut TcpStream, bytes: &[u8]) -> std::io::Result<()> {
    let length = u32::try_from(bytes.len()).unwrap();
    stream.write_all(&length.to_be_bytes())?;
    stream.write_all(bytes)
}

fn read_frame(stream: &mut TcpStream, maximum: usize) -> std::io::Result<Vec<u8>> {
    let mut length = [0_u8; 4];
    stream.read_exact(&mut length)?;
    let length = u32::from_be_bytes(length) as usize;
    assert!(length > 0 && length <= maximum, "frame length {length}");
    let mut bytes = vec![0_u8; length];
    stream.read_exact(&mut bytes)?;
    Ok(bytes)
}

/// Sends a client hello and returns the server hello, or `None` when the
/// sidecar closes the connection instead of answering.
fn hello(stream: &mut TcpStream, secret: &[u8; 32], nonce: &[u8; 32]) -> Option<ServerHello> {
    let client = ClientHello::new(secret, nonce).unwrap();
    write_frame(stream, &serde_json::to_vec(&client).unwrap()).ok()?;
    match read_frame(stream, MAX_HELLO_BYTES) {
        Ok(reply) => Some(serde_json::from_slice(&reply).expect("server hello is JSON")),
        Err(error) => {
            assert_ne!(
                error.kind(),
                std::io::ErrorKind::WouldBlock,
                "the sidecar must close a refused connection, not stall it"
            );
            assert_ne!(error.kind(), std::io::ErrorKind::TimedOut);
            None
        }
    }
}

#[test]
fn ready_record_announces_the_loopback_port() {
    let sidecar = RunningSidecar::start(std::process::id());
    let mut keys = sidecar
        .ready
        .as_object()
        .expect("ready is an object")
        .keys()
        .cloned()
        .collect::<Vec<_>>();
    keys.sort();
    assert_eq!(
        keys,
        [
            "alpn",
            "endpoint_addr",
            "endpoint_id",
            "event",
            "local_port"
        ]
    );
    assert_ne!(sidecar.port(), 0);
    sidecar.stop();
}

#[test]
fn allowed_pid_with_correct_hmac_is_served() {
    let sidecar = RunningSidecar::start(std::process::id());
    let mut stream = sidecar.connect();
    let nonce = [0x11_u8; 32];
    let server = hello(&mut stream, &SECRET, &nonce).expect("the allowed client is answered");
    assert_eq!(server.endpoint_id, sidecar.ready["endpoint_id"]);
    let server_nonce = server
        .verify(&SECRET, &nonce)
        .expect("the sidecar proves the same bootstrap secret");
    let keys = derive_session_keys(&SECRET, &nonce, &server_nonce).unwrap();

    let ping = encode_record(RecordKind::Ping, 0, [9; 16], b"", &keys.client_to_sidecar).unwrap();
    write_frame(&mut stream, &ping).unwrap();
    let reply = read_frame(&mut stream, LOCAL_MAX_RECORD_BYTES).expect("ping is answered");
    let mut guard = SequenceGuard::new();
    let record = decode_record(&reply, &keys.sidecar_to_client, &mut guard).unwrap();
    assert_eq!(record.kind, RecordKind::Ack);
    assert_eq!(record.message_id, [9; 16]);

    drop(stream);
    let stderr = sidecar.stop();
    assert!(
        !stderr.contains("local_peer_rejected"),
        "stderr was: {stderr:?}"
    );
}

#[test]
fn wrong_hmac_is_refused() {
    let sidecar = RunningSidecar::start(std::process::id());
    let mut stream = sidecar.connect();
    assert!(
        hello(&mut stream, &[0x3d; 32], &[0x12; 32]).is_none(),
        "a wrong client proof must not receive a server hello"
    );
    sidecar.stop();
}

#[test]
fn correct_hmac_from_a_foreign_pid_is_refused() {
    // A live process that is not this test: it owns no connection to the
    // sidecar, so naming it as the allowed client must lock this test out.
    let mut foreign = Command::new("/bin/sleep")
        .arg("60")
        .stdin(Stdio::null())
        .spawn()
        .expect("spawn a foreign process");
    let sidecar = RunningSidecar::start(foreign.id());
    let mut stream = sidecar.connect();
    let refused = hello(&mut stream, &SECRET, &[0x13; 32]).is_none();
    let stderr = sidecar.stop();
    let _ = foreign.kill();
    let _ = foreign.wait();
    assert!(
        refused,
        "a connection owned by a foreign pid must be refused"
    );
    assert!(
        stderr.contains("local_peer_rejected"),
        "the refusal must be recorded; stderr was: {stderr:?}"
    );
}

fn bootstrap_outcome(bytes: &[u8]) -> (bool, String) {
    let mut child = Command::new(BINARY)
        .args(["--loopback", "--bootstrap-stdin", "--local-only"])
        .arg("--allowed-client-pid")
        .arg(std::process::id().to_string())
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn the sidecar binary");
    let mut stdin = child.stdin.take().unwrap();
    let _ = stdin.write_all(bytes);
    drop(stdin);
    let output = child.wait_with_output().expect("sidecar exits");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn stdin_bootstrap_refuses_short_long_and_trailing_input() {
    for input in [
        Vec::new(),
        vec![0x3c_u8; 31],
        vec![0x3c_u8; 33],
        [SECRET.as_slice(), b"\n".as_slice()].concat(),
    ] {
        let (succeeded, stderr) = bootstrap_outcome(&input);
        assert!(
            !succeeded,
            "{} bootstrap bytes must be refused",
            input.len()
        );
        assert!(
            stderr.contains("bootstrap_rejected"),
            "stderr was: {stderr:?}"
        );
    }
}

#[test]
fn loopback_flags_are_all_required_together_and_exclude_the_unix_socket() {
    let missing_pid = Command::new(BINARY)
        .args(["--loopback", "--bootstrap-stdin"])
        .output()
        .expect("sidecar runs");
    assert!(!missing_pid.status.success());
    assert!(String::from_utf8_lossy(&missing_pid.stderr).contains("--allowed-client-pid"));

    let zero_pid = Command::new(BINARY)
        .args([
            "--loopback",
            "--bootstrap-stdin",
            "--allowed-client-pid",
            "0",
        ])
        .output()
        .expect("sidecar runs");
    assert!(!zero_pid.status.success());

    let both = Command::new(BINARY)
        .args([
            "--uds",
            "/tmp/mycelium-loopback-conflict.sock",
            "--bootstrap-fd",
            "0",
            "--loopback",
            "--bootstrap-stdin",
            "--allowed-client-pid",
            "1",
        ])
        .output()
        .expect("sidecar runs");
    assert!(!both.status.success());
    assert!(String::from_utf8_lossy(&both.stderr).contains("cannot be used with"));
}
