// SPDX-License-Identifier: AGPL-3.0-only
//! The sidecar's durable artifact-admission journal and content-verified object store
//! (`native/mobile_core/src/artifact_net.rs:118-260,391-433 @ f4e366e9`), behind the
//! [`ArtifactObjects`] seam of [`crate::iroh_artifact_source::ArtifactService`].
//!
//! Persisted format (the same bytes the v0.1.4 sidecar writes to `<state>/source-ledger.jsonl`):
//! a genesis header line, then one JSON line per admitted request. Opening replays the file,
//! keeps the entries of grants still presentable, and rewrites it compacted (tmp, fsync, rename,
//! directory fsync). Every admission is appended and fsynced under the same lock as the replay
//! fence and the per-grant byte budget, before any byte is read. A failed append poisons the
//! journal. A grant whose window began before genesis is refused. Fence entries live until their
//! request window has passed; at capacity new requests are refused, never evicting.
//!
//! The fence capacity and the clock skew are caller-supplied (the binary reads them from the
//! budget registry); no bound is hard-coded here.
use crate::{
    iroh_artifact::ArtifactError,
    iroh_artifact_source::{AdmittedChunk, ArtifactObjects},
};
use budgets::Span;
use protocol::artifact_grant::{ChunkRequest, Grant, range_digest};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    fs::{File, OpenOptions},
    io::{Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};

pub const LEDGER_FILE: &str = "source-ledger.jsonl";
const LEDGER_TMP: &str = "source-ledger.jsonl.tmp";
const LEDGER_PROTOCOL: &str = "mycelium.artifact_source_ledger.v1";

pub struct DurableConfig {
    pub objects: PathBuf,
    pub state_dir: PathBuf,
    /// Live fence entries per source; at capacity new requests are refused.
    pub fence_capacity: usize,
    /// How long after its expiry a grant or request can still be presented.
    pub clock_skew_ms: u64,
    pub now: Arc<dyn Fn() -> u64 + Send + Sync>,
}

fn error(code: &str) -> ArtifactError {
    ArtifactError(code.to_owned())
}

struct Journal {
    path: PathBuf,
    file: File,
}
struct SourceState {
    /// Fence key -> the request's expiry (kept until it cannot be replayed).
    fence: BTreeMap<String, u64>,
    fence_capacity: usize,
    /// grant id -> (bytes served, first serve instant) for budget and pacing.
    grants: BTreeMap<String, (u64, Instant)>,
    /// object digest hex -> verified length.
    verified: BTreeMap<String, u64>,
    /// `None` once an append failed: everything is refused until the source is reopened.
    journal: Option<Journal>,
    genesis_unix_ms: u64,
    clock_skew_ms: u64,
}

impl SourceState {
    fn admit(&mut self, fence: String, request_expires: u64, grant_id: &str, bytes: u64, now: u64) {
        self.prune(now);
        self.fence.insert(fence, request_expires);
        let entry = self
            .grants
            .entry(grant_id.to_owned())
            .or_insert((0, Instant::now()));
        entry.0 += bytes;
    }
    /// Forget fence entries whose request can no longer be presented.
    fn prune(&mut self, now: u64) {
        let skew = self.clock_skew_ms;
        self.fence
            .retain(|_, expires| expires.saturating_add(skew) >= now);
    }
    /// The whole admission decision for one request, under the caller's lock: refuse, or journal
    /// durably and charge. Returns the pacing delay in milliseconds.
    fn admit_request(
        &mut self,
        grant: &Grant,
        fence_key: String,
        request_expires: u64,
        length: u64,
        now: u64,
    ) -> Result<u64, ArtifactError> {
        if self.journal.is_none() {
            return Err(error("artifact_source_ledger_unavailable"));
        }
        if grant.not_before_unix_ms < self.genesis_unix_ms {
            return Err(error("artifact_source_ledger_reset"));
        }
        self.prune(now);
        if self.fence.contains_key(&fence_key) {
            return Err(error("artifact_chunk_request_replay"));
        }
        if self.fence.len() >= self.fence_capacity {
            return Err(error("artifact_source_busy"));
        }
        let (served, since) = *self
            .grants
            .entry(grant.grant_id.clone())
            .or_insert((0, Instant::now()));
        if served.saturating_add(length) > grant.maximum_total_bytes {
            return Err(error("artifact_grant_budget_exhausted"));
        }
        let appended = self.journal.as_mut().is_some_and(|journal| {
            journal
                .append(grant, &fence_key, request_expires, length)
                .is_err()
        });
        if appended {
            // Possibly a partial line on disk: poison the journal so no later admission is glued
            // onto it and lost at replay.
            self.journal = None;
            return Err(error("artifact_source_ledger_unavailable"));
        }
        self.admit(fence_key, request_expires, &grant.grant_id, length, now);
        // Pacing: the grant's served bytes may not run ahead of its rate.
        let rate = grant.maximum_bytes_per_second.max(1);
        let due_ms = served.saturating_add(length).saturating_mul(1000) / rate;
        let elapsed = u64::try_from(since.elapsed().as_millis()).unwrap_or(u64::MAX);
        Ok(due_ms.saturating_sub(elapsed))
    }
}

