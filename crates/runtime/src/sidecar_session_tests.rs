// SPDX-License-Identifier: AGPL-3.0-only
//! The local-IPC session loop and dispatcher against transcripts recorded by EXECUTING the
//! v0.1.4 sidecar's own `handle_local_session` / `process_local_record` / `control_call`
//! (`fixtures/sidecar-oracle-vectors.json` `session_transcripts`; harness in
//! `L9-evidence/r9-oracle-harness/`). Requests are replayed byte for byte; every response
//! kind, sequence, message id and payload must equal the oracle's. In-crate because the
//! inbound admission of the oracle's `admit_inbound` harness steps goes through the driver's
//! private state.
use super::*;
use crate::{
    iroh_host::LoopbackHost,
    iroh_pool::{DriverLimits, Services},
    sidecar_local::{
        ControlCallLimits, RecordKind, SequenceGuard, client_hello, decode_record,
        derive_session_keys, encode_record, hmac_sha256, verify_server_hello,
    },
    sidecar_pool::PoolLimits,
};
use budgets::{BudgetId as B, ClockDomain, Entries, MonoInstant};
use serde_json::Value;
use std::{
    sync::Arc,
    time::{Duration, Instant},
};
use tokio::io::{AsyncReadExt, AsyncWriteExt, DuplexStream};
use transport::Channel;

const SECRET: [u8; 32] = [0x5a; 32];
const ENDPOINT: &str = "duplex-endpoint";

struct Mono(Instant);
impl Clock for Mono {
    fn monotonic(&self) -> MonoInstant {
        MonoInstant::new(ClockDomain(33), self.0.elapsed().as_millis() as u64)
    }
    fn wall_seconds(&self) -> f64 {
        panic!("the session loop must not consult wall time")
    }
}
fn ms(value: u64) -> Span {
    Span::from_millis(value)
}
fn slice() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis() * 4)
}
fn pause(units: u64) -> Duration {
    Duration::from_millis(units)
}
fn session_limits() -> SessionLimits {
    SessionLimits {
        handshake: ms(2_000),
        receive_poll: ms(250),
        control: ControlCallLimits {
            ceiling_millis: 60_000,
            max_command_bytes: 4 * 1024 * 1024,
        },
    }
}
fn driver_limits() -> DriverLimits {
    DriverLimits {
        pool: PoolLimits {
            capacity: 2,
            seen_factor: 8,
            seen_floor: 64,
            initial_retry: ms(100),
            max_retry: ms(5_000),
            lifetime: ms(300_000),
        },
        connect: ms(5_000),
        stream_io: ms(60_000),
    }
}
fn oracle() -> Value {
    serde_json::from_str(include_str!("../fixtures/sidecar-oracle-vectors.json")).unwrap()
}
fn unhex(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}
fn text(value: &Value) -> &str {
    value.as_str().unwrap()
}
fn transcript(name: &str) -> Value {
    oracle()["session_transcripts"]
        .as_array()
        .unwrap()
        .iter()
        .find(|t| t["name"] == name)
        .unwrap_or_else(|| panic!("no transcript {name}"))
        .clone()
}
/// The oracle's harness masked the clock digits of `measured_at_unix_ms` (a wall time).
fn mask_measured(mut bytes: Vec<u8>) -> Vec<u8> {
    let needle = b"\"measured_at_unix_ms\":";
    if let Some(at) = bytes.windows(needle.len()).position(|w| w == needle) {
        let start = at + needle.len();
        let end = start
            + bytes[start..]
                .iter()
                .take_while(|b| b.is_ascii_digit())
                .count();
        bytes.splice(start..end, *b"0");
    }
    bytes
}

struct Rig {
    rt: tokio::runtime::Runtime,
    clock: Arc<Mono>,
}
impl Rig {
    fn new() -> Self {
        Self {
            rt: tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
                .unwrap(),
            clock: Arc::new(Mono(Instant::now())),
        }
    }
    fn deadline(&self) -> MonoInstant {
        self.clock.monotonic().checked_add(ms(60_000)).unwrap()
    }
}

