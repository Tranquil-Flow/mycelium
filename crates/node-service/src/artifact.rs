// SPDX-License-Identifier: AGPL-3.0-only
//! Signed artifact staging over L9's `mycelium.artifact_chunk.v1` carrier.
//! The owner is the source: it seed-signs one grant bound to the member's node,
//! Iroh endpoint and key, and serves behind a fsynced admission journal. The
//! checkpoint is a pack of fixed-size content-addressed segments (the grant's
//! `allowed_object_digests`); the member never receives a filesystem path, it
//! requests each segment, verifies every signed receipt, and checks the pack
//! digest before native admission. Development scope: the journal is a
//! fixture-grade append-only file (no restart recovery/pruning); the unmet L6/L7
//! production journal seam recorded by L9 is NOT closed here.
use crate::loopback::Result;
use budgets::{BudgetId as B, Bytes, Entries, Span};
use protocol::{
    artifact_grant::{self as ag, ChunkRequest, Grant, GrantLimits},
    identity::Identity,
};
use runtime::{
    iroh_artifact::{ArtifactError, ArtifactWireLimits},
    iroh_artifact_source::{AdmittedChunk, ArtifactObjects, ArtifactServeLimits},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs::File,
    io::{Read, Write},
    os::unix::fs::FileExt,
    path::{Path, PathBuf},
    sync::Mutex,
};

pub fn grant_limits() -> Result<GrantLimits> {
    let ms = |id: B| -> Result<u64> {
        Ok(id
            .span(Entries::ZERO)
            .map_err(|e| format!("grant limit: {e:?}"))?
            .millis())
    };
    Ok(GrantLimits {
        max_grant_ttl_ms: ms(B::PyMyceliumMobileArtifactGrantMaxGrantTtlMsL38)?,
        max_request_ttl_ms: ms(B::PyMyceliumMobileArtifactGrantMaxRequestTtlMsL39)?,
        request_ttl_ms: ms(B::PyFinalMyceliumMobileArtifactGrantTtlMsL309C18)?,
        clock_skew_ms: ms(B::PyMyceliumMobileArtifactGrantClockSkewMsL40)?,
    })
}
fn chunk_bytes() -> Result<u64> {
    Ok(B::PyShiftMyceliumMobileArtifactGrantMaxChunkBytesL42C0
        .bytes()
        .map_err(|e| format!("chunk bytes: {e:?}"))?
        .get())
}
fn rate_bytes_per_second() -> Result<u64> {
    match B::PyShiftMyceliumMobileArtifactGrantMaximumBytesPerSecondL210C36
        .read_entry()
        .value
    {
        budgets::table::Value::BytesPerSecond(rate) if rate.get() > 0 => Ok(rate.get()),
        _ => Err("artifact_rate_budget".into()),
    }
}

