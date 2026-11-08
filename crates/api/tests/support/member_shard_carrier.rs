// SPDX-License-Identifier: AGPL-3.0-only
//! Real product artifact service/carrier across an exact-owned process. Controlled
//! grant issuer and recorded byte-slice corpus, NOT application pairing/serving.
use super::*;
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use protocol::{artifact_grant as ag, identity::Identity};
use runtime::{
    iroh_host::LoopbackHost,
    sidecar_artifact::{grant_limits, serve_limits},
};
use std::{
    io::{BufRead, BufReader, Write},
    net::UdpSocket,
    process::{Child, Command, Stdio},
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};
use transport::{Channel, Clock};
struct Time(Instant);
impl Clock for Time {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(7), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("carrier must use monotonic time")
    }
}
impl Time {
    fn deadline(&self) -> MonoInstant {
        self.monotonic()
            .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
            .unwrap()
    }
}
fn allowance() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis())
}
fn rt() -> tokio::runtime::Runtime {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .unwrap()
}
fn grant_fixture() -> Value {
    serde_json::from_str(include_str!(
        "../../../protocol/fixtures/artifact_grant.json"
    ))
    .unwrap()
}
fn now() -> u64 {
    grant_fixture()["now_unix_ms"].as_u64().unwrap()
}
fn seed() -> Identity {
    Identity::from_secret("b7-controlled-grant-issuer", &[21; 32]).unwrap()
}
#[test]
#[ignore = "exact-owned subprocess entrypoint, exercised by carrier test"]
fn carrier_child() {
    if std::env::var("B7_ARTIFACT_CHILD").as_deref() != Ok("source") {
        return;
    }
    let f = Fixture::new();
    let rt = rt();
    let clock = Time(Instant::now());
    let mut host = LoopbackHost::bind_protocols(
        &rt,
        &clock,
        clock.deadline(),
        iroh::SecretKey::from_bytes(&[22; 32]),
        &[Channel::ArtifactChunk],
    )
    .unwrap();
    let address = host.address().unwrap();
    let socket = *address.ip_addrs().next().unwrap();
    let signer = Identity::from_secret(&address.id.to_string(), &[23; 32]).unwrap();
    let objects = f.root.join("objects");
    fs::create_dir(&objects).unwrap();
    fs::write(
        objects.join(ag::range_digest(&f.bytes).strip_prefix("sha256:").unwrap()),
        &f.bytes,
    )
    .unwrap();
    let objects =
        runtime::artifact_journal::DurableObjects::open(runtime::artifact_journal::DurableConfig {
            objects,
            state_dir: f.root.join("source-journal"),
            fence_capacity: B::ArtifactMaxFence.count().unwrap().get() as usize,
            clock_skew_ms: grant_limits().unwrap().clock_skew_ms,
            now: Arc::new(now),
        })
        .unwrap();
    let service = runtime::iroh_artifact_source::ArtifactService::new(
        signer,
        seed().public_key_record(),
        grant_limits().unwrap(),
        Arc::new(objects),
        serve_limits().unwrap(),
        Arc::new(now),
    )
    .unwrap();
    println!(
        "B7_READY {}",
        json!({"id":address.id.to_string(),"socket":socket,"pid":std::process::id()})
    );
    std::io::stdout().flush().unwrap();
    let connection = host.accept(&clock, clock.deadline()).unwrap();
    rt.block_on(service.serve_connection(connection)).unwrap();
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    assert!(UdpSocket::bind(socket).is_ok());
    println!("B7_CLOSED");
    std::io::stdout().flush().unwrap();
}
struct Peer {
    child: Child,
    lines: mpsc::Receiver<String>,
    reader: Option<std::thread::JoinHandle<()>>,
    done: bool,
}
impl Peer {
    fn spawn() -> Self {
        let mut child = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "carrier::carrier_child",
                "--ignored",
                "--nocapture",
            ])
            .env("B7_ARTIFACT_CHILD", "source")
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let stdout = child.stdout.take().unwrap();
        let (tx, lines) = mpsc::channel();
        let reader = std::thread::spawn(move || {
            for line in BufReader::new(stdout).lines() {
                if tx.send(line.unwrap()).is_err() {
                    return;
                }
            }
        });
        Self {
            child,
            lines,
            reader: Some(reader),
            done: false,
        }
    }
    fn line(&self, prefix: &str) -> String {
        let end = Instant::now() + allowance();
        loop {
            let line = self
                .lines
                .recv_timeout(end.saturating_duration_since(Instant::now()))
                .unwrap();
            if let Some(s) = line.strip_prefix(prefix) {
                return s.into();
            }
        }
    }
    fn finish(&mut self) {
        self.line("B7_CLOSED");
        // stdout EOF also bounds exit (the owned reader is joined only after EOF).
        let end = Instant::now() + allowance();
        while self
            .lines
            .recv_timeout(end.saturating_duration_since(Instant::now()))
            .is_ok()
        {}
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success(), "child exit: {status}");
                break;
            }
            assert!(Instant::now() < end, "child exit deadline");
            std::thread::yield_now();
        }
        self.done = true;
        self.reader.take().unwrap().join().unwrap();
    }
}
impl Drop for Peer {
    fn drop(&mut self) {
        if !self.done {
            if self.child.try_wait().unwrap().is_none() {
                self.child.kill().unwrap();
            }
            self.child.wait().unwrap();
        }
        if let Some(reader) = self.reader.take() {
            reader.join().unwrap();
        }
    }
}
#[test]
fn real_carrier_receives_chunks_retries_and_revocation_keeps_bytes_unwritten() {
    let f = Fixture::new();
    let mut peer = Peer::spawn();
    let ready: Value = serde_json::from_str(&peer.line("B7_READY ")).unwrap();
    assert_eq!(ready["pid"], peer.child.id());
    assert_ne!(peer.child.id(), std::process::id());
    let socket: std::net::SocketAddr = serde_json::from_value(ready["socket"].clone()).unwrap();
    let address = iroh::EndpointAddr::new(ready["id"].as_str().unwrap().parse().unwrap())
        .with_ip_addr(socket);
    let rt = rt();
    let clock = Time(Instant::now());
    let mut host = LoopbackHost::bind_with_secret(
        &rt,
        &clock,
        clock.deadline(),
        iroh::SecretKey::from_bytes(&[24; 32]),
    )
    .unwrap();
    let local = host.address().unwrap();
    let local_socket = *local.ip_addrs().next().unwrap();
    let member = Identity::from_secret(&local.id.to_string(), &[25; 32]).unwrap();
    let source = Identity::from_secret(&address.id.to_string(), &[23; 32]).unwrap();
    let mut message = grant_fixture()["grant"]["message"].clone();
    message["recipient_node_id"] = json!("node-0");
    message["recipient_endpoint_id"] = json!(member.endpoint_id());
    message["recipient_key_digest"] = json!(member.verification_key_digest());
    message["authorized_sources"] = json!([{"endpoint_id":source.endpoint_id(),"verification_key_digest":source.verification_key_digest(),"direct_addrs":[]}]);
    message["allowed_object_digests"] = json!([ag::range_digest(&f.bytes)]);
    message["not_before_unix_ms"] = json!(now());
    let envelope = json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"signature":seed().sign(&message).unwrap(),"message":message,"verification_key":seed().public_key_record()});
    let grant = ag::verify_grant(
        &envelope,
        &seed().public_key_record(),
        now(),
        grant_limits().unwrap(),
    )
    .unwrap();
    let conn = host
        .connect_channel(&address, Channel::ArtifactChunk, &clock, clock.deadline())
        .unwrap();
    let mut session = runtime::iroh_artifact::ChunkSession::new(
        conn,
        &rt,
        &clock,
        local.id,
        serve_limits().unwrap().wire,
    )
    .unwrap();
    let mut receive = f.begin("carrier");
    let split = f.bytes.len() / 2;
    for (index, offset, len) in [
        (0, 0, split),
        (1, 0, split),
        (2, split, f.bytes.len() - split),
        (3, split, f.bytes.len() - split),
    ] {
        let id = format!("b7-chunk-{index}");
        let request = ag::ChunkRequest {
            request_id: id.clone(),
            source_endpoint_id: source.endpoint_id().into(),
            object_digest: ag::range_digest(&f.bytes),
            offset_bytes: offset as u64,
            length_bytes: len as u64,
        };
        let envelope = ag::sign_chunk_request(
            &member,
            &grant,
            &id,
            &id,
            source.endpoint_id(),
            &request.object_digest,
            request.offset_bytes,
            request.length_bytes,
            now(),
            grant_limits().unwrap(),
        )
        .unwrap();
        if index == 2 {
            let calls = std::cell::Cell::new(0);
            let result = receive.fetch_chunk(
                &mut session,
                &envelope,
                &request,
                &grant,
                clock.deadline(),
                &|| {
                    calls.set(calls.get() + 1);
                    if calls.get() == 2 {
                        Err(api::compiler_inputs::Error("member_generation_changed"))
                    } else {
                        Ok(())
                    }
                },
            );
            assert_eq!(result.unwrap_err().0, "member_generation_changed");
            assert_eq!(receive.received_bytes().unwrap(), split as u64);
        } else {
            receive
                .fetch_chunk(
                    &mut session,
                    &envelope,
                    &request,
                    &grant,
                    clock.deadline(),
                    &|| Ok(()),
                )
                .unwrap();
        }
    }
    receive.verify(&|| Ok(())).unwrap();
    assert_eq!(fs::read(receive.path()).unwrap(), f.bytes);
    receive.discard().unwrap();
    assert_eq!(f.charge(), 0);
    drop(session);
    host.close(&clock, clock.deadline()).unwrap();
    drop(host);
    drop(rt);
    peer.finish();
    assert!(UdpSocket::bind(socket).is_ok());
    assert!(UdpSocket::bind(local_socket).is_ok());
}
