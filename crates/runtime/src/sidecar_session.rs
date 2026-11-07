// SPDX-License-Identifier: AGPL-3.0-only
//! Local-IPC session loop and dispatcher over [`PooledRouter`]: the authenticated half of
//! the v0.1.4 sidecar's local channel (`native/iroh_transport/src/sidecar.rs @ f4e366e9`
//! `handle_local_session` 956-994, `authenticated_session_loop` 996-1041,
//! `process_local_record` 1049-1260, `write_authenticated_record` 1279-1296,
//! `read/write_length_prefixed` 1298-1330). Verified against transcripts recorded by
//! executing those functions (`fixtures/sidecar-oracle-vectors.json`).
//!
//! What it does not own: accepting connections, the 16-session cap, the listener's peer
//! authorization and the process boundary (`runtime::sidecar_process`); randomness (the
//! caller supplies the server nonce); time bounds (caller-supplied [`SessionLimits`]; the
//! shipped values are 2 s handshake and 250 ms receive poll, no native L1 id yet).
use crate::{
    iroh_pool::{PoolError, PooledRouter, frame_digest},
    sidecar_local::{
        ControlCallLimits, LOCAL_MAX_RECORD_BYTES, LocalProtocolError, Record, RecordKind,
        SequenceGuard, SessionKeys, decode_record, delivery_from_payload, delivery_payload,
        derive_session_keys, encode_record, error_payload, parse_control_call_with, parse_send,
        server_hello, validate_router_ingress, verify_client_hello,
    },
    sidecar_pool::{EnqueueOutcome, EnqueueRequest, OutboundTerminal, PeerBinding},
};
use budgets::Span;
use iroh::{EndpointAddr, EndpointId};
use std::time::Duration;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use transport::Clock;

/// Hello objects are at most this many bytes (`MAX_HELLO_BYTES`, local.rs:27).
pub const MAX_HELLO_BYTES: usize = 4 * 1024;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SessionError {
    Authentication,
    Protocol,
    Disconnected,
    Timeout,
    Runtime,
}

/// Caller-supplied bounds. Shipped: `handshake` 2 s (`LOCAL_HANDSHAKE_TIMEOUT`,
/// sidecar.rs:67), `receive_poll` 250 ms (`RECEIVE_POLL_WAIT`, sidecar.rs:52), control
/// ceiling 60 s and command cap 4 MiB (sidecar.rs:95-96).
#[derive(Clone, Copy, Debug)]
pub struct SessionLimits {
    pub handshake: Span,
    pub receive_poll: Span,
    pub control: ControlCallLimits,
}

/// `ConfigurePeerPayload` (serde `deny_unknown_fields`: exactly these three keys). The
/// crate has no serde derive dependency, so the shape is checked on the JSON value; a
/// repeated key (which serde's derive rejects) collapses to its last value here.
struct ConfigurePeerPayload {
    endpoint_id: String,
    endpoint_addr: EndpointAddr,
    generation: u64,
}
fn parse_peer_value(value: &serde_json::Value) -> Result<ConfigurePeerPayload, ()> {
    let object = value.as_object().ok_or(())?;
    if object.len() != 3 {
        return Err(());
    }
    Ok(ConfigurePeerPayload {
        endpoint_id: object
            .get("endpoint_id")
            .and_then(|v| v.as_str())
            .ok_or(())?
            .to_owned(),
        endpoint_addr: serde_json::from_value(object.get("endpoint_addr").ok_or(())?.clone())
            .map_err(|_| ())?,
        generation: object
            .get("generation")
            .and_then(|v| v.as_u64())
            .ok_or(())?,
    })
}
fn parse_peer_bytes(bytes: &[u8]) -> Result<ConfigurePeerPayload, ()> {
    parse_peer_value(&serde_json::from_slice(bytes).map_err(|_| ())?)
}
/// `ConfigurePeersPayload`: exactly `{"peers":[...]}`.
fn parse_peers_bytes(bytes: &[u8]) -> Result<Vec<ConfigurePeerPayload>, ()> {
    let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|_| ())?;
    let object = value.as_object().ok_or(())?;
    if object.len() != 1 {
        return Err(());
    }
    object
        .get("peers")
        .and_then(|v| v.as_array())
        .ok_or(())?
        .iter()
        .map(parse_peer_value)
        .collect()
}
/// `AdmissionCounterRequest` (unknown keys are ignored, as in the sidecar).
fn parse_candidate(bytes: &[u8]) -> Result<EndpointId, ()> {
    let value: serde_json::Value = serde_json::from_slice(bytes).map_err(|_| ())?;
    value
        .get("candidate_endpoint_id")
        .and_then(|v| v.as_str())
        .ok_or(())?
        .parse::<EndpointId>()
        .map_err(|_| ())
}