/// One content-addressed segment of the pack, in file order.
#[derive(Clone, Debug)]
pub struct Segment {
    pub offset: u64,
    pub length: u64,
    pub digest: String,
}
/// The checkpoint as an ordered pack, measured once per driver (not per request).
pub struct Source {
    pub path: PathBuf,
    pub size: u64,
    pub segments: Vec<Segment>,
    pub pack_digest: String,
}
pub fn manifest_value(segments: &[Segment]) -> Value {
    Value::Array(
        segments
            .iter()
            .map(|s| json!([s.offset, s.length, s.digest]))
            .collect(),
    )
}
pub fn pack_digest(segments: &[Segment]) -> Result<String> {
    Ok(ag::range_digest(&protocol::canonical::to_bytes(
        &manifest_value(segments),
    )?))
}
/// Drop the member's staged model copy after a failed run and keep what it was.
///
/// The supervisor kills a failed run's workers, so the member's own removal-on-drop never runs and a
/// ~942 MB copy would be left in its private state. The owner is co-located with that state in this
/// development slice. The copy is measured exactly as the source is (size and pack digest, and
/// whether it equals the offered source) and then removed; `record` retains the evidence. Returns
/// `None` when nothing was staged. The staging integrity check itself is not involved.
pub fn discard_staged(
    member_state: &Path,
    source_pack_digest: &str,
    record: &Path,
) -> Result<Option<Value>> {
    let path = member_state.join("staging").join("model.safetensors");
    match std::fs::symlink_metadata(&path) {
        Ok(meta) if meta.is_file() => {}
        Ok(_) => return Err("staged_artifact_not_a_file".into()),
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let measured = Source::measure(&path)?;
    std::fs::remove_file(&path)?;
    let outcome = json!({
        "size": measured.size,
        "pack_digest": measured.pack_digest,
        "source_pack_digest": source_pack_digest,
        "matches_source": measured.pack_digest == source_pack_digest,
        "removed": !path.exists(),
    });
    std::fs::write(record, serde_json::to_vec_pretty(&outcome)?)?;
    Ok(Some(outcome))
}
impl Source {
    pub fn measure(path: &Path) -> Result<Self> {
        let mut file = File::open(path)?;
        let size = file.metadata()?.len();
        let step = chunk_bytes()?;
        let mut buffer = vec![0u8; usize::try_from(step)?];
        let (mut segments, mut offset) = (Vec::new(), 0u64);
        while offset < size {
            let length = step.min(size - offset);
            let slice = &mut buffer[..usize::try_from(length)?];
            file.read_exact(slice)?;
            segments.push(Segment {
                offset,
                length,
                digest: ag::range_digest(slice),
            });
            offset += length;
        }
        let pack_digest = pack_digest(&segments)?;
        Ok(Self {
            path: path.to_owned(),
            size,
            segments,
            pack_digest,
        })
    }
    /// Sorted, unique object digests (identical segments are one object).
    fn objects(&self) -> Vec<String> {
        self.segments
            .iter()
            .map(|s| s.digest.clone())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect()
    }
}

/// Development object store: commit (fence + charge + fsync) strictly before read.
pub struct CheckpointObjects {
    file: File,
    objects: BTreeMap<String, (u64, u64)>,
    journal: Mutex<(File, BTreeSet<String>, u64)>,
    pub reads: std::sync::atomic::AtomicU64,
}
impl CheckpointObjects {
    pub fn open(source: &Source, journal: &Path) -> Result<Self> {
        let mut objects = BTreeMap::new();
        for s in &source.segments {
            objects
                .entry(s.digest.clone())
                .or_insert((s.offset, s.length));
        }
        Ok(Self {
            file: File::open(&source.path)?,
            objects,
            journal: Mutex::new((
                std::fs::OpenOptions::new()
                    .create_new(true)
                    .append(true)
                    .open(journal)?,
                BTreeSet::new(),
                0,
            )),
            reads: 0.into(),
        })
    }
}
impl ArtifactObjects for CheckpointObjects {
    fn verified_size(&self, digest: &str) -> Option<u64> {
        self.objects.get(digest).map(|(_, length)| *length)
    }
    fn admit_and_read(
        &self,
        grant: &Grant,
        request: &ChunkRequest,
        nonce: &str,
        _request_expiry: u64,
        _now: u64,
    ) -> std::result::Result<AdmittedChunk, ArtifactError> {
        let refuse = |code: &str| ArtifactError(code.into());
        let (base, _) = self
            .objects
            .get(&request.object_digest)
            .ok_or_else(|| refuse("artifact_object_unavailable"))?;
        let fence = format!("{}\0{}\0{nonce}", grant.grant_id, request.request_id);
        let mut state = self
            .journal
            .lock()
            .map_err(|_| refuse("artifact_source_internal_error"))?;
        if state.1.contains(&fence) {
            return Err(refuse("artifact_chunk_request_replay"));
        }
        let charged = state
            .2
            .checked_add(request.length_bytes)
            .filter(|n| *n <= grant.maximum_total_bytes)
            .ok_or_else(|| refuse("artifact_grant_budget_exhausted"))?;
        writeln!(state.0, "{}", ag::range_digest(fence.as_bytes()))
            .and_then(|()| state.0.sync_data())
            .map_err(|_| refuse("artifact_source_internal_error"))?;
        state.1.insert(fence);
        state.2 = charged;
        drop(state);
        let mut bytes = vec![
            0u8;
            usize::try_from(request.length_bytes)
                .map_err(|_| refuse("artifact_chunk_range_invalid"))?
        ];
        self.file
            .read_exact_at(&mut bytes, base + request.offset_bytes)
            .map_err(|_| refuse("artifact_source_internal_error"))?;
        self.reads
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        // Honor the grant's own rate ceiling: the response is held for the time the
        // granted bytes-per-second allows.
        let pace = request
            .length_bytes
            .saturating_mul(1000)
            .div_ceil(grant.maximum_bytes_per_second);
        Ok(AdmittedChunk {
            bytes,
            pace: Span::from_millis(pace),
        })
    }
}

/// Seed-signed grant for exactly one pack, one source, one recipient.
pub struct Offer {
    pub grant: Value,
    pub manifest: Value,
    pub size: u64,
    pub wire: Value,
}
#[allow(clippy::too_many_arguments)]
pub fn offer(
    seed: &Identity,
    swarm: &str,
    generation: u64,
    recipient_node: &str,
    recipient_endpoint: &str,
    recipient_key_digest: &str,
    source_endpoint: &str,
    source: &Source,
    now_ms: u64,
) -> Result<Offer> {
    let limits = grant_limits()?;
    let ttl = B::PyMyceliumMobileArtifactGrantDefaultGrantTtlMsL37
        .span(Entries::ZERO)
        .map_err(|e| format!("grant ttl: {e:?}"))?
        .millis();
    let message = json!({
        "protocol": ag::GRANT_PROTOCOL,
        "grant_id": format!("slice-{generation}-grant"),
        "swarm_id": swarm,
        "seed_node_id": "a-owner",
        "recipient_node_id": recipient_node,
        "recipient_endpoint_id": recipient_endpoint,
        "recipient_key_digest": recipient_key_digest,
        "assignment_id": format!("slice-{generation}-{recipient_node}"),
        "manifest_pack_digest": source.pack_digest,
        "allowed_object_digests": source.objects(),
        "authorized_sources": [{"endpoint_id": source_endpoint,
            "verification_key_digest": seed.verification_key_digest(), "direct_addrs": []}],
        "maximum_total_bytes": source.size,
        "maximum_bytes_per_second": rate_bytes_per_second()?,
        "maximum_chunk_bytes": chunk_bytes()?,
        "issued_at_unix_ms": now_ms,
        "not_before_unix_ms": now_ms,
        "expires_at_unix_ms": now_ms + ttl,
    });
    let grant = json!({"protocol": protocol::membership::SIGNED_MESSAGE_PROTOCOL,
        "signature": seed.sign(&message)?, "verification_key": seed.public_key_record(),
        "message": message});
    let verified = ag::verify_grant(&grant, &seed.public_key_record(), now_ms, limits)
        .map_err(|e| format!("own grant invalid: {e}"))?;
    // Wire limits are derived from a worst-case exemplar of this very grant (a
    // fixed-width id/nonce, a full-length range) signed with stand-in keys of
    // identical size, not invented. The registry has no source-specific
    // identifier for them yet (round4-seam-requests.json).
    let exemplar_member = Identity::from_secret(recipient_endpoint, &[7u8; 32])?;
    let exemplar_source = Identity::from_secret(source_endpoint, &[9u8; 32])?;
    let length = chunk_bytes()?.min(source.size);
    let id = request_id(u64::MAX);
    let digest = source.segments[0].digest.clone();
    let envelope = ag::sign_chunk_request(
        &exemplar_member,
        &verified,
        &id,
        &nonce(&id),
        source_endpoint,
        &digest,
        0,
        length,
        now_ms,
        limits,
    )
    .map_err(|e| format!("exemplar request: {e}"))?;
    let request_bytes = protocol::canonical::to_bytes(&envelope)?.len();
    let request = ChunkRequest {
        request_id: id,
        source_endpoint_id: source_endpoint.into(),
        object_digest: digest,
        offset_bytes: 0,
        length_bytes: length,
    };
    let receipt = ag::sign_chunk_receipt(
        &exemplar_source,
        &request,
        &verified,
        &vec![0u8; usize::try_from(length)?],
        now_ms,
    )
    .map_err(|e| format!("exemplar receipt: {e}"))?;
    let header = json!({"protocol": runtime::iroh_artifact::RESPONSE_PROTOCOL,
        "status": "ok", "receipt": receipt});
    let header_bytes = protocol::canonical::to_bytes(&header)?.len();
    Ok(Offer {
        grant,
        manifest: manifest_value(&source.segments),
        size: source.size,
        wire: json!({"request_bytes": request_bytes, "header_bytes": header_bytes}),
    })
}
pub fn request_id(index: u64) -> String {
    format!("chunk-{index:020}")
}
pub fn nonce(request_id: &str) -> String {
    format!("nonce-{request_id}")
}
pub fn segments(manifest: &Value) -> Result<Vec<Segment>> {
    manifest
        .as_array()
        .ok_or("artifact_manifest")?
        .iter()
        .map(|row| {
            Ok(Segment {
                offset: row[0].as_u64().ok_or("artifact_manifest")?,
                length: row[1].as_u64().ok_or("artifact_manifest")?,
                digest: row[2].as_str().ok_or("artifact_manifest")?.to_owned(),
            })
        })
        .collect()
}
/// Wire limits are the v0.1.4 `artifact_net.rs` caps (`MAX_REQUEST_BYTES`, `MAX_HEADER_BYTES`,
/// `IO_TIMEOUT`) from the registry. The offer carries the sizes of the signed exemplars the
/// owner actually built; an exemplar larger than a cap is refused rather than served.
pub fn wire_limits(wire: &Value) -> Result<ArtifactWireLimits> {
    let offered = |k: &str| -> Result<u64> {
        wire[k]
            .as_u64()
            .filter(|n| *n > 0)
            .ok_or_else(|| "artifact_wire".into())
    };
    let cap = |id: B| -> Result<Bytes> {
        id.bytes()
            .map_err(|e| format!("artifact cap: {e:?}").into())
    };
    let request_bytes = cap(B::ArtifactMaxRequestBytes)?;
    let header_bytes = cap(B::ArtifactMaxHeaderBytes)?;
    if offered("request_bytes")? > request_bytes.get()
        || offered("header_bytes")? > header_bytes.get()
    {
        return Err("artifact_wire_exceeds_limit".into());
    }
    Ok(ArtifactWireLimits {
        request_bytes,
        header_bytes,
        io: B::ArtifactIoTimeout
            .span(Entries::ZERO)
            .map_err(|e| format!("artifact io: {e:?}"))?,
    })
}
/// Serving limits: the registered v0.1.4 connection, per-peer and blocking-work slots and
/// read/idle waits, and a pace ceiling equal to one full granted chunk at the granted rate
/// (the source `MAX_PACE`, `ArtifactMaxPace`, is the upper bound of that pace).
pub fn serve_limits(wire: ArtifactWireLimits) -> Result<ArtifactServeLimits> {
    let span = |id: B, what: &str| -> Result<Span> {
        id.span(Entries::ZERO)
            .map_err(|e| format!("artifact {what}: {e:?}").into())
    };
    let count = |id: B, what: &str| -> Result<Entries> {
        id.count()
            .map_err(|e| format!("artifact {what}: {e:?}").into())
    };
    let pace = Span::from_millis(
        chunk_bytes()?
            .saturating_mul(1000)
            .div_ceil(rate_bytes_per_second()?),
    );
    let ceiling = span(B::ArtifactMaxPace, "pace ceiling")?;
    Ok(ArtifactServeLimits {
        wire,
        connections: count(B::ArtifactMaxConnections, "connections")?,
        per_peer: count(B::ArtifactMaxConnectionsPerPeer, "per peer")?,
        blocking: count(B::ArtifactMaxBlockingWork, "blocking work")?,
        read: span(B::ArtifactReadTimeout, "read")?,
        idle: span(B::ArtifactIdleTimeout, "idle")?,
        maximum_pace: if pace > ceiling {
            return Err("artifact_pace_exceeds_source_ceiling".into());
        } else {
            pace
        },
    })
}