async fn write_prefixed(stream: &mut DuplexStream, bytes: &[u8]) {
    stream.write_u32(bytes.len() as u32).await.unwrap();
    stream.write_all(bytes).await.unwrap();
}
async fn read_prefixed(stream: &mut DuplexStream) -> Vec<u8> {
    let length = stream.read_u32().await.unwrap() as usize;
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).await.unwrap();
    bytes
}

async fn try_read_prefixed(stream: &mut DuplexStream) -> Option<Vec<u8>> {
    let length = stream.read_u32().await.ok()? as usize;
    let mut bytes = vec![0; length];
    stream.read_exact(&mut bytes).await.ok()?;
    Some(bytes)
}

struct Client {
    stream: DuplexStream,
    keys: crate::sidecar_local::SessionKeys,
    send_sequence: u64,
    receive: SequenceGuard,
}
impl Client {
    async fn handshake(mut stream: DuplexStream) -> Self {
        let client_nonce = [0x21_u8; 32];
        write_prefixed(&mut stream, &client_hello(&SECRET, &client_nonce).unwrap()).await;
        let reply = read_prefixed(&mut stream).await;
        let (server_nonce, endpoint) = verify_server_hello(&reply, &SECRET, &client_nonce).unwrap();
        assert_eq!(endpoint, ENDPOINT);
        Self {
            stream,
            keys: derive_session_keys(&SECRET, &client_nonce, &server_nonce).unwrap(),
            send_sequence: 0,
            receive: SequenceGuard::new(),
        }
    }
    async fn send(&mut self, kind: RecordKind, id: [u8; 16], payload: &[u8]) {
        let encoded = encode_record(
            kind,
            self.send_sequence,
            id,
            payload,
            &self.keys.client_to_sidecar,
        )
        .unwrap();
        self.send_sequence += 1;
        write_prefixed(&mut self.stream, &encoded).await;
    }
    async fn read(&mut self) -> crate::sidecar_local::Record {
        let reply = tokio::time::timeout(slice(), read_prefixed(&mut self.stream))
            .await
            .expect("the session answers");
        decode_record(&reply, &self.keys.sidecar_to_client, &mut self.receive).unwrap()
    }
}

/// The body of a hello whose length prefix the server already rejected. v0.1.4 reads the
/// size first and ends the session, so the peer may be gone before the body is written:
/// a broken pipe here is the rejection arriving first (returned as `true`), anything else is
/// a real failure.
async fn write_rejected_body(stream: &mut DuplexStream, bytes: &[u8]) -> bool {
    match stream.write_all(bytes).await {
        Ok(()) => false,
        Err(error) if error.kind() == std::io::ErrorKind::BrokenPipe => true,
        Err(error) => panic!("unexpected write error {error:?}"),
    }
}
/// Waits (bounded by the router-close budget x4, no new deadline) until the task ended.
async fn wait_until_finished<T>(task: &tokio::task::JoinHandle<T>) {
    let started = Instant::now();
    while !task.is_finished() {
        assert!(started.elapsed() < slice(), "the session did not end");
        tokio::time::sleep(pause(1)).await;
    }
}
fn spawn_session(
    rig: &Rig,
    router: &Arc<PooledRouter<Mono>>,
    limits: SessionLimits,
) -> (
    DuplexStream,
    tokio::task::JoinHandle<Result<(), SessionError>>,
) {
    let (client, server) = tokio::io::duplex(64 * 1024 * 1024);
    let router = router.clone();
    let task = rig.rt.spawn(async move {
        serve_session(server, 1, &SECRET, ENDPOINT, [0x42; 32], &router, &limits).await
    });
    (client, task)
}

fn request_payload(request: &Value) -> Vec<u8> {
    let shown = text(&request["payload_hex"]);
    match shown.strip_prefix("zeros:") {
        Some(length) => vec![0; length.parse().unwrap()],
        None => unhex(shown),
    }
}

