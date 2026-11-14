// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's transfer step: serve compiled stage shards to members.
//!
//! [`ShardSource`] owns one artifact-carrier endpoint (LocalOnly loopback, or
//! direct LAN on exactly one interface) on its own thread and runtime, plus the
//! durable object journal over the compiler's content-addressed objects. For
//! each member [`transfer`] (1) requests the member's own consent and refuses
//! before any offer unless it covers the bytes (`member_consent_required`),
//! (2) verifies the object and establishes journal genesis BEFORE the grant is
//! issued (issuing first trips the source's ledger-reset fence), (3) issues the
//! seed-signed offer naming this source's key (and, on LAN, its signed
//! addresses), (4) serves exactly that offer to one connection from the
//! offer's recipient carrier key while the member receives and verifies, and
//! returns the member's receipt. Only that key is admitted. Direct LAN: the key
//! is installed in the host before the job's first accept and ENFORCED by the
//! host after TLS authentication and before it hands the connection to the job
//! (other keys are closed `LAN_KEY_REFUSED`, reason `lan_key_refused`, and
//! unpinned at once); the job checks the key again, which also covers a
//! connection carried over from an earlier job. LocalOnly: only that second
//! check (reason `recipient_key_refused`). Any other or failed handshake is
//! closed unserved and the job keeps accepting until its own absolute
//! deadline. Serving ends at that deadline or at `close`, whichever comes
//! first. Bytes, recipes and offers are all produced elsewhere and verified on
//! both sides here.
//!
//! `close` is bounded in every state (LAN-R2-A1): it returns within one
//! `OwnerPoll` (the accept slice in progress, both modes; handshakes in flight
//! carry over between slices and end with the endpoint) plus one `RouterClose`
//! (the endpoint's close, then storage work still running), whether the
//! thread waits in accept, is mid-handshake, verifies an object at admission
//! or serves a stream. Storage work runs on this source's runtime's blocking
//! pool, never on the source thread: object verification at admission (which
//! also checks Stop between reads) and the artifact service's chunk reads and
//! journal appends. What cannot be interrupted (a read or fsync inside the
//! kernel) is not joined without limit: it gets what is left of that
//! `RouterClose` (`Runtime::shutdown_timeout`), then it is ABANDONED to finish
//! on its own pool thread, which drops its files when the call returns, and
//! `close` says so (`source_storage_abandoned`). That is safe for the files it
//! touches: the content-addressed objects are only read; a late journal
//! append or rewrite on a directory a later source reuses can at worst lose
//! fence lines of THIS source or make the later source refuse its ledger
//! (`artifact_source_ledger_corrupt` / `_reset`), never let it serve a replay,
//! because every source signs with a fresh key and a grant (with its requests)
//! names that key.
use super::link::{MemberLink, Refusal};
use crate::{app_host, joined_shard};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact_source::ArtifactService,
    iroh_host::{LAN_KEY_REFUSED, LanPolicy, LoopbackHost},
    sidecar_artifact::{grant_limits, serve_limits},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
        mpsc,
    },
    time::{Duration, Instant},
};
use transport::{Channel, Clock};

