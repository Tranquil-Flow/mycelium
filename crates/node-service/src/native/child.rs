// SPDX-License-Identifier: AGPL-3.0-only
//! Member / owner-leaf child process. Everything it says to, or hears from, the
//! owner crosses real Iroh QUIC: signed NodeControl pulls and commands, and the
//! Router ALPN for L5 activation bytes. There is no TCP socket in this process.
use super::{
    Result, activation,
    agent_worker::{AgentWorker, Fault, WorkerExit},
    header, host, number, phase, pressure, receipts, safety, service, span, text,
};
use crate::{
    artifact,
    control::{Incoming, Peer, Session, Wire},
    enrollment, iroh_frames,
};
use budgets::{BudgetId as B, WallExpiry};
use protocol::{canonical, identity::Identity};
use serde_json::{Value, json};
use stage_contract::Input;
use std::{path::Path, time::Instant};

/// Stats of a stage whose worker process is gone. The reap must be proven: this agent signs the
/// cleanup only for a worker its supervisor reaped without error.
fn dead_stats(exit: &WorkerExit) -> Result<Value> {
    if !exit.reaped {
        return Err("worker_reap_unproven".into());
    }
    Ok(json!({"context_closed":false,"worker_exit":exit.json()}))
}

/// Local member service: consent and owner pin are loaded from private member
/// state, never granted by the owner over the wire. UI pairing remains separate.
pub fn member(launch: &str, state_root: &Path, owner_leaf: bool) -> Result<()> {
    member_mode(launch, state_root, owner_leaf, false)
}
/// Explicit local qualification launcher; the owner cannot enable this clock.
pub fn qualification_member(launch: &str, state_root: &Path) -> Result<()> {
    member_mode(launch, state_root, false, true)
}
fn member_mode(
    launch: &str,
    state_root: &Path,
    owner_leaf: bool,
    allow_test_clock: bool,
) -> Result<()> {
    let pressure_watch = safety::PressureWatch::start()?;
    let signing = enrollment::identity(state_root)?;
    let local = if owner_leaf {
        None
    } else {
        Some(enrollment::LocalMembership::load(state_root)?)
    };
    let launch: Value = serde_json::from_str(launch)?;
    let owner_addr = iroh_frames::peer(&launch["owner"])?;
    // Member: the owner's signing key is the one in the persisted invitation.
    // Owner-leaf: a private child of the owner process, pinned by its spawner.
    let owner_key = match &local {
        Some(local) => local.invitation.owner_key.clone(),
        None => launch["owner_key"].clone(),
    };
    if !owner_key.is_object() {
        return Err("owner_pin_missing".into());
    }
    let wire_runtime = iroh_frames::runtime()?;
    let wire_clock = iroh_frames::Clock::new(0);
    let mut wire_host = iroh_frames::bind_persisted(
        &wire_runtime,
        &wire_clock,
        state_root,
        &iroh_frames::CHILD_CHANNELS,
    )?;
    let wire_address = iroh_frames::address(&wire_host)?;
    let key = if owner_leaf {
        signing
    } else {
        Identity::from_secret(text(&wire_address, "id")?, &signing.secret_bytes())?
    };
    let role = if owner_leaf { "a-owner" } else { "b-member" };
    let membership_address = wire_address["addresses"]
        .as_array()
        .and_then(|a| a.first())
        .and_then(Value::as_str)
        .ok_or("iroh_member_address")?
        .to_owned();
    let wire = Wire {
        host: &wire_host,
        clock: &wire_clock,
        runtime: &wire_runtime,
        signer: &key,
        watch: None,
    };
    let mut owner = Peer::new(owner_addr.clone(), 0, Session::child(owner_key));
    let request = owner.call(
        &wire,
        "register",
        json!({"node":role,"pid":std::process::id(),"key":key.public_key_record(),"iroh":wire_address}),
        B::WorkerStartup,
    )?;
    if request["qualification_clock"] == true && !allow_test_clock {
        return Err("qualification_clock_not_admitted".into());
    }
    let node = text(&request, "node")?;
    if node != role {
        return Err("local_role_mismatch".into());
    }
    let generation = number(&request, "generation")?;
    let report = host()?;
    let member_origin = Instant::now();
    let wall_origin = if request["qualification_clock"] == true {
        number(&request, "qualification_now")?
    } else {
        receipts::wall_ms()?
    };
    // The member holds no path to the owner's files: its checkpoint is staged
    // through the signed artifact carrier into private member state before it is
    // registered with membership/capability reports (they age while staging, and
    // staging is not worker startup). The owner's own co-located leaf
    // reads the owner's checkpoint path directly.
    let mut staged: Option<StagedFile> = None;
    let mut staged_path = None;
    let mut artifact_stats = json!(null);
    if let Some(local) = local.as_ref() {
        let offer = owner.call(&wire, "offer", json!({}), B::WorkerStartup)?;
        let guard = StagedFile::create(state_root)?;
        let path = guard.path.clone();
        let fetched = stage_artifact(
            &wire,
            &key,
            &local.invitation.owner_key,
            &owner_addr,
            node,
            &offer,
            &path,
        )?;
        staged = Some(guard);
        artifact_stats =
            json!({"chunks":fetched.0,"bytes":fetched.1,"staged_via":"signed-artifact-chunk"});
        staged_path = Some(path);
    }
    let member_now = || -> Result<WallExpiry> {
        Ok(WallExpiry::from_unix_millis(
            wall_origin
                .checked_add(u64::try_from(member_origin.elapsed().as_millis())?)
                .ok_or("member_clock_overflow")?,
        ))
    };
    let now = member_now()?.unix_millis();
    let membership_generation = request
        .get("membership_generation")
        .unwrap_or(&request["generation"]);
    let member_incarnation = request
        .get("member_incarnation")
        .unwrap_or(&request["request_id"]);
    let mut join_request = if let Some(local) = &local {
        if local.invitation.swarm != text(&request, "swarm_id")?
            || local.invitation.owner_key != request["owner_key"]
            || local.invitation.member_key != key.public_key_record()
            || local.consent.withdrawn_at.is_some()
        {
            return Err("local_membership_pin_mismatch".into());
        }
        Some(enrollment::join(
            &key,
            &local.invitation,
            member_incarnation.as_str().ok_or("member_incarnation")?,
            &membership_address,
            WallExpiry::from_unix_millis(now),
        )?)
    } else {
        None
    };
    let statement = json!({"protocol":"mycelium.membership.capability_report.v1","swarm_id":request["swarm_id"],"message_id":service::random_id().map_err(|e|e.code)?,
        "sender_node_id":node,"sender_endpoint_id":key.endpoint_id(),"recipient_node_id":"a-owner","platform":"darwin","architecture":"arm64","issued_at":now as f64/1000.,"expires_at":(now+span(B::MembershipTtl).millis()) as f64/1000.,
        "generation":membership_generation,"incarnation":member_incarnation,"memory_bytes":report["memory"],"available_storage_bytes":report["storage"],
        "backends":["mlx"],"precisions":["int8-weight-only"]});
    let envelope = json!({"protocol":protocol::membership::SIGNED_MESSAGE_PROTOCOL,"message":statement,"signature":key.sign(&statement)?,"verification_key":key.public_key_record()});
    owner.call(
        &wire,
        "hello",
        json!({"capability":envelope,"key":key.public_key_record(),"endpoint":key.endpoint_id(),"pid":std::process::id(),"join":join_request,"iroh":wire_address}),
        B::WorkerStartup,
    )?;
    let plan = owner.call(&wire, "plan", json!({}), B::WorkerStartup)?;
    let caps: node::Caps = if let Some(local) = &local {
        local.consent.caps.clone()
    } else {
        serde_json::from_value(request["caps"].clone())?
    };
    if number(&plan, "memory")? > caps.memory_bytes {
        return Err("member_assignment_exceeds_consent".into());
    }
    if let Some(local) = &local {
        local.verify_acceptance(
            join_request.as_ref().ok_or("join_request_missing")?,
            &plan["acceptance"],
            member_now()?,
        )?;
        if plan["acceptance"]["message"]["membership_generation"] != *membership_generation {
            return Err("member_acceptance_generation".into());
        }
    }
    let start = u32::try_from(number(&plan, "start")?)?;
    let checkpoint = if owner_leaf {
        if plan.get("artifact").is_some() {
            return Err("owner_leaf_artifact_refused".into());
        }
        std::path::PathBuf::from(text(&plan, "checkpoint")?)
    } else {
        if plan.get("checkpoint").is_some() {
            return Err("member_checkpoint_path_refused".into());
        }
        staged_path.ok_or("member_artifact_not_staged")?
    };
    // RU-020: this process is the AGENT (signer, NodeControl, Router carrier). The compute
    // lives in its own native worker process, which this agent survives.
    // This existing signed plan has ONLY the legacy dense schema. Do not add
    // a stage_image field to it or infer MoE from a filename/header. A future
    // authenticated MoE source-plan adapter must call start_assigned explicitly.
    let image = crate::native_selection::legacy_plan_image(&plan, owner_leaf)?;
    let mut stage = Some(AgentWorker::start_assigned(
        &checkpoint,
        &image,
        number(&plan, "memory")?,
        u32::try_from(number(&plan, "tokens")?)?,
        text(&request, "request_id")?,
        generation,
    )?);
    // Set once the worker is gone: the agent stays up, answers `close` with its signed receipt.
    let mut dead: Option<WorkerExit> = None;
    owner.call(
        &wire,
        "ready",
        json!({"ready":true,"membership_accepted":node=="b-member"}),
        B::WorkerStartup,
    )?;
    let request_id = text(&request, "request_id")?.to_owned();
    let mut frames: Option<iroh_frames::Frames<'_>> = None;
    let mut position = 0;
    let mut step_index = 0;
    let closed = loop {
        let connection = match wire.next(B::MemberChosenWall)? {
            Incoming::Router(connection) => {
                if frames.is_some() || connection.remote_id() != owner_addr.id {
                    connection.close(2u32.into(), b"unpinned router carrier");
                    return Err("iroh_router_carrier_refused".into());
                }
                frames = Some(iroh_frames::Frames::new(
                    connection,
                    &wire_runtime,
                    &wire_clock,
                    &owner_addr,
                    generation,
                )?);
                continue;
            }
            Incoming::Artifact(connection) => {
                connection.close(2u32.into(), b"no artifact service");
                return Err("iroh_artifact_not_served".into());
            }
            Incoming::Control(connection) => connection,
        };
        if connection.remote_id() != owner_addr.id {
            connection.close(2u32.into(), b"unpinned control caller");
            continue;
        }
        let mut receiver = wire.receiver(&connection, generation)?;
        let call = receiver
            .receive(wire_clock.deadline(B::MemberChosenWall)?)
            .map_err(|e| format!("node control receive: {e}"))?;
        let (seq, kind, control) = owner.session.accept_call(call.bytes())?;
        let reply = |body: Value| owner.session.reply(&key, &kind, seq, body);
        match kind.as_str() {
            "renew_prepare" => {
                if request["qualification_clock"] != true {
                    return Err("qualification_clock_not_enabled".into());
                }
                let local = local.as_ref().ok_or("renewal_member_required")?;
                let fresh = enrollment::join(
                    &key,
                    &local.invitation,
                    member_incarnation.as_str().ok_or("member_incarnation")?,
                    &membership_address,
                    WallExpiry::from_unix_millis(number(&control, "qualification_now")?),
                )?;
                call.respond(&reply(json!({"join":fresh}))?)
                    .map_err(|e| format!("node control respond: {e}"))?;
                wire.consumed(&connection);
                join_request = Some(fresh);
            }
            "renew" => {
                if request["qualification_clock"] != true {
                    return Err("qualification_clock_not_enabled".into());
                }
                let local = local.as_ref().ok_or("renewal_member_required")?;
                local.verify_acceptance(
                    join_request.as_ref().ok_or("join_request_missing")?,
                    &control["acceptance"],
                    WallExpiry::from_unix_millis(number(&control, "qualification_now")?),
                )?;
                let statement = json!({"accepted":canonical::digest(&control["acceptance"])?});
                call.respond(&reply(
                    json!({"signature":key.sign(&statement)?,"statement":statement}),
                )?)
                .map_err(|e| format!("node control respond: {e}"))?;
                wire.consumed(&connection);
            }
            "close" => {
                // A live worker closes its stage and is reaped. A worker that died (earlier, or
                // while closing) was reaped by its supervisor: the agent still signs, because
                // the OS released the process and the staged copy is removed below. A worker
                // whose reap is not proven is never signed for.
                let mut stats = match (stage.take(), dead.take()) {
                    (_, Some(exit)) => dead_stats(&exit)?,
                    (Some(worker), None) => match worker.close() {
                        Ok((stats, _)) => stats,
                        Err(Fault::Died(exit)) => dead_stats(&exit)?,
                        Err(Fault::Other(code)) => return Err(code.into()),
                    },
                    (None, None) => return Err("worker_not_prepared".into()),
                };
                if let Some(file) = staged.take() {
                    file.remove()?;
                    artifact_stats["staged_file_removed"] = json!(true);
                }
                stats["artifact"] = artifact_stats.clone();
                if let Some(carrier) = frames.take() {
                    carrier.close()?;
                }
                // The Router carrier is closed and the native stage is gone. The
                // NodeControl endpoint stays up only to deliver this reply; its
                // own close is proven below by this process exiting successfully.
                stats["iroh_router_closed"] = json!(true);
                stats["pressure_samples"] = json!(pressure_watch.samples());
                stats["pressure_max"] = json!(1);
                let response = receipts::cleaned(&key, &control["subject"], generation, node)?;
                call.respond(&reply(json!({"cleanup":response,"stats":stats}))?)
                    .map_err(|e| format!("node control respond: {e}"))?;
                wire.consumed(&connection);
                break true;
            }
            "step" => {
                let phase = if step_index == 0 { "PREFILL" } else { "DECODE" };
                let h = header(&request_id, generation, position, phase);
                // Reject a step that does not match this stage before acknowledging.
                if control.get("tokens").is_some() != (start == 0) {
                    return Err(if start == 0 {
                        "unexpected_activation".into()
                    } else {
                        "unexpected_tokens".into()
                    });
                }
                call.respond(&reply(json!({"accepted":true}))?)
                    .map_err(|e| format!("node control respond: {e}"))?;
                wire.consumed(&connection);
                let (input, count) = if let Some(tokens) = control.get("tokens") {
                    let ids: Vec<u32> = serde_json::from_value(tokens.clone())?;
                    let count = u32::try_from(ids.len())?;
                    (Input::Tokens(ids), count)
                } else {
                    let raw = frames.as_mut().ok_or("iroh_router_missing")?.receive(&h)?;
                    let a = activation::unframe(&raw, &h)?;
                    let count = a.shape[1];
                    (Input::Hidden(a), count)
                };
                let stepped = match (stage.as_mut(), dead.as_ref()) {
                    (_, Some(exit)) => Err(Fault::Died(exit.clone())),
                    (Some(worker), None) => worker.step(input),
                    (None, None) => Err(Fault::Other("worker_not_prepared".into())),
                };
                let activation = match stepped {
                    Ok(activation) => activation,
                    Err(Fault::Died(exit)) => {
                        // The worker is gone, the agent is not. Release the owner now: it is
                        // blocked on this step's activation, so close the Router carrier (its
                        // read fails at once) and keep serving NodeControl for the cleanup.
                        phase::mark("agent_survived_worker_death", &format!("pid={}", exit.pid));
                        if let Some(carrier) = frames.take() {
                            carrier.close()?;
                        }
                        dead = Some(exit);
                        continue;
                    }
                    Err(Fault::Other(code)) => return Err(code.into()),
                };
                let output_frame = activation::frame(&h, &activation)?;
                frames
                    .as_mut()
                    .ok_or("iroh_router_missing")?
                    .send(&output_frame)?;
                position = position.checked_add(count).ok_or("position_overflow")?;
                step_index += 1;
            }
            other => return Err(format!("unknown_control_command:{other}").into()),
        }
    };
    drop(frames);
    if closed {
        iroh_frames::close(&mut wire_host, &wire_clock)?;
    }
    drop(wire_host);
    drop(wire_runtime);
    pressure()?;
    Ok(())
}