#[test]
fn the_oracle_script_replays_byte_for_byte_through_the_port_dispatcher() {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let router = Arc::new(
        PooledRouter::start_with(
            &host,
            rig.clock.clone(),
            driver_limits(),
            Services::default(),
        )
        .unwrap(),
    );
    let script = transcript("script_no_carrier");
    let steps = script["steps"].as_array().unwrap();
    // The first plain Send carries the oracle harness's valid Router frame.
    let frame = steps
        .iter()
        .find(|s| s["request"]["kind"] == 1 && s["response"]["kind"] == 4)
        .map(|s| request_payload(&s["request"]))
        .unwrap();
    let (stream, session) = spawn_session(&rig, &router, session_limits());
    rig.rt.block_on(async {
        let mut client = Client::handshake(stream).await;
        let mut replayed = 0;
        for (index, step) in steps.iter().enumerate() {
            if step.get("harness").is_some() {
                assert_eq!(step["harness"], "admit_inbound");
                let id = [step["id_byte"].as_u64().unwrap() as u8; 16];
                let origin = step["source_hex"].as_str().map(|source| {
                    (
                        <[u8; 32]>::try_from(unhex(source)).unwrap(),
                        step["generation"].as_u64().unwrap(),
                    )
                });
                router.admit_for_test(origin, id, frame.clone());
                continue;
            }
            let request = &step["request"];
            let response = &step["response"];
            let kind = RecordKind::try_from(request["kind"].as_u64().unwrap() as u8).unwrap();
            let id = [request["id_byte"].as_u64().unwrap() as u8; 16];
            client.send(kind, id, &request_payload(request)).await;
            let record = client.read().await;
            assert_eq!(
                (record.kind as u8, record.sequence, hex(&record.message_id)),
                (
                    response["kind"].as_u64().unwrap() as u8,
                    response["sequence"].as_u64().unwrap(),
                    text(&response["message_id_hex"]).to_owned()
                ),
                "step {index}: {request}"
            );
            assert_eq!(
                hex(&mask_measured(record.payload)),
                text(&response["payload_hex"]),
                "step {index}: {request}"
            );
            replayed += 1;
        }
        assert!(replayed >= 55, "only {replayed} oracle requests replayed");
        drop(client);
        assert_eq!(session.await.unwrap(), Err(SessionError::Disconnected));
    });
    rig.rt.block_on(router.shutdown());
    drop(router);
    host.close(&*rig.clock, rig.deadline()).unwrap();
}

/// When the client writes the body of its hello relative to the server's verdict.
/// `Natural` is whatever the scheduler gives; `AfterVerdict` forces the interleaving a
/// loaded host produced once (EV-0295): the server has already rejected the length prefix
/// (v0.1.4 `read_prefixed`, local.rs: the size check precedes the body read) and dropped
/// its end before the client's body write.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum BodyOrder {
    Natural,
    AfterVerdict,
}

#[test]
fn handshake_failures_end_with_the_oracle_results_and_never_answer_a_bad_client() {
    run_handshake_cases(BodyOrder::Natural);
}

#[test]
fn handshake_failures_hold_when_the_body_is_written_after_the_server_already_ended() {
    run_handshake_cases(BodyOrder::AfterVerdict);
}

