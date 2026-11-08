// SPDX-License-Identifier: AGPL-3.0-only
//! Local-only joined member byte receiver. Private stdin/stdout operator channel;
//! no serving/route-ready claim. The process owns pairing, QUIC and B3 custody.
use api::{
    app_host,
    joined_shard::native::NativeShard,
    joined_shard::{self, JoinedShard},
    peer,
    preferences::{resource_policy, standing_consent},
    seed::SeedOptions,
};
use budgets::{BudgetId as B, Entries};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{iroh_host::LoopbackHost, sidecar_artifact::serve_limits};
use serde_json::{Value, json};
use std::path::PathBuf;
use transport::{Channel, Clock};
#[path = "shard_member/membership.rs"]
mod membership;
use membership::Membership;
#[path = "shard_member/control.rs"]
mod control;
#[path = "shard_member/leave.rs"]
mod leave;
#[path = "shard_member/serve.rs"]
mod serve;
type Result<T> = std::result::Result<T, String>;
fn main() {
    if let Err(code) = run() {
        eprintln!("shard_member_failed:{code}");
        std::process::exit(1);
    }
}
fn run() -> Result<()> {
    let mut args = std::env::args().skip(1);
    if args.next().as_deref() != Some("--state-root") {
        return Err("usage: --state-root PATH".into());
    }
    let root = PathBuf::from(args.next().ok_or("state_root_required")?);
    if args.next().is_some() {
        return Err("unexpected_argument".into());
    }
    // Public host admission normalizes trusted ancestors and refuses root links.
    let authority =
        app_host::Authority::<app_host::Unmanaged>::load(root).map_err(str::to_owned)?;
    let root = authority.root;
    let (signer, _, _) = app_host::identity(&root).map_err(|e| e.code.to_owned())?;
    // Pump QUIC even while stdin, file verification or native compute blocks.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .map_err(|_| "runtime_failed")?;
    let clock = api::managed_carrier::HostClock::default();
    let deadline = || {
        clock
            .monotonic()
            .checked_add(
                B::RouterClose
                    .span(Entries::ZERO)
                    .map_err(|_| "budget_unavailable")?,
            )
            .map_err(|_| "deadline_invalid".to_owned())
    };
    let mut host = LoopbackHost::bind(&rt, &clock, deadline()?).map_err(|e| format!("{e:?}"))?;
    let addr = host.address().map_err(|e| format!("{e:?}"))?;
    let carrier = Identity::from_secret(&addr.id.to_string(), &signer.secret_bytes())
        .map_err(|_| "identity_failed")?;
    let mut session: Option<Membership> = None;
    let mut native: Option<NativeShard> = None;
    let mut shard: Option<JoinedShard> = None;
    // Digest of the last signed resident proof THIS process produced.
    let mut proof_digest: Option<String> = None;
    let mut left = false;
    let input = control::Input::start()?;
    while let Ok(operation) = input.requests.recv() {
        let operation = operation?;
        let request = &operation.request;
        let command = request["command"]
            .as_str()
            .ok_or("operator_command_invalid")?;
        let reply: Result<Value> = (|| match command {
            _ if left
                && matches!(
                    command,
                    "receive" | "prepare_native" | "serve" | "accept" | "resume"
                ) =>
            {
                Err("member_left".into())
            }
            "carrier_address" => Ok(json!({"endpoint_id":addr.id.to_string(),
                "socket":addr.ip_addrs().next().ok_or("carrier_address_missing")?})),
            "serve" => {
                let member = session.as_ref().ok_or("not_joined")?;
                serve::serve(
                    &host,
                    &rt,
                    &clock,
                    native.as_mut().ok_or("native_required")?,
                    member,
                    &signer,
                    &request["route"],
                    proof_digest.as_deref().ok_or("native_proof_required")?,
                    &operation,
                )
            }
            "leave" => {
                let mode = request["mode"]
                    .as_str()
                    .ok_or("leave_mode_invalid")?
                    .to_owned();
                let began = std::time::Instant::now();
                let mut steps = leave::Leave {
                    root: root.clone(),
                    signer: &signer,
                    native: &mut native,
                    shard: &mut shard,
                    session: &mut session,
                    fenced: &mut left,
                    began,
                    withdrawal: None,
                    unload: json!(null),
                    timings: Vec::new(),
                    refunded: false,
                };
                let record = api::leave::MemberLeave::new(&root, api::leave::Limits::default())
                    .run(&mode, &mut steps, &mut api::leave::SystemClock::default())
                    .map_err(|e| e.code.to_owned())?;
                Ok(
                    json!({"record":record,"withdrawal":steps.withdrawal,"unload":steps.unload,
                    "steps":steps.timings,"refunded":steps.refunded,
                    "elapsed_ms":began.elapsed().as_secs_f64()*1000.0,"route_ready":false}),
                )
            }
            "pair_request" => app_host::build_pair_request(&root, &root)
                .map(|v| v.to_value())
                .map_err(|e| e.code.to_owned()),
            "accept" => {
                if session.is_some() {
                    return Err("already_joined".into());
                }
                let socket: std::net::SocketAddr =
                    serde_json::from_value(request["seed_address"].clone())
                        .map_err(|_| "seed_address_invalid")?;
                if !socket.ip().is_loopback() {
                    return Err("seed_address_not_loopback".into());
                }
                let options = SeedOptions {
                    ca_pem: Some(
                        request["ca_pem"]
                            .as_str()
                            .ok_or("ca_required")?
                            .as_bytes()
                            .to_vec(),
                    ),
                    connect_to: Some(socket),
                    server_name: Some("127.0.0.1".into()),
                };
                let random = |n| {
                    app_host::random(n)
                        .map(|v| app_host::hex(&v))
                        .unwrap_or_default()
                };
                let env = peer::JoinEnv {
                    signer: &signer,
                    clock: &app_host::now,
                    random_hex: &random,
                    seed: &options,
                };
                let mut accepted = peer::accept_handoff(&root, true, &request["handoff"], &env)
                    .map_err(|e| e.code.to_owned())?;
                peer::renew(&root, true, accepted.session.as_mut(), &env)
                    .map_err(|e| e.code.to_owned())?;
                session = Some(Membership::new(
                    accepted.session.ok_or("session_missing")?,
                    root.clone(),
                    &signer,
                    options,
                )?);
                let held = session
                    .as_ref()
                    .ok_or("session_missing")?
                    .session
                    .lock()
                    .map_err(|_| "session_poisoned")?;
                joined_shard::register(
                    held.as_ref().ok_or("session_missing")?,
                    &signer,
                    &carrier,
                    app_host::now(),
                )
                .map_err(|e| e.0.to_owned())
            }
            "resume" => {
                let member = session.as_ref().ok_or("not_joined")?;
                member.resume(&signer)?;
                member.current(&signer).map_err(|e| e.0.to_owned())
            }
            "consent" => {
                let held = session
                    .as_ref()
                    .ok_or("not_joined")?
                    .session
                    .lock()
                    .map_err(|_| "session_poisoned")?;
                let session = held.as_ref().ok_or("not_joined")?;
                resource_policy::write(&root, &request["policy"]).map_err(|e| e.code.to_owned())?;
                standing_consent::grant(
                    &root,
                    &request["consent"],
                    &request["policy"],
                    app_host::now(),
                    request["device_memory_bytes"].as_u64(),
                )
                .map_err(|e| e.0.to_owned())?;
                let binding = session.binding().ok_or("not_joined")?;
                standing_consent::bind(&root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).map_err(|e| e.0.to_owned())?;
                Ok(json!({"consent":"granted"}))
            }
            "receive" => {
                operation.check()?;
                let remaining = operation
                    .deadline
                    .saturating_duration_since(std::time::Instant::now());
                let end = clock
                    .monotonic()
                    .checked_add(budgets::Span::from_millis(
                        remaining
                            .as_millis()
                            .try_into()
                            .map_err(|_| "deadline_invalid")?,
                    ))
                    .map_err(|_| "deadline_invalid")?;
                if shard.is_some() || native.is_some() {
                    return Err("shard_already_owned".into());
                }
                let session = session.as_ref().ok_or("not_joined")?;
                let offer = &request["offer"];
                let source: iroh::EndpointId = request["source_id"]
                    .as_str()
                    .ok_or("source_id_missing")?
                    .parse()
                    .map_err(|_| "source_id_invalid")?;
                let socket: std::net::SocketAddr =
                    serde_json::from_value(request["source_socket"].clone())
                        .map_err(|_| "source_socket_invalid")?;
                if !socket.ip().is_loopback() {
                    return Err("source_not_loopback".into());
                }
                // Retain custody BEFORE any fallible carrier work; failures keep debt.
                shard = Some(
                    JoinedShard::begin(
                        &root,
                        session
                            .session
                            .lock()
                            .map_err(|_| "session_poisoned")?
                            .as_ref()
                            .ok_or("not_joined")?,
                        &signer,
                        &carrier,
                        offer,
                        app_host::now(),
                    )
                    .map_err(|e| e.0.to_owned())?,
                );
                let receive = shard.as_mut().unwrap();
                let connection = host
                    .connect_channel(
                        &iroh::EndpointAddr::new(source).with_ip_addr(socket),
                        Channel::ArtifactChunk,
                        &clock,
                        end.earliest(deadline()?).map_err(|_| "deadline_invalid")?,
                    )
                    .map_err(|e| format!("{e:?}"))?;
                let mut link = runtime::iroh_artifact::ChunkSession::new(
                    connection,
                    &rt,
                    &clock,
                    addr.id,
                    serve_limits().map_err(|_| "budget_unavailable")?.wire,
                )
                .map_err(|e| e.0)?;
                let size = offer["message"]["transfer"]["size_bytes"]
                    .as_u64()
                    .ok_or("size_invalid")?;
                let cap = B::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
                    .bytes()
                    .map_err(|_| "budget_unavailable")?
                    .get();
                let mut offset = 0;
                while offset < size {
                    operation.check()?;
                    // Never hold membership's renewal mutex over network I/O.
                    let snapshot = session
                        .session
                        .lock()
                        .map_err(|_| "session_poisoned")?
                        .as_ref()
                        .ok_or("not_joined")?
                        .clone();
                    let length = cap
                        .min(protocol::artifact_grant::MAX_CHUNK_BYTES)
                        .min(size - offset);
                    let chunk = ChunkRequest {
                        request_id: format!("chunk-{offset}"),
                        source_endpoint_id: source.to_string(),
                        object_digest: offer["message"]["transfer"]["content_digest"]
                            .as_str()
                            .ok_or("digest_invalid")?
                            .into(),
                        offset_bytes: offset,
                        length_bytes: length,
                    };
                    receive
                        .fetch_chunk_checked(
                            &mut link,
                            &chunk,
                            &snapshot,
                            &signer,
                            &carrier,
                            &app_host::now,
                            end,
                            &|| {
                                operation.check().map_err(|_| {
                                    api::compiler_inputs::Error(
                                        if operation
                                            .cancelled
                                            .load(std::sync::atomic::Ordering::SeqCst)
                                        {
                                            "member_operation_cancelled"
                                        } else {
                                            "member_operation_deadline"
                                        },
                                    )
                                })?;
                                session.current(&signer).map(drop)
                            },
                        )
                        .map_err(|e| e.0)?;
                    offset += length;
                }
                link.finish();
                let snapshot = session
                    .session
                    .lock()
                    .map_err(|_| "session_poisoned")?
                    .as_ref()
                    .ok_or("not_joined")?
                    .clone();
                receive
                    .verify_checked(&snapshot, &signer, &carrier, &app_host::now, &|| {
                        operation.check().map_err(|_| {
                            api::compiler_inputs::Error("member_operation_interrupted")
                        })?;
                        session.current(&signer).map(drop)
                    })
                    .map_err(|e| e.0.to_owned())?;
                Ok(
                    json!({"received_bytes":size,"path":receive.path(),"node_id":session.current(&signer).map_err(|e| e.0.to_owned())?["node_id"],"generation":session.current(&signer).map_err(|e| e.0.to_owned())?["generation"],"route_ready":false,"pid":std::process::id()}),
                )
            }
            "prepare_native" => {
                if native.is_none() {
                    native = Some(
                        NativeShard::new(shard.take().ok_or("shard_required")?)
                            .map_err(|e| e.0.to_owned())?,
                    );
                }
                let member = session.as_ref().ok_or("not_joined")?;
                let range = serde_json::from_value(request["range"].clone())
                    .map_err(|_| "range_invalid")?;
                let tokens = request["tokens"].as_u64().ok_or("tokens_invalid")? as usize;
                let nonce = request["nonce"].as_str().ok_or("nonce_invalid")?;
                let source = PathBuf::from(
                    request["forbidden_source"]
                        .as_str()
                        .ok_or("source_invalid")?,
                );
                let signed = native
                    .as_mut()
                    .unwrap()
                    .prepare_until(
                        range,
                        tokens,
                        nonce,
                        &signer,
                        &|| member.current(&signer),
                        operation.cancelled.clone(),
                        &source,
                        operation.deadline,
                    )
                    .map_err(|e| e.0.to_owned())?;
                proof_digest = Some(
                    api::joined_shard::route::proof_digest(&signed).map_err(|e| e.0.to_owned())?,
                );
                Ok(signed)
            }
            "native_step" => {
                use base64::Engine;
                let member = session.as_ref().ok_or("not_joined")?;
                let input: Option<Vec<u32>> = serde_json::from_value(request["tokens"].clone())
                    .map_err(|_| "tokens_invalid")?;
                let payload = base64::engine::general_purpose::STANDARD
                    .decode(request["payload"].as_str().ok_or("payload_invalid")?)
                    .map_err(|_| "payload_invalid")?;
                let bytes = native
                    .as_mut()
                    .ok_or("native_required")?
                    .step_until(
                        input.as_deref(),
                        &payload,
                        &|| member.current(&signer),
                        operation.cancelled.clone(),
                        operation.deadline,
                    )
                    .map_err(|e| e.0.to_owned())?;
                Ok(json!({"payload":base64::engine::general_purpose::STANDARD.encode(bytes)}))
            }
            "native_close" => native
                .as_mut()
                .ok_or("native_required")?
                .close()
                .map_err(|e| e.0.to_owned()),
            "discard" => {
                if let Some(compute) = native.as_mut() {
                    compute.discard().map_err(|e| e.0.to_owned())?;
                }
                native = None;
                if let Some(receive) = shard.as_mut() {
                    receive.discard().map_err(|e| e.0.to_owned())?;
                }
                shard = None;
                Ok(json!({"inert_bytes_discarded":true,"route_ready":false}))
            }
            "close" => Ok(json!({"closing":true,"route_ready":false})),
            _ => Err("operator_command_unknown".into()),
        })();
        let out = match reply {
            Ok(value) => json!({"ok":true,"result":value}),
            Err(code) => json!({"ok":false,"code":code}),
        };
        input.finished()?;
        control::reply(&out)?;
        if command == "close" {
            break;
        }
    }
    input.join()?;
    host.close(&clock, deadline()?)
        .map_err(|e| format!("{e:?}"))?;
    drop(host);
    drop(rt);
    // Retained shard debt deliberately survives EOF; no native cleanup inference.
    Ok(())
}