/// Private, owner-only staging file; removed on close and on any error path.
struct StagedFile {
    path: std::path::PathBuf,
    removed: bool,
}
impl StagedFile {
    fn create(state_root: &Path) -> Result<Self> {
        let staging = state_root.join("staging");
        // Descriptor-relative, no-follow, owner-only (0700) directory validation.
        crate::private_state::PrivateRoot::open(&staging)?;
        let path = staging.join("model.safetensors");
        // A stale file in our private dir (crashed prior run) is replaced, never reused.
        if std::fs::symlink_metadata(&path).is_ok() {
            std::fs::remove_file(&path)?;
        }
        Ok(Self {
            path,
            removed: false,
        })
    }
    fn remove(mut self) -> Result<()> {
        std::fs::remove_file(&self.path)?;
        self.removed = true;
        if self.path.exists() {
            return Err("staged_artifact_residue".into());
        }
        Ok(())
    }
}
impl Drop for StagedFile {
    fn drop(&mut self) {
        if !self.removed && std::fs::remove_file(&self.path).is_err() {
            eprintln!("staged artifact removal failed on error path");
        }
    }
}
/// Fetch the granted pack segment-by-segment over the signed artifact ALPN.
fn stage_artifact(
    wire: &Wire<'_>,
    key: &Identity,
    owner_key: &Value,
    owner_addr: &iroh::EndpointAddr,
    node: &str,
    offer: &Value,
    path: &Path,
) -> Result<(u64, u64)> {
    use protocol::artifact_grant::{self as ag, ChunkRequest};
    use std::os::unix::fs::{FileExt as _, OpenOptionsExt as _};
    let limits = artifact::grant_limits()?;
    let grant = ag::verify_grant(&offer["grant"], owner_key, receipts::wall_ms()?, limits)
        .map_err(|e| format!("artifact grant: {e}"))?;
    let segments = artifact::segments(&offer["manifest"])?;
    let pack = artifact::pack_digest(&segments)?;
    grant
        .check_binding(
            node,
            key.endpoint_id(),
            &key.verification_key_digest(),
            &pack,
        )
        .map_err(|e| format!("artifact grant binding: {e}"))?;
    let source = owner_addr.id.to_string();
    let size = number(offer, "size")?;
    let mut expected_offset = 0u64;
    for s in &segments {
        if s.offset != expected_offset
            || grant
                .allowed_object_digests
                .binary_search(&s.digest)
                .is_err()
        {
            return Err("artifact_manifest_binding".into());
        }
        expected_offset = expected_offset
            .checked_add(s.length)
            .ok_or("artifact_manifest_overflow")?;
    }
    if expected_offset != size
        || size != grant.maximum_total_bytes
        || !grant.sources.contains_key(&source)
    {
        return Err("artifact_manifest_binding".into());
    }
    let wire_limits = artifact::wire_limits(&offer["wire"])?;
    let local = wire
        .host
        .address()
        .map_err(|e| format!("iroh address: {e:?}"))?
        .id;
    let connection = wire
        .host
        .connect_channel(
            owner_addr,
            transport::Channel::ArtifactChunk,
            wire.clock,
            wire.clock.deadline(B::WorkerStartup)?,
        )
        .map_err(|e| format!("artifact connect: {e:?}"))?;
    let handle = connection.clone();
    let mut session = runtime::iroh_artifact::ChunkSession::new(
        connection,
        wire.runtime,
        wire.clock,
        local,
        wire_limits,
    )
    .map_err(|e| format!("artifact session: {e}"))?;
    let file = std::fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .mode(0o600)
        .open(path)?;
    let mut bytes = 0u64;
    for (index, segment) in segments.iter().enumerate() {
        let id = artifact::request_id(u64::try_from(index)?);
        let envelope = ag::sign_chunk_request(
            key,
            &grant,
            &id,
            &artifact::nonce(&id),
            &source,
            &segment.digest,
            0,
            segment.length,
            receipts::wall_ms()?,
            limits,
        )
        .map_err(|e| format!("artifact request: {e}"))?;
        let request = ChunkRequest {
            request_id: id,
            source_endpoint_id: source.clone(),
            object_digest: segment.digest.clone(),
            offset_bytes: 0,
            length_bytes: segment.length,
        };
        let data = session
            .fetch(
                &envelope,
                &request,
                &grant,
                wire.clock.deadline(B::WorkerStartup)?,
            )
            .map_err(|e| format!("artifact chunk {index}: {e}"))?;
        if data.len() as u64 != segment.length || ag::range_digest(&data) != segment.digest {
            return Err("artifact_segment_mismatch".into());
        }
        file.write_all_at(&data, segment.offset)?;
        bytes += segment.length;
    }
    file.sync_all()?;
    drop(session);
    handle.close(0u32.into(), b"artifact staged");
    Ok((u64::try_from(segments.len())?, bytes))
}