fn run_handshake_cases(order: BodyOrder) {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let router = Arc::new(
        PooledRouter::start_with(
            &host,
            rig.clock.clone(),
            driver_limits(),
            Services::default(),
        )
        .unwrap(),
    );
    let cn = [0x21_u8; 32];
    let good: Value = serde_json::from_slice(&client_hello(&SECRET, &cn).unwrap()).unwrap();
    let wrong_secret = client_hello(&[0x5b; 32], &cn).unwrap();
    let mut unknown_field = good.clone();
    unknown_field["x"] = 1.into();
    let mut wrong_protocol = good.clone();
    wrong_protocol["protocol"] = "other".into();
    let mut alias = serde_json::Map::new();
    alias.insert("protocol".into(), good["protocol"].clone());
    alias.insert("nonce".into(), good["client_nonce"].clone());
    alias.insert("proof".into(), good["client_proof"].clone());
    let cases: Vec<(&str, Option<Vec<u8>>)> = vec![
        ("wrong_secret", Some(wrong_secret)),
        ("not_json", Some(b"not json".to_vec())),
        (
            "unknown_field",
            Some(serde_json::to_vec(&unknown_field).unwrap()),
        ),
        (
            "alias_fields_accepted_then_waits",
            Some(serde_json::to_vec(&alias).unwrap()),
        ),
        (
            "wrong_protocol",
            Some(serde_json::to_vec(&wrong_protocol).unwrap()),
        ),
        ("hello_zero_length", None),
        ("hello_over_4096", Some(vec![b' '; 4097])),
    ];
    let expected = transcript("handshake_failures");
    let expected = expected["cases"].as_array().unwrap();
    assert_eq!(expected.len(), cases.len());
    for ((name, bytes), oracle_case) in cases.into_iter().zip(expected) {
        assert_eq!(oracle_case["name"], name);
        let (mut client, task) = spawn_session(&rig, &router, session_limits());
        rig.rt.block_on(async {
            match &bytes {
                Some(bytes)
                    if order == BodyOrder::AfterVerdict && bytes.len() > MAX_HELLO_BYTES =>
                {
                    client.write_u32(bytes.len() as u32).await.unwrap();
                    wait_until_finished(&task).await;
                    assert!(
                        write_rejected_body(&mut client, bytes).await,
                        "{name}: the body reached a server that had already ended"
                    );
                }
                Some(bytes) if bytes.len() > MAX_HELLO_BYTES => {
                    client.write_u32(bytes.len() as u32).await.unwrap();
                    // Either the body arrives first or the rejection does; both are correct.
                    let arrived_after_verdict = write_rejected_body(&mut client, bytes).await;
                    eprintln!("{name}: body written after the verdict: {arrived_after_verdict}");
                }
                Some(bytes) => write_prefixed(&mut client, bytes).await,
                None => client.write_u32(0).await.unwrap(),
            }
            // No fixed reply window: a case the oracle answers is awaited up to the
            // router-close budget x4; a case it never answers is judged after the
            // session ended (an unwritten reply cannot arrive later, so the read below
            // is end-of-stream, not a guess about scheduling).
            let expects_reply = oracle_case["server_replied"].as_bool().unwrap();
            if !expects_reply {
                wait_until_finished(&task).await;
            }
            let replied = tokio::time::timeout(slice(), try_read_prefixed(&mut client))
                .await
                .ok()
                .flatten();
            assert_eq!(replied.is_some(), expects_reply, "{name}: server replied");
            let result = if oracle_case["session_result"] == "still_running_after_5s" {
                // The oracle accepted the aliased hello and kept waiting for records.
                drop(client);
                "still_running_after_5s".to_owned()
            } else {
                format!(
                    "{:?}",
                    tokio::time::timeout(slice(), task).await.unwrap().unwrap()
                )
            };
            if result != "still_running_after_5s" {
                assert_eq!(result, text(&oracle_case["session_result"]), "{name}");
            }
        });
    }
    // Silent client: the handshake bound ends it (the oracle: Timeout after >= 2 s).
    let started = Instant::now();
    let (client, task) = spawn_session(&rig, &router, session_limits());
    let result = rig
        .rt
        .block_on(async { tokio::time::timeout(slice(), task).await.unwrap().unwrap() });
    drop(client);
    assert_eq!(
        format!("{result:?}"),
        text(&transcript("silent_client")["session_result"])
    );
    assert!(started.elapsed() >= Duration::from_millis(session_limits().handshake.millis()));
    rig.rt.block_on(router.shutdown());
    drop(router);
    host.close(&*rig.clock, rig.deadline()).unwrap();
}