impl Journal {
    /// Replays `<dir>/source-ledger.jsonl` into `state` (a new ledger gets genesis = now), keeps
    /// the entries of grants still inside their window, and rewrites it compacted.
    fn open(dir: &Path, state: &mut SourceState, now: u64) -> Result<Self, ArtifactError> {
        let unavailable = |_: std::io::Error| error("artifact_source_ledger_unavailable");
        std::fs::create_dir_all(dir).map_err(unavailable)?;
        let path = dir.join(LEDGER_FILE);
        let mut kept: Vec<String> = Vec::new();
        let genesis = match std::fs::symlink_metadata(&path) {
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => now,
            Err(_) => return Err(error("artifact_source_ledger_unavailable")),
            Ok(meta) if !meta.file_type().is_file() => {
                return Err(error("artifact_source_ledger_corrupt"));
            }
            Ok(_) => {
                let raw = std::fs::read_to_string(&path).map_err(unavailable)?;
                let complete = raw.ends_with('\n');
                let lines: Vec<&str> = raw.lines().collect();
                let corrupt = || error("artifact_source_ledger_corrupt");
                let header: Value = lines
                    .first()
                    .and_then(|line| serde_json::from_str(line).ok())
                    .ok_or_else(corrupt)?;
                let genesis = match (
                    header["protocol"].as_str(),
                    header["genesis_unix_ms"].as_u64(),
                ) {
                    (Some(LEDGER_PROTOCOL), Some(genesis)) if lines.len() > 1 || complete => {
                        genesis
                    }
                    _ => return Err(corrupt()),
                };
                for (index, line) in lines.iter().enumerate().skip(1) {
                    let parsed = serde_json::from_str::<Value>(line).ok().and_then(|v| {
                        Some((
                            v["g"].as_str()?.to_owned(),
                            v["e"].as_u64()?,
                            v["f"].as_str()?.to_owned(),
                            v["n"].as_u64()?,
                            v["x"].as_u64()?,
                        ))
                    });
                    let Some((grant_id, grant_expires, fence, bytes, request_expires)) = parsed
                    else {
                        // Only a torn final line (crash mid-append) is not an admission.
                        if index + 1 == lines.len() && !complete {
                            continue;
                        }
                        return Err(corrupt());
                    };
                    if grant_expires.saturating_add(state.clock_skew_ms) < now {
                        continue; // the grant can no longer be presented
                    }
                    state.admit(fence, request_expires, &grant_id, bytes, now);
                    kept.push((*line).to_owned());
                }
                genesis
            }
        };
        state.genesis_unix_ms = genesis;
        let tmp = dir.join(LEDGER_TMP);
        let mut file = File::create(&tmp).map_err(unavailable)?;
        writeln!(
            file,
            "{}",
            json!({"protocol": LEDGER_PROTOCOL, "genesis_unix_ms": genesis})
        )
        .map_err(unavailable)?;
        for line in &kept {
            writeln!(file, "{line}").map_err(unavailable)?;
        }
        file.sync_all().map_err(unavailable)?;
        std::fs::rename(&tmp, &path).map_err(unavailable)?;
        File::open(dir)
            .and_then(|handle| handle.sync_all())
            .map_err(unavailable)?;
        let file = OpenOptions::new()
            .append(true)
            .open(&path)
            .map_err(unavailable)?;
        Ok(Self { path, file })
    }
    /// One whole line per write, then fsync.
    fn append(
        &mut self,
        grant: &Grant,
        fence: &str,
        request_expires: u64,
        bytes: u64,
    ) -> std::io::Result<()> {
        let mut line = serde_json::to_vec(&json!({
            "g": grant.grant_id, "e": grant.expires_at_unix_ms, "f": fence,
            "n": bytes, "x": request_expires,
        }))?;
        line.push(b'\n');
        self.file.write_all(&line)?;
        self.file.sync_data()
    }
}

/// Content-addressed objects (`<root>/<sha256 hex>`) served behind the durable journal.
pub struct DurableObjects {
    root: PathBuf,
    state: Mutex<SourceState>,
}