struct Response {
    kind: RecordKind,
    message_id: [u8; 16],
    payload: Vec<u8>,
}
fn ack(message_id: [u8; 16]) -> Response {
    Response {
        kind: RecordKind::Ack,
        message_id,
        payload: Vec::new(),
    }
}
fn failure(message_id: [u8; 16], code: &str) -> Response {
    Response {
        kind: RecordKind::Error,
        message_id,
        payload: error_payload(code),
    }
}

async fn read_prefixed<S: AsyncRead + Unpin>(
    stream: &mut S,
    maximum: usize,
) -> Result<Vec<u8>, SessionError> {
    let length = stream
        .read_u32()
        .await
        .map_err(|_| SessionError::Disconnected)? as usize;
    if length == 0 || length > maximum {
        return Err(SessionError::Protocol);
    }
    let mut bytes = vec![0_u8; length];
    stream
        .read_exact(&mut bytes)
        .await
        .map_err(|_| SessionError::Disconnected)?;
    Ok(bytes)
}
async fn write_prefixed<S: AsyncWrite + Unpin>(
    stream: &mut S,
    bytes: &[u8],
) -> Result<(), SessionError> {
    let length = u32::try_from(bytes.len()).map_err(|_| SessionError::Protocol)?;
    stream
        .write_u32(length)
        .await
        .map_err(|_| SessionError::Disconnected)?;
    stream
        .write_all(bytes)
        .await
        .map_err(|_| SessionError::Disconnected)
}
fn span(value: Span) -> Duration {
    Duration::from_millis(value.millis())
}