#[test]
fn post_handshake_violations_end_the_session_with_the_oracle_results() {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let router = Arc::new(
        PooledRouter::start_with(
            &host,
            rig.clock.clone(),
            driver_limits(),
            Services::default(),
        )
        .unwrap(),
    );
    let expected = transcript("post_handshake_session_ends");
    let run = |tamper: &dyn Fn(&mut Client) -> Vec<u8>, raw_length: Option<u32>| -> String {
        let (stream, task) = spawn_session(&rig, &router, session_limits());
        rig.rt.block_on(async {
            let mut client = Client::handshake(stream).await;
            match raw_length {
                Some(length) => client.stream.write_u32(length).await.unwrap(),
                None => {
                    let bytes = tamper(&mut client);
                    write_prefixed(&mut client.stream, &bytes).await;
                }
            }
            format!(
                "{:?}",
                tokio::time::timeout(slice(), task).await.unwrap().unwrap()
            )
        })
    };
    let ping = |key: [u8; 32], sequence: u64| {
        encode_record(RecordKind::Ping, sequence, [4; 16], b"", &key).unwrap()
    };
    assert_eq!(
        run(&|c| ping(c.keys.sidecar_to_client, 0), None),
        text(&expected["wrong_direction_key"])
    );
    assert_eq!(
        run(&|c| ping(c.keys.client_to_sidecar, 1), None),
        text(&expected["sequence_gap"])
    );
    assert_eq!(
        run(
            &|c| {
                let mut e = ping(c.keys.client_to_sidecar, 0);
                e[1] = 99;
                let n = e.len();
                let tag = hmac_sha256(&c.keys.client_to_sidecar, &e[..n - 32]);
                e[n - 32..].copy_from_slice(&tag);
                e
            },
            None
        ),
        text(&expected["unknown_kind_valid_tag"])
    );
    assert_eq!(
        run(&|_| Vec::new(), Some(0)),
        text(&expected["zero_length_record"])
    );
    assert_eq!(
        run(
            &|_| Vec::new(),
            Some(crate::sidecar_local::LOCAL_MAX_RECORD_BYTES as u32 + 1)
        ),
        text(&expected["record_length_over_cap"])
    );
    rig.rt.block_on(router.shutdown());
    drop(router);
    host.close(&*rig.clock, rig.deadline()).unwrap();
}