fn refusal(code: impl Into<String>) -> Refusal {
    Refusal(code.into())
}
type Done = mpsc::Sender<Result<(), Refusal>>;
enum Job {
    /// Verify the object and establish journal genesis (before the grant).
    Admit { hash: String, done: Done },
    /// Serve exactly `offer` to one connection.
    Serve {
        hash: String,
        offer: Value,
        done: Done,
    },
}
pub struct ShardSource {
    jobs: Option<mpsc::Sender<Job>>,
    thread: Option<std::thread::JoinHandle<Result<(), Refusal>>>,
    endpoint: iroh::EndpointId,
    addrs: Vec<SocketAddr>,
    key: Value,
    lan: bool,
    /// Set by `close`; the source thread serves nothing once it is set.
    closing: Arc<AtomicBool>,
    /// Woken by `close`: every wait of the source thread ([`closed`]) ends.
    stopping: Arc<tokio::sync::Notify>,
}
/// Resolves once `close` has begun (`stop` set, then `wake` notified): any
/// number of waiters, registered before or after it.
async fn closed(stop: &AtomicBool, wake: &tokio::sync::Notify) {
    loop {
        let notified = wake.notified();
        tokio::pin!(notified);
        notified.as_mut().enable();
        if stop.load(Ordering::SeqCst) {
            return;
        }
        notified.await;
    }
}
fn budget(id: B) -> Result<budgets::Span, Refusal> {
    id.span(Entries::ZERO)
        .map_err(|_| refusal("budget_unavailable"))
}
impl ShardSource {
    /// Bind the source endpoint and open its job thread. `objects` holds the
    /// compiler's content-addressed bytes (`<hex digest>` file names).
    pub fn start(
        authority: Arc<seed::Coordinator>,
        objects: PathBuf,
        journal: PathBuf,
        lan: Option<(IpAddr, LanPolicy)>,
    ) -> Result<Self, Refusal> {
        Self::start_with_objects(authority, objects, journal, lan, None)
    }
    /// Packaged compiler handoff: carry admission-time source custody into every read.
    pub fn start_with_objects(
        authority: Arc<seed::Coordinator>,
        objects: PathBuf,
        journal: PathBuf,
        lan: Option<(IpAddr, LanPolicy)>,
        pinned: Option<Arc<runtime::artifact_journal::PinnedObjects>>,
    ) -> Result<Self, Refusal> {
        Self::start_with_wall(
            authority,
            objects,
            journal,
            lan,
            pinned,
            budget(B::MemberChosenWall)?,
        )
    }
    /// [`Self::start_with_objects`] with each job's absolute wall (the app: `MemberChosenWall`)
    /// given: the deadline its admission, accept and serving end at. A test shortens it to reach
    /// that deadline; no product path passes anything else.
    pub fn start_with_wall(
        authority: Arc<seed::Coordinator>,
        objects: PathBuf,
        journal: PathBuf,
        lan: Option<(IpAddr, LanPolicy)>,
        pinned: Option<Arc<runtime::artifact_journal::PinnedObjects>>,
        wall: budgets::Span,
    ) -> Result<Self, Refusal> {
        let close = budget(B::RouterClose)?;
        let fence = B::ArtifactMaxFence
            .count()
            .map_err(|_| refusal("budget_unavailable"))?
            .get() as usize;
        let skew = grant_limits()
            .map_err(|_| refusal("budget_unavailable"))?
            .clock_skew_ms;
        let on_lan = lan.is_some();
        let poll = budget(B::OwnerPoll)?;
        let closing = Arc::new(AtomicBool::new(false));
        let stopping = Arc::new(tokio::sync::Notify::new());
        let (stop, stopped) = (closing.clone(), stopping.clone());
        let (ready_tx, ready_rx) = mpsc::channel();
        let (jobs, inbox) = mpsc::channel::<Job>();
        let thread = std::thread::Builder::new()
            .name("joined-owner-source".into())
            .spawn(move || -> Result<(), Refusal> {
                // One worker that keeps driving iroh while this thread waits, so a refusal or
                // close queued before a return is always flushed (REVIEW-laneB20f; as remote.rs).
                let rt = tokio::runtime::Builder::new_multi_thread()
                    .worker_threads(1)
                    .enable_all()
                    .build()
                    .map_err(|_| refusal("runtime_failed"))?;
                let clock = crate::managed_carrier::HostClock::default();
                let until = |span: budgets::Span| {
                    clock
                        .monotonic()
                        .checked_add(span)
                        .map_err(|_| refusal("deadline_invalid"))
                };
                let bound = match lan {
                    Some((ip, policy)) => LoopbackHost::bind_lan(
                        &rt,
                        &clock,
                        until(wall)?,
                        iroh::SecretKey::generate(),
                        &[Channel::ArtifactChunk],
                        ip,
                        policy,
                    ),
                    None => LoopbackHost::bind_protocols(
                        &rt,
                        &clock,
                        until(wall)?,
                        iroh::SecretKey::generate(),
                        &[Channel::ArtifactChunk],
                    ),
                };
                let mut host = bound.or_else(|e| {
                    let code = format!("source_bind_failed:{e:?}");
                    if ready_tx.send(Err(refusal(code.clone()))).is_err() {
                        eprintln!("joined owner source: starter gone");
                    }
                    Err(refusal(code))
                })?;
                let addr = host
                    .address()
                    .map_err(|e| refusal(format!("source_address_failed:{e:?}")))?;
                let secret: [u8; 32] = app_host::random(32)?
                    .try_into()
                    .map_err(|_| refusal("source_identity_failed"))?;
                let signer = Identity::from_secret(&addr.id.to_string(), &secret)
                    .map_err(|_| refusal("source_identity_failed"))?;
                let sent = ready_tx.send(Ok((
                    addr.id,
                    addr.ip_addrs().copied().collect::<Vec<_>>(),
                    signer.public_key_record(),
                )));
                let mut admitted: BTreeMap<String, DurableObjects> = BTreeMap::new();
                // Storage work this thread put on the blocking pool: every admission holds a
                // clone of `custody`, every chunk read of the artifact service a strong
                // reference to its job's objects (`served`). What still holds one after the
                // runtime's shutdown below was abandoned (module note).
                let custody = Arc::new(());
                let mut served = Vec::new();
                // A starter that went away just closes the endpoint below.
                while sent.is_ok() {
                    let Ok(job) = inbox.recv() else { break };
                    if stop.load(Ordering::SeqCst) {
                        let (Job::Admit { done, .. } | Job::Serve { done, .. }) = job;
                        if done.send(Err(refusal("source_closed"))).is_err() {
                            eprintln!("joined owner source: closed job waiter gone");
                        }
                        continue;
                    }
                    match job {
                        Job::Admit { hash, done } => {
                            let result = (|| {
                                let config = DurableConfig {
                                    objects: objects.clone(),
                                    state_dir: journal.clone(),
                                    fence_capacity: fence,
                                    clock_skew_ms: skew,
                                    now: Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
                                };
                                let (pinned, halt, held) =
                                    (pinned.clone(), stop.clone(), custody.clone());
                                let wall = Duration::from_millis(wall.millis());
                                let end = Instant::now() + wall;
                                // On the blocking pool: the source thread keeps observing
                                // Stop while a read is held (LAN-R2-A1).
                                let verifying = rt.spawn_blocking(move || {
                                    let custody = held;
                                    let store = DurableObjects::open(config)
                                        .map_err(|e| {
                                            refusal(format!("source_store_failed:{}", e.0))
                                        })?
                                        .with_pinned(pinned);
                                    store
                                        .verify_bounded(&hash, &|| {
                                            if halt.load(Ordering::SeqCst) {
                                                Err(runtime::iroh_artifact::ArtifactError(
                                                    "source_closed".into(),
                                                ))
                                            } else if Instant::now() < end {
                                                Ok(())
                                            } else {
                                                Err(runtime::iroh_artifact::ArtifactError(
                                                    "source_verify_deadline".into(),
                                                ))
                                            }
                                        })
                                        .map_err(|e| {
                                            refusal(format!("source_verify_failed:{}", e.0))
                                        })?;
                                    drop(custody);
                                    Ok::<_, Refusal>((hash, store))
                                });
                                let (hash, store) = rt.block_on(async {
                                    tokio::select! {
                                        joined = verifying => joined.map_err(|_| {
                                            refusal("source_verify_failed:source_verify_panicked")
                                        })?,
                                        () = closed(&stop, &stopped) => Err(refusal("source_closed")),
                                        () = tokio::time::sleep(wall) => {
                                            Err(refusal("source_verify_failed:source_verify_deadline"))
                                        }
                                    }
                                })?;
                                admitted.insert(hash, store);
                                Ok(())
                            })();
                            if done.send(result).is_err() {
                                eprintln!("joined owner source: admit waiter gone");
                            }
                        }
                        Job::Serve { hash, offer, done } => {
                            let result = (|| {
                                let store = admitted
                                    .remove(&hash)
                                    .ok_or_else(|| refusal("source_object_not_admitted"))?;
                                // The one key this job serves: the recipient carrier the
                                // seed-signed offer names (JoinedSource::new checks the offer).
                                let recipient: iroh::EndpointId = offer["message"]["grant"]
                                    ["message"]["recipient_endpoint_id"]
                                    .as_str()
                                    .and_then(|key| key.parse().ok())
                                    .ok_or_else(|| refusal("joined_shard_invalid"))?;
                                let wrapped = Arc::new(joined_shard::JoinedSource::new(
                                    authority.clone(),
                                    offer,
                                    store,
                                )?);
                                served.push(Arc::downgrade(&wrapped));
                                let service = ArtifactService::new(
                                    Identity::from_secret(
                                        signer.endpoint_id(),
                                        &signer.secret_bytes(),
                                    )
                                    .map_err(|_| refusal("source_identity_failed"))?,
                                    authority.signer().public_key_record(),
                                    grant_limits().map_err(|_| refusal("budget_unavailable"))?,
                                    wrapped,
                                    serve_limits().map_err(|_| refusal("budget_unavailable"))?,
                                    Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
                                )
                                .map_err(|e| refusal(format!("source_service_failed:{e:?}")))?;
                                let end = until(wall)?;
                                // LAN-A1: installed before accept; the host enforces it after
                                // TLS authentication, before handing a connection to this job.
                                if on_lan {
                                    host.admit_peer_ids(&[recipient]).map_err(|e| {
                                        refusal(format!("source_admission_failed:{e:?}"))
                                    })?;
                                }
                                // `close` is seen within one slice of OwnerPoll, in both
                                // modes: in-flight handshakes carry over to the next accept
                                // and end with the endpoint (LAN-R2-A1).
                                let accepted = loop {
                                    if stop.load(Ordering::SeqCst) {
                                        break Err(refusal("source_closed"));
                                    }
                                    let slice = until(poll)?
                                        .earliest(end)
                                        .map_err(|_| refusal("deadline_invalid"))?;
                                    match host.accept(&clock, slice) {
                                        Ok(connection) if connection.remote_id() == recipient => {
                                            break Ok(connection);
                                        }
                                        // LocalOnly admits any key; on the LAN, a connection
                                        // carried over from an earlier job's key: closed
                                        // unserved (this check's own reason), the job goes on.
                                        Ok(connection) => {
                                            connection.close(
                                                LAN_KEY_REFUSED.into(),
                                                b"recipient_key_refused",
                                            );
                                        }
                                        // A slice ended, or a refused or failed handshake:
                                        // retried until the job's own absolute deadline.
                                        Err(
                                            transport::Error::Expired
                                            | transport::Error::Authentication,
                                        ) if end.remaining(clock.monotonic()).is_ok() => continue,
                                        Err(e) => {
                                            break Err(refusal(format!(
                                                "source_accept_failed:{e:?}"
                                            )));
                                        }
                                    }
                                };
                                if stop.load(Ordering::SeqCst) {
                                    return Err(refusal("source_closed"));
                                }
                                let connection = accepted?;
                                // Serving ends at the job's deadline or at `close`: a peer
                                // that keeps the connection busy cannot hold either.
                                let left = end
                                    .remaining(clock.monotonic())
                                    .map(|span| Duration::from_millis(span.millis()))
                                    .unwrap_or(Duration::ZERO);
                                let served = connection.clone();
                                rt.block_on(async {
                                    tokio::select! {
                                        outcome = service.serve_connection(connection) => outcome
                                            .map_err(|e| refusal(format!("source_serve_failed:{e:?}"))),
                                        () = closed(&stop, &stopped) => {
                                            served.close(0u32.into(), b"source_closed");
                                            Err(refusal("source_closed"))
                                        }
                                        () = tokio::time::sleep(left) => {
                                            served.close(0u32.into(), b"source_deadline");
                                            Err(refusal("source_serve_deadline"))
                                        }
                                    }
                                })
                            })();
                            if done.send(result).is_err() {
                                eprintln!("joined owner source: serve waiter gone");
                            }
                        }
                    }
                }
                // One RouterClose for the endpoint's close and the storage work still
                // running; whatever outlives it is abandoned, never joined (module note).
                let ends = until(close)?;
                let shut = host.close(&clock, ends);
                drop(host);
                let left = ends
                    .remaining(clock.monotonic())
                    .map(|span| Duration::from_millis(span.millis()))
                    .unwrap_or(Duration::ZERO);
                rt.shutdown_timeout(left);
                let abandoned = Arc::strong_count(&custody) > 1
                    || served.iter().any(|objects| objects.strong_count() > 0);
                shut.map_err(|e| refusal(format!("source_close_failed:{e:?}")))?;
                if abandoned {
                    eprintln!(
                        "joined owner source: storage work still running was left to finish \
                         in the background (source_storage_abandoned)"
                    );
                    return Err(refusal("source_storage_abandoned"));
                }
                Ok(())
            })
            .map_err(|_| refusal("source_thread_failed"))?;
        let ready = ready_rx
            .recv_timeout(Duration::from_millis(wall.millis()))
            .map_err(|_| refusal("source_start_timeout"));
        let mut source = Self {
            jobs: Some(jobs),
            thread: Some(thread),
            endpoint: iroh::SecretKey::generate().public(),
            addrs: Vec::new(),
            key: Value::Null,
            lan: on_lan,
            closing,
            stopping,
        };
        let (endpoint, addrs, key) = match ready.and_then(|r| r) {
            Ok(ready) => ready,
            Err(e) => {
                if let Err(close) = source.shutdown() {
                    eprintln!("joined owner source start cleanup: {close:?}");
                }
                return Err(e);
            }
        };
        source.endpoint = endpoint;
        source.addrs = addrs;
        source.key = key;
        Ok(source)
    }
    pub fn endpoint_id(&self) -> iroh::EndpointId {
        self.endpoint
    }
    /// Where the source listens (direct LAN: signed into each grant).
    pub fn addrs(&self) -> &[SocketAddr] {
        &self.addrs
    }
    /// The source's public key record (no source secret reaches the seed).
    pub fn key(&self) -> &Value {
        &self.key
    }
    fn job(&self, make: impl FnOnce(Done) -> Job, within: Duration) -> Result<(), Refusal> {
        let (done, wait) = mpsc::channel();
        self.jobs
            .as_ref()
            .ok_or_else(|| refusal("source_closed"))?
            .send(make(done))
            .map_err(|_| refusal("source_closed"))?;
        wait.recv_timeout(within)
            .map_err(|_| refusal("source_job_timeout"))?
    }
    fn shutdown(&mut self) -> Result<(), Refusal> {
        self.closing.store(true, Ordering::SeqCst);
        // Every wait of the source thread ends now (an admission, a connection being
        // served); an accept slice within OwnerPoll.
        self.stopping.notify_waiters();
        self.jobs.take();
        match self.thread.take() {
            Some(t) => t.join().map_err(|_| refusal("source_thread_panicked"))?,
            None => Ok(()),
        }
    }
    /// Close the endpoint and join the thread, within the bound of the module
    /// note in every state; `source_storage_abandoned` when storage work that
    /// could not be interrupted was left to finish in the background.
    pub fn close(mut self) -> Result<(), Refusal> {
        self.shutdown()
    }
}
impl Drop for ShardSource {
    fn drop(&mut self) {
        if let Err(e) = self.shutdown() {
            eprintln!("joined owner source close: {e:?}");
        }
    }
}
/// The member's verified receipt and the offer it received.
pub struct Transferred {
    pub offer: Value,
    pub received: Value,
}
/// One stage's transfer (see the module doc). `file` and `recipe` are the
/// compiler's `node_files[node][0]` and `node_slice_plans[node]` rows.
#[allow(clippy::too_many_arguments)]
pub fn transfer(
    source: &ShardSource,
    coordinator: &seed::Coordinator,
    member: &mut dyn MemberLink,
    registration: &Value,
    run: &str,
    file: &Value,
    recipe: &Value,
    within: Duration,
) -> Result<Transferred, Refusal> {
    let bytes = file["size_bytes"]
        .as_u64()
        .ok_or_else(|| refusal("joined_shard_invalid"))?;
    let status = member.call(json!({"command":"consent_status"}), within)?;
    super::consent::require(coordinator, registration, &status, bytes)?;
    let hash = file["content_digest"]
        .as_str()
        .ok_or_else(|| refusal("joined_shard_invalid"))?
        .to_owned();
    source.job(
        |done| Job::Admit {
            hash: hash.clone(),
            done,
        },
        within,
    )?;
    let signed = if source.lan {
        source.addrs.clone()
    } else {
        Vec::new()
    };
    let offer = joined_shard::issue_with_source_addrs(
        coordinator,
        registration,
        run,
        file,
        recipe,
        &source.endpoint.to_string(),
        &source.key,
        &signed,
    )?;
    let (done, served) = mpsc::channel();
    source
        .jobs
        .as_ref()
        .ok_or_else(|| refusal("source_closed"))?
        .send(Job::Serve {
            hash,
            offer: offer.clone(),
            done,
        })
        .map_err(|_| refusal("source_closed"))?;
    let received = member.call(
        json!({"command":"receive","offer":offer,"source_id":source.endpoint.to_string(),
            "source_socket":source.addrs.first()}),
        within,
    )?;
    served
        .recv_timeout(within)
        .map_err(|_| refusal("source_job_timeout"))??;
    Ok(Transferred { offer, received })
}
