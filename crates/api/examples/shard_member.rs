// SPDX-License-Identifier: AGPL-3.0-only
//! Local-only joined member byte receiver. Private stdin/stdout operator channel;
//! no serving/route-ready claim. The process owns pairing, QUIC and B3 custody.
use api::{
    app_host,
    joined_shard::{self, JoinedShard},
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
    seed::SeedOptions,
};
use budgets::{BudgetId as B, Entries};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{iroh_host::LoopbackHost, sidecar_artifact::serve_limits};
use serde_json::{Value, json};
use std::{
    io::{BufRead, Read, Write},
    path::PathBuf,
};
use transport::{Channel, Clock};
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
    let rt = tokio::runtime::Builder::new_current_thread()
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
    let mut session: Option<Session> = None;
    let mut shard: Option<JoinedShard> = None;
    let mut input = std::io::stdin().lock();
    let cap = api::preferences::private_state::private_json_limit().map_err(|e| e.0.to_owned())?;
    loop {
        let mut line = Vec::new();
        let read = std::io::Read::by_ref(&mut input)
            .take(cap + 1)
            .read_until(b'\n', &mut line)
            .map_err(|_| "operator_read_failed")?;
        if read == 0 {
            break;
        }
        if line.len() as u64 > cap || !line.ends_with(b"\n") {
            return Err("operator_frame_invalid".into());
        }
        let request: Value = serde_json::from_slice(&line).map_err(|_| "operator_json_invalid")?;
        let command = request["command"]
            .as_str()
            .ok_or("operator_command_invalid")?;
        let reply: Result<Value> = (|| match command {
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
                session = accepted.session;
                joined_shard::register(
                    session.as_ref().ok_or("session_missing")?,
                    &signer,
                    &carrier,
                    app_host::now(),
                )
                .map_err(|e| e.0.to_owned())
            }
            "consent" => {
                let session = session.as_ref().ok_or("not_joined")?;
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
                if shard.is_some() {
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
                    JoinedShard::begin(&root, session, &signer, &carrier, offer, app_host::now())
                        .map_err(|e| e.0.to_owned())?,
                );
                let receive = shard.as_mut().unwrap();
                let connection = host
                    .connect_channel(
                        &iroh::EndpointAddr::new(source).with_ip_addr(socket),
                        Channel::ArtifactChunk,
                        &clock,
                        deadline()?,
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
                    let length = cap.min(size - offset);
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
                        .fetch_chunk(
                            &mut link,
                            &chunk,
                            session,
                            &signer,
                            &carrier,
                            &app_host::now,
                            deadline()?,
                        )
                        .map_err(|e| e.0)?;
                    offset += length;
                }
                receive
                    .verify(session, &signer, &carrier, &app_host::now)
                    .map_err(|e| e.0.to_owned())?;
                Ok(
                    json!({"received_bytes":size,"path":receive.path(),"node_id":session.node_id(),"generation":session.generation(),"route_ready":false,"pid":std::process::id()}),
                )
            }
            "discard" => {
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
        writeln!(std::io::stdout(), "{out}").map_err(|_| "operator_write_failed")?;
        std::io::stdout()
            .flush()
            .map_err(|_| "operator_write_failed")?;
        if command == "close" {
            break;
        }
    }
    host.close(&clock, deadline()?)
        .map_err(|e| format!("{e:?}"))?;
    drop(host);
    drop(rt);
    // Retained shard debt deliberately survives EOF; no native cleanup inference.
    Ok(())
}