/// ControlCall execution against a real loopback NodeControl endpoint; the oracle's
/// own `control_call` produced the codes (`control_call_execution` transcript).
#[test]
fn control_calls_execute_over_node_control_with_the_oracle_codes() {
    enum Phone {
        Echo(&'static [u8]),
        Empty,
        Huge,
        Silent,
        Close,
    }
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let mut phone = LoopbackHost::bind_protocols(
        &rig.rt,
        &*rig.clock,
        rig.deadline(),
        iroh::SecretKey::from_bytes(&[0x55; 32]),
        &[Channel::NodeControl],
    )
    .unwrap();
    let router = Arc::new(
        PooledRouter::start_with(
            &host,
            rig.clock.clone(),
            driver_limits(),
            Services::default(),
        )
        .unwrap(),
    );
    let commands = Arc::new(std::sync::Mutex::new(Vec::<Vec<u8>>::new()));
    let behaviours = vec![
        Phone::Echo(b"{\"ok\":true}"),
        Phone::Empty,
        Phone::Huge,
        Phone::Silent,
        Phone::Close,
    ];
    let acceptor = {
        let endpoint = phone.endpoint().unwrap().clone();
        let commands = commands.clone();
        rig.rt.spawn(async move {
            let mut connections = tokio::task::JoinSet::new();
            for behaviour in behaviours {
                let Some(incoming) = endpoint.accept().await else {
                    return;
                };
                let commands = commands.clone();
                connections.spawn(async move {
                    let Ok(connection) = incoming.await else {
                        return;
                    };
                    let Ok((mut send, mut recv)) = connection.accept_bi().await else {
                        return;
                    };
                    let Ok(command) = recv.read_to_end(4 * 1024 * 1024).await else {
                        return;
                    };
                    commands.lock().unwrap().push(command);
                    match behaviour {
                        Phone::Echo(bytes) => {
                            observed("send.write_all", send.write_all(bytes).await);
                            observed("send.finish", send.finish());
                            observed("send.stopped", send.stopped().await);
                        }
                        Phone::Empty => {
                            observed("send.finish", send.finish());
                            observed("send.stopped", send.stopped().await);
                        }
                        Phone::Huge => {
                            observed(
                                "send.write_all",
                                send.write_all(&vec![1; 4 * 1024 * 1024 + 1]).await,
                            );
                            observed("send.finish", send.finish());
                            observed("send.stopped", send.stopped().await);
                        }
                        Phone::Silent => tokio::time::sleep(pause(5_000)).await,
                        Phone::Close => {
                            connection.close(iroh::endpoint::VarInt::from_u32(9), b"no")
                        }
                    }
                });
            }
            // Keep the per-connection tasks alive until they finish or the acceptor is aborted.
            while connections.join_next().await.is_some() {}
        })
    };
    // Without `enable_control_carrier` the call answers carrier_unavailable (oracle: no endpoint).
    let phone_address = phone.address().unwrap();
    let phone_key = *phone_address.id.as_bytes();
    let configure = serde_json::to_vec(&serde_json::json!({"peers": [
        {"endpoint_id": phone_address.id.to_string(), "endpoint_addr": phone_address, "generation": 1}
    ]}))
    .unwrap();
    let steps = transcript("control_call_execution");
    let steps = steps["steps"].as_array().unwrap();
    let code = |step: &Value| -> String {
        let response = &step["response"];
        if response["kind"] == 19 {
            text(&response["payload_text"]).to_owned()
        } else {
            text(&serde_json::from_str::<Value>(text(&response["payload_text"])).unwrap()["code"])
                .to_owned()
        }
    };
    let call = |millis: u64, command: &[u8]| {
        [phone_key.as_slice(), &millis.to_be_bytes(), command].concat()
    };
    let (stream, session) = spawn_session(&rig, &router, session_limits());
    rig.rt.block_on(async {
        let mut client = Client::handshake(stream).await;
        // 0: unknown peer (nothing configured) answers unknown_peer once the carrier is on.
        client
            .send(
                RecordKind::ControlCall,
                [1; 16],
                &call(1000, b"{\"command\":\"health\"}"),
            )
            .await;
        let record = client.read().await;
        assert_eq!(
            String::from_utf8(record.payload).unwrap(),
            "{\"code\":\"carrier_unavailable\"}"
        );
        assert!(
            router.enable_control_carrier(crate::iroh_pool::ControlCarrierLimits {
                concurrency: 4,
                max_response_bytes: 4 * 1024 * 1024,
            }),
            "first enable creates the carrier"
        );
        client
            .send(
                RecordKind::ControlCall,
                [1; 16],
                &call(1000, b"{\"command\":\"health\"}"),
            )
            .await;
        let record = client.read().await;
        assert_eq!(
            (record.kind, code_from_error(&record.payload)),
            (RecordKind::Error, code(&steps[0]))
        );
        client
            .send(RecordKind::ConfigurePeers, [2; 16], &configure)
            .await;
        assert_eq!(client.read().await.kind, RecordKind::Ack);
        for (index, (millis, command)) in [
            (5_000, "health"),
            (5_000, "empty"),
            (10_000, "huge"),
            (800, "silent"),
            (3_000, "closed"),
        ]
        .into_iter()
        .enumerate()
        {
            let body = format!("{{\"command\":\"{command}\"}}");
            client
                .send(
                    RecordKind::ControlCall,
                    [3 + index as u8; 16],
                    &call(millis, body.as_bytes()),
                )
                .await;
            let record = client.read().await;
            let expected = &steps[2 + index];
            assert_eq!(
                (
                    record.kind as u8,
                    String::from_utf8(record.payload.clone()).unwrap()
                ),
                (
                    expected["response"]["kind"].as_u64().unwrap() as u8,
                    text(&expected["response"]["payload_text"]).to_owned()
                ),
                "control call {command}"
            );
        }
        drop(client);
        assert_eq!(session.await.unwrap(), Err(SessionError::Disconnected));
    });
    assert_eq!(
        commands
            .lock()
            .unwrap()
            .iter()
            .map(|c| String::from_utf8_lossy(c).into_owned())
            .collect::<Vec<_>>(),
        transcript("control_call_execution")["phone_received_commands"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| text(c).to_owned())
            .collect::<Vec<_>>()
    );
    acceptor.abort();
    rig.rt.block_on(router.shutdown());
    drop(router);
    host.close(&*rig.clock, rig.deadline()).unwrap();
    phone.close(&*rig.clock, rig.deadline()).unwrap();
}

fn code_from_error(payload: &[u8]) -> String {
    serde_json::from_slice::<Value>(payload).unwrap()["code"]
        .as_str()
        .unwrap()
        .to_owned()
}

enum Behaviour {
    Echo,
    Silent,
}
/// A loopback NodeControl phone: one behaviour per accepted connection; reports each
/// command it read and how each connection ended.
fn spawn_phone(
    rig: &Rig,
    phone: &LoopbackHost<'_>,
    behaviours: Vec<Behaviour>,
) -> (
    tokio::task::JoinHandle<()>,
    Arc<std::sync::Mutex<Vec<String>>>,
) {
    let log = Arc::new(std::sync::Mutex::new(Vec::new()));
    let endpoint = phone.endpoint().unwrap().clone();
    let shared = log.clone();
    let task = rig.rt.spawn(async move {
        let mut connections = tokio::task::JoinSet::new();
        for behaviour in behaviours {
            let Some(incoming) = endpoint.accept().await else {
                return;
            };
            let log = shared.clone();
            connections.spawn(async move {
                let Ok(connection) = incoming.await else {
                    return;
                };
                let Ok((mut send, mut recv)) = connection.accept_bi().await else {
                    return;
                };
                let Ok(command) = recv.read_to_end(1024).await else {
                    return;
                };
                log.lock()
                    .unwrap()
                    .push(format!("command {}", String::from_utf8_lossy(&command)));
                match behaviour {
                    Behaviour::Echo => {
                        observed("send.write_all", send.write_all(b"{\"ok\":true}").await);
                        observed("send.finish", send.finish());
                        observed("send.stopped", send.stopped().await);
                    }
                    Behaviour::Silent => {
                        let error = connection.closed().await;
                        let text = match error {
                            iroh::endpoint::ConnectionError::ApplicationClosed(close) => format!(
                                "code={} reason={}",
                                close.error_code.into_inner(),
                                String::from_utf8_lossy(&close.reason)
                            ),
                            other => format!("other={other}"),
                        };
                        log.lock().unwrap().push(format!("closed {text}"));
                    }
                }
            });
        }
        while connections.join_next().await.is_some() {}
    });
    (task, log)
}

/// sidecar.rs tests `control_call_rotation_*` / `control_call_client_hangup_releases_slot_and_session`:
/// a peer change ends a pending call as `peer_rotated`; a client hangup ends the session,
/// closes the call's connection with code 7 and frees the carrier slot.
#[test]
fn a_pending_control_call_ends_on_rotation_and_on_client_hangup_and_frees_its_slot() {
    let rig = Rig::new();
    let mut host = LoopbackHost::bind(&rig.rt, &*rig.clock, rig.deadline()).unwrap();
    let mut phone = LoopbackHost::bind_protocols(
        &rig.rt,
        &*rig.clock,
        rig.deadline(),
        iroh::SecretKey::from_bytes(&[0x56; 32]),
        &[Channel::NodeControl],
    )
    .unwrap();
    let router = Arc::new(
        PooledRouter::start_with(
            &host,
            rig.clock.clone(),
            driver_limits(),
            Services::default(),
        )
        .unwrap(),
    );
    assert!(
        router.enable_control_carrier(crate::iroh_pool::ControlCarrierLimits {
            concurrency: 1,
            max_response_bytes: 1024,
        }),
        "first enable creates the carrier"
    );
    let (acceptor, log) = spawn_phone(
        &rig,
        &phone,
        vec![Behaviour::Silent, Behaviour::Silent, Behaviour::Echo],
    );
    let address = phone.address().unwrap();
    let key = *address.id.as_bytes();
    router.configure_peers(vec![(address.clone(), 1)]).unwrap();
    let call = |millis: u64| [key.as_slice(), &millis.to_be_bytes(), b"{\"c\":1}"].concat();
    let wait_for = |what: &str, needle: &str| {
        let end = Instant::now() + slice();
        while !log.lock().unwrap().iter().any(|l| l.starts_with(needle)) {
            assert!(
                Instant::now() < end,
                "timed out: {what}: {:?}",
                log.lock().unwrap()
            );
            std::thread::sleep(pause(10));
        }
    };

    // Rotation while the call is pending.
    let (stream, session) = spawn_session(&rig, &router, session_limits());
    let mut client = rig.rt.block_on(async {
        let mut client = Client::handshake(stream).await;
        client
            .send(RecordKind::ControlCall, [1; 16], &call(30_000))
            .await;
        client
    });
    wait_for("phone read the first command", "command");
    router.configure_peers(vec![(address.clone(), 2)]).unwrap();
    let record = rig.rt.block_on(client.read());
    assert_eq!(
        (record.kind, String::from_utf8(record.payload).unwrap()),
        (RecordKind::Error, "{\"code\":\"peer_rotated\"}".to_owned())
    );
    wait_for(
        "the call connection closed by the carrier",
        "closed code=7 reason=control call ended",
    );
    drop(client);
    assert_eq!(
        rig.rt
            .block_on(async { tokio::time::timeout(slice(), session).await })
            .unwrap()
            .unwrap(),
        Err(SessionError::Disconnected)
    );

    // Client hangup: the slot (concurrency 1) is free for the next call, which succeeds.
    let (stream, session) = spawn_session(&rig, &router, session_limits());
    rig.rt.block_on(async {
        let mut client = Client::handshake(stream).await;
        client
            .send(RecordKind::ControlCall, [2; 16], &call(30_000))
            .await;
        // Hang up only once the phone holds the call (as the sidecar's own test does).
        let end = Instant::now() + slice();
        while log
            .lock()
            .unwrap()
            .iter()
            .filter(|l| l.starts_with("command"))
            .count()
            < 2
        {
            assert!(Instant::now() < end, "second call never reached the phone");
            tokio::time::sleep(pause(10)).await;
        }
        drop(client);
        assert_eq!(
            tokio::time::timeout(slice(), session)
                .await
                .unwrap()
                .unwrap(),
            Ok(())
        );
    });
    let end = Instant::now() + slice();
    while log
        .lock()
        .unwrap()
        .iter()
        .filter(|l| l.starts_with("closed code=7"))
        .count()
        < 2
    {
        assert!(
            Instant::now() < end,
            "second call connection not closed: {:?}",
            log.lock().unwrap()
        );
        std::thread::sleep(pause(10));
    }
    let (stream, session) = spawn_session(&rig, &router, session_limits());
    rig.rt.block_on(async {
        let mut client = Client::handshake(stream).await;
        client
            .send(RecordKind::ControlCall, [3; 16], &call(5_000))
            .await;
        let record = client.read().await;
        assert_eq!(
            (record.kind, record.payload),
            (RecordKind::ControlCallResult, b"{\"ok\":true}".to_vec())
        );
        drop(client);
        assert_eq!(session.await.unwrap(), Err(SessionError::Disconnected));
    });
    acceptor.abort();
    rig.rt.block_on(router.shutdown());
    drop(router);
    host.close(&*rig.clock, rig.deadline()).unwrap();
    phone.close(&*rig.clock, rig.deadline()).unwrap();
}

/// A result whose failure is tolerated by design (a cleanup, a peer that already ended the
/// call): the outcome is printed to the test log, never dropped.
fn observed<T, E: std::fmt::Debug>(what: &str, result: Result<T, E>) {
    if let Err(error) = result {
        eprintln!("tolerated {what}: {error:?}");
    }
}