/// One local connection: handshake, then records until the peer hangs up or violates the
/// protocol. On every exit the session's undelivered inbound items go back to the front of
/// the queue (`serve_local_session`, sidecar.rs:792-812).
pub async fn serve_session<S, C>(
    stream: S,
    session_id: u64,
    secret: &[u8],
    endpoint_id: &str,
    server_nonce: [u8; 32],
    router: &PooledRouter<C>,
    limits: &SessionLimits,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
    C: Clock + Send + Sync + 'static,
{
    let result = handle_session(
        stream,
        session_id,
        secret,
        endpoint_id,
        server_nonce,
        router,
        limits,
    )
    .await;
    router.redeliver_session(session_id);
    result
}

async fn handle_session<S, C>(
    mut stream: S,
    session_id: u64,
    secret: &[u8],
    endpoint_id: &str,
    server_nonce: [u8; 32],
    router: &PooledRouter<C>,
    limits: &SessionLimits,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
    C: Clock + Send + Sync + 'static,
{
    let handshake = span(limits.handshake);
    let hello = tokio::time::timeout(handshake, read_prefixed(&mut stream, MAX_HELLO_BYTES))
        .await
        .map_err(|_| SessionError::Timeout)??;
    let client_nonce = verify_client_hello(&hello, secret).map_err(|error| match error {
        LocalProtocolError::InvalidHello => SessionError::Protocol,
        _ => SessionError::Authentication,
    })?;
    let reply = server_hello(secret, &client_nonce, &server_nonce, endpoint_id)
        .map_err(|_| SessionError::Runtime)?;
    tokio::time::timeout(handshake, write_prefixed(&mut stream, &reply))
        .await
        .map_err(|_| SessionError::Timeout)??;
    let keys = derive_session_keys(secret, &client_nonce, &server_nonce)
        .map_err(|_| SessionError::Runtime)?;
    session_loop(&mut stream, session_id, &keys, router, limits).await
}

async fn session_loop<S, C>(
    stream: &mut S,
    session_id: u64,
    keys: &SessionKeys,
    router: &PooledRouter<C>,
    limits: &SessionLimits,
) -> Result<(), SessionError>
where
    S: AsyncRead + AsyncWrite + Unpin,
    C: Clock + Send + Sync + 'static,
{
    let mut receive_sequence = SequenceGuard::new();
    let mut send_sequence = Some(0_u64);
    loop {
        let encoded = read_prefixed(stream, LOCAL_MAX_RECORD_BYTES).await?;
        let record = decode_record(&encoded, &keys.client_to_sidecar, &mut receive_sequence)
            .map_err(|_| SessionError::Authentication)?;
        // sidecar.rs:1011-1013: only these two wait on a terminal while the client may
        // hang up (a routed send also waits but is not raced against the socket).
        let waits_for_terminal = matches!(
            record.kind,
            RecordKind::SendConfirmed | RecordKind::ControlCall
        );
        let response = process(record, session_id, router, limits);
        tokio::pin!(response);
        let response = if waits_for_terminal {
            let mut unexpected = [0_u8; 1];
            tokio::select! {
                response = &mut response => response,
                read = stream.read(&mut unexpected) => match read {
                    Ok(0) => return Ok(()),
                    Ok(_) => return Err(SessionError::Protocol),
                    Err(_) => return Err(SessionError::Disconnected),
                },
            }
        } else {
            response.await
        };
        let current = send_sequence.ok_or(SessionError::Protocol)?;
        let encoded = encode_record(
            response.kind,
            current,
            response.message_id,
            &response.payload,
            &keys.sidecar_to_client,
        )
        .map_err(|_| SessionError::Protocol)?;
        send_sequence = current.checked_add(1);
        write_prefixed(stream, &encoded).await?;
    }
}

fn endpoint_key(raw: &[u8; 32]) -> Option<[u8; 32]> {
    EndpointId::from_bytes(raw).ok().map(|_| *raw)
}

/// `validate_peer` (sidecar.rs:1841-1862); the loopback-only address rule is the pool's.
fn parse_peer(payload: ConfigurePeerPayload) -> Result<(EndpointAddr, u64), ()> {
    if payload.generation == 0
        || payload.endpoint_addr.id.to_string() != payload.endpoint_id
        || payload.endpoint_id.parse::<EndpointId>().is_err()
    {
        return Err(());
    }
    Ok((payload.endpoint_addr, payload.generation))
}

async fn process<C>(
    record: Record,
    session_id: u64,
    router: &PooledRouter<C>,
    limits: &SessionLimits,
) -> Response
where
    C: Clock + Send + Sync + 'static,
{
    let id = record.message_id;
    match record.kind {
        RecordKind::Send | RecordKind::SendConfirmed | RecordKind::SendRouted => {
            let routed = record.kind == RecordKind::SendRouted;
            let confirmed = routed || record.kind == RecordKind::SendConfirmed;
            // The destination point is checked before the generations (sidecar.rs:1060-1070).
            if routed {
                let valid = record
                    .payload
                    .first_chunk::<32>()
                    .is_some_and(|raw| endpoint_key(raw).is_some());
                if !valid {
                    return failure(id, "invalid_peer");
                }
            }
            let send = match parse_send(record.kind, record.payload) {
                Ok(send) => send,
                Err(code) => return failure(id, code),
            };
            // Any ingress refusal, including the size cap, answers invalid_frame (1101-1103).
            if validate_router_ingress(&send.frame).is_err() {
                return failure(id, "invalid_frame");
            }
            let request = EnqueueRequest {
                message_id: id,
                digest: frame_digest(&send.frame),
                payload: send.frame,
                expected_generation: send.expected_generation,
                source_generation: send.source_generation,
                destination: send.destination,
            };
            match router.enqueue(request) {
                EnqueueOutcome::Queued(handle) | EnqueueOutcome::Duplicate(handle) => {
                    if !confirmed {
                        return ack(id);
                    }
                    match router.wait_terminal(handle).await {
                        OutboundTerminal::Delivered => ack(id),
                        OutboundTerminal::PeerRotated => failure(id, "peer_rotated"),
                        OutboundTerminal::Cancelled => failure(id, "cancelled"),
                        OutboundTerminal::ReplayCollision => failure(id, "replay_collision"),
                    }
                }
                EnqueueOutcome::ReplayCollision => failure(id, "replay_collision"),
                EnqueueOutcome::PeerRotated => failure(id, "peer_rotated"),
                // `Refused` (clock arithmetic) has no v0.1.4 counterpart; the queue could not
                // take the item either way.
                EnqueueOutcome::Full | EnqueueOutcome::Refused => failure(id, "queue_full"),
            }
        }
        RecordKind::Receive => match router.receive(session_id, limits.receive_poll).await {
            Some(item) => Response {
                kind: RecordKind::Delivery,
                message_id: item.message_id,
                payload: delivery_payload(item.generation, &item.payload),
            },
            None => failure(id, "empty"),
        },
        RecordKind::ReceiveFrom => match router.receive(session_id, limits.receive_poll).await {
            Some(item) => Response {
                kind: RecordKind::DeliveryFrom,
                message_id: item.message_id,
                payload: delivery_from_payload(
                    item.source_endpoint,
                    item.generation,
                    &item.payload,
                ),
            },
            None => failure(id, "empty"),
        },
        RecordKind::Ack => {
            if router.ack(session_id, id) {
                ack(id)
            } else {
                failure(id, "unknown_delivery")
            }
        }
        RecordKind::Cancel => {
            if router.cancel(id) {
                ack(id)
            } else {
                failure(id, "unknown_message")
            }
        }
        RecordKind::ConfigurePeer => {
            let Ok(payload) = parse_peer_bytes(&record.payload) else {
                return failure(id, "invalid_peer");
            };
            let Ok((address, generation)) = parse_peer(payload) else {
                return failure(id, "invalid_peer");
            };
            // Exclusive single peer (sidecar.rs:1869-1895): the same sole binding is a no-op;
            // otherwise the generation must exceed the HIGHEST generation of any binding.
            let (primary, all) = router.peer_snapshot();
            let replacement = PeerBinding {
                endpoint: *address.id.as_bytes(),
                generation,
            };
            if all.len() == 1 && primary == Some(replacement) {
                return ack(id);
            }
            if all
                .iter()
                .map(|binding| binding.generation)
                .max()
                .is_some_and(|max| generation <= max)
            {
                return failure(id, "invalid_peer");
            }
            match router.configure_peers(vec![(address, generation)]) {
                Ok(_) => ack(id),
                Err(PoolError::Address | PoolError::Configure(_)) => failure(id, "invalid_peer"),
            }
        }
        RecordKind::ConfigurePeers => {
            let Ok(peers) = parse_peers_bytes(&record.payload) else {
                return failure(id, "invalid_peer");
            };
            let mut ordered = Vec::with_capacity(peers.len());
            for peer in peers {
                match parse_peer(peer) {
                    Ok(parsed) => ordered.push(parsed),
                    Err(()) => return failure(id, "invalid_peer"),
                }
            }
            match router.configure_peers(ordered) {
                Ok(_) => ack(id),
                Err(PoolError::Address | PoolError::Configure(_)) => failure(id, "invalid_peer"),
            }
        }
        RecordKind::Ping => ack(id),
        RecordKind::GetTransportObservations => Response {
            kind: RecordKind::TransportObservations,
            message_id: id,
            payload: router.transport_observations_json().into_bytes(),
        },
        RecordKind::GetAdmissionCounters => {
            let Ok(candidate) = parse_candidate(&record.payload) else {
                return failure(id, "invalid_candidate_endpoint");
            };
            Response {
                kind: RecordKind::AdmissionCounters,
                message_id: id,
                payload: router
                    .admission_counters_json(*candidate.as_bytes())
                    .into_bytes(),
            }
        }
        RecordKind::ControlCall => {
            let call = match parse_control_call_with(&record.payload, limits.control, |raw| {
                endpoint_key(raw).is_some()
            }) {
                Ok(call) => call,
                Err(code) => return failure(id, code),
            };
            match router
                .control_call(
                    call.destination,
                    Duration::from_millis(call.millis),
                    call.command,
                )
                .await
            {
                Ok(response) => Response {
                    kind: RecordKind::ControlCallResult,
                    message_id: id,
                    payload: response,
                },
                Err(code) => failure(id, code),
            }
        }
        RecordKind::Delivery
        | RecordKind::DeliveryFrom
        | RecordKind::TransportObservations
        | RecordKind::AdmissionCounters
        | RecordKind::ControlCallResult
        | RecordKind::Error => failure(id, "invalid_kind"),
    }
}

#[cfg(test)]
#[path = "sidecar_session_tests.rs"]
mod tests;