impl DurableObjects {
    /// Opens (or creates) the journal and replays it.
    pub fn open(config: DurableConfig) -> Result<Self, ArtifactError> {
        let mut state = SourceState {
            fence: BTreeMap::new(),
            fence_capacity: config.fence_capacity.max(1),
            grants: BTreeMap::new(),
            verified: BTreeMap::new(),
            journal: None,
            genesis_unix_ms: u64::MAX,
            clock_skew_ms: config.clock_skew_ms,
        };
        let journal = Journal::open(&config.state_dir, &mut state, (config.now)())?;
        state.journal = Some(journal);
        Ok(Self {
            root: config.objects,
            state: Mutex::new(state),
        })
    }
    /// Opt-in bounded verification before publishing immutable large objects.
    /// Existing callers retain their behavior. Checkpoints may enforce absolute
    /// deadlines/cancellation; interrupted scans never populate the verified cache.
    pub fn verify_bounded(
        &self,
        digest: &str,
        checkpoint: &dyn Fn() -> Result<(), ArtifactError>,
    ) -> Result<u64, ArtifactError> {
        use sha2::{Digest, Sha256};
        let hex = digest
            .strip_prefix("sha256:")
            .filter(|h| {
                h.len() == 64
                    && h.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            .ok_or_else(|| error("artifact_object_digest_invalid"))?;
        checkpoint()?;
        let mut file =
            File::open(self.root.join(hex)).map_err(|_| error("artifact_object_unavailable"))?;
        let metadata = file
            .metadata()
            .map_err(|_| error("artifact_object_unavailable"))?;
        if !metadata.is_file() {
            return Err(error("artifact_object_unavailable"));
        }
        let cap = budgets::BudgetId::PyMyceliumA13MacosSliceAcquisitionMaxSegmentBytesL63
            .bytes()
            .map_err(|_| error("budget_unavailable"))?
            .get() as usize;
        let mut buffer = vec![0; cap];
        let mut hash = Sha256::new();
        let mut size = 0_u64;
        loop {
            checkpoint()?;
            let n = file
                .read(&mut buffer)
                .map_err(|_| error("artifact_object_unavailable"))?;
            if n == 0 {
                break;
            }
            hash.update(&buffer[..n]);
            size = size
                .checked_add(n as u64)
                .ok_or_else(|| error("artifact_object_unavailable"))?;
        }
        checkpoint()?;
        if size != metadata.len() || format!("{:x}", hash.finalize()) != hex {
            return Err(error("artifact_source_chunk_unverified"));
        }
        self.lock().verified.insert(hex.into(), size);
        Ok(size)
    }
    /// Where the admission journal lives (for operators and tests).
    pub fn journal_path(&self) -> Option<PathBuf> {
        self.lock()
            .journal
            .as_ref()
            .map(|journal| journal.path.clone())
    }
    fn lock(&self) -> std::sync::MutexGuard<'_, SourceState> {
        // A panic while the lock was held leaves plain data; refusals stay correct.
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

impl ArtifactObjects for DurableObjects {
    /// Length of a held object whose content matches its name (hashed once).
    fn verified_size(&self, digest: &str) -> Option<u64> {
        let hex = digest.strip_prefix("sha256:")?;
        if let Some(length) = self.lock().verified.get(hex) {
            return Some(*length);
        }
        let bytes = std::fs::read(self.root.join(hex)).ok()?;
        if range_digest(&bytes) != digest {
            return None; // artifact_source_chunk_unverified: not served
        }
        let length = bytes.len() as u64;
        self.lock().verified.insert(hex.to_owned(), length);
        Some(length)
    }
    fn admit_and_read(
        &self,
        grant: &Grant,
        request: &ChunkRequest,
        nonce: &str,
        request_expiry: u64,
        now: u64,
    ) -> Result<AdmittedChunk, ArtifactError> {
        // A2's fence key: sha256(grant_id \0 request_id \0 nonce).
        let fence_key = range_digest(
            format!("{}\0{}\0{}", grant.grant_id, request.request_id, nonce).as_bytes(),
        )["sha256:".len()..]
            .to_owned();
        // Check, journal and charge under one lock: concurrent streams for the same grant
        // serialize here, and nothing is read or sent before the admission is durable.
        let pace = self.lock().admit_request(
            grant,
            fence_key,
            request_expiry,
            request.length_bytes,
            now,
        )?;
        let unavailable = || error("artifact_object_unavailable");
        let hex = request
            .object_digest
            .strip_prefix("sha256:")
            .ok_or_else(unavailable)?;
        let mut file = File::open(self.root.join(hex)).map_err(|_| unavailable())?;
        let length = usize::try_from(request.length_bytes).map_err(|_| unavailable())?;
        let mut bytes = vec![0_u8; length];
        file.seek(SeekFrom::Start(request.offset_bytes))
            .and_then(|_| file.read_exact(&mut bytes))
            .map_err(|_| unavailable())?;
        Ok(AdmittedChunk {
            bytes,
            pace: Span::from_millis(pace),
        })
    }
}

#[cfg(test)]
mod tests {
    //! Transcription of the oracle's two unit tests (`artifact_net.rs:641-674 @ f4e366e9`, executed
    //! green: `L9-evidence/r14-oracle-journal-unit-tests.log`), every assertion kept.
    use super::*;

    fn skew() -> u64 {
        budgets::BudgetId::PyMyceliumMobileArtifactGrantClockSkewMsL40
            .span(budgets::Entries::ZERO)
            .map(|span| span.millis())
            .unwrap_or(0)
    }
    fn grant(not_before: u64) -> Grant {
        Grant {
            grant_id: "grant-1".into(),
            recipient_node_id: "node".into(),
            recipient_endpoint_id: "a".repeat(64),
            recipient_key_digest: format!("sha256:{}", "1".repeat(64)),
            assignment_id: "route-1".into(),
            manifest_pack_digest: format!("sha256:{}", "2".repeat(64)),
            allowed_object_digests: vec![],
            sources: BTreeMap::new(),
            source_addrs: BTreeMap::new(),
            maximum_total_bytes: 100,
            maximum_bytes_per_second: 1 << 30,
            maximum_chunk_bytes: 64,
            not_before_unix_ms: not_before,
            expires_at_unix_ms: not_before + 900_000,
            envelope: Value::Null,
        }
    }
    fn dir(tag: &str) -> PathBuf {
        let nanos = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.as_nanos());
        std::env::temp_dir().join(format!(
            "mycelium-ledger-{tag}-{}-{nanos}",
            std::process::id()
        ))
    }
    fn state() -> SourceState {
        SourceState {
            fence: BTreeMap::new(),
            fence_capacity: usize::MAX,
            grants: BTreeMap::new(),
            verified: BTreeMap::new(),
            journal: None,
            genesis_unix_ms: u64::MAX,
            clock_skew_ms: skew(),
        }
    }
    fn code(result: Result<u64, ArtifactError>) -> String {
        result.expect_err("refused").0
    }

    #[test]
    fn a_failed_append_poisons_the_journal_and_refuses_everything_after() {
        let d = dir("poison");
        let mut st = state();
        st.journal = Some(Journal::open(&d, &mut st, 1_000).unwrap());
        let g = grant(1_000);
        assert!(st.admit_request(&g, "f1".into(), 60_000, 10, 2_000).is_ok());
        // The append handle can no longer write (as on a full or failing disk).
        let path = st.journal.as_ref().unwrap().path.clone();
        st.journal.as_mut().unwrap().file = File::open(&path).unwrap();
        assert_eq!(
            code(st.admit_request(&g, "f2".into(), 60_000, 10, 2_000)),
            "artifact_source_ledger_unavailable"
        );
        assert!(st.journal.is_none());
        assert_eq!(
            code(st.admit_request(&g, "f3".into(), 60_000, 10, 2_000)),
            "artifact_source_ledger_unavailable"
        );
        assert!(
            !st.fence.contains_key("f2") && st.grants["grant-1"].0 == 10,
            "a refused request is neither fenced nor charged"
        );
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn fence_entries_outlive_their_window_only_and_never_evict_live_ones() {
        let d = dir("fence");
        let mut st = state();
        st.journal = Some(Journal::open(&d, &mut st, 1_000).unwrap());
        st.fence_capacity = 1;
        let g = grant(1_000);
        st.admit_request(&g, "f1".into(), 10_000, 1, 2_000).unwrap();
        assert_eq!(
            code(st.admit_request(&g, "f2".into(), 10_000, 1, 2_000)),
            "artifact_source_busy"
        );
        // Once f1's request window (+ skew) has passed it is pruned and a new request fits; f1
        // itself can no longer be presented by then.
        st.admit_request(&g, "f2".into(), 200_000, 1, 10_000 + skew() + 1)
            .unwrap();
        assert!(!st.fence.contains_key("f1"));
        // A grant from before genesis is refused.
        assert_eq!(
            code(st.admit_request(&grant(999), "f9".into(), 200_000, 1, 2_000)),
            "artifact_source_ledger_reset"
        );
        std::fs::remove_dir_all(d).unwrap();
    }
}
