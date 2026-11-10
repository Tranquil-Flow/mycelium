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
//! addresses), (4) serves exactly that offer to one connection while the member
//! receives and verifies, and returns the member's receipt. Bytes, recipes and
//! offers are all produced elsewhere and verified on both sides here.
use super::link::{MemberLink, Refusal};
use crate::{app_host, joined_shard};
use budgets::{BudgetId as B, Entries};
use protocol::identity::Identity;
use runtime::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact_source::ArtifactService,
    iroh_host::{LanPolicy, LoopbackHost},
    sidecar_artifact::{grant_limits, serve_limits},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    net::{IpAddr, SocketAddr},
    path::PathBuf,
    sync::{Arc, mpsc},
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
        let wall = budget(B::MemberChosenWall)?;
        let close = budget(B::RouterClose)?;
        let fence = B::ArtifactMaxFence
            .count()
            .map_err(|_| refusal("budget_unavailable"))?
            .get() as usize;
        let skew = grant_limits()
            .map_err(|_| refusal("budget_unavailable"))?
            .clock_skew_ms;
        let on_lan = lan.is_some();
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
                let mut host = match bound {
                    Ok(host) => host,
                    Err(e) => {
                        let code = format!("source_bind_failed:{e:?}");
                        if ready_tx.send(Err(refusal(code.clone()))).is_err() {
                            eprintln!("joined owner source: starter gone");
                        }
                        return Err(refusal(code));
                    }
                };
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
                // A starter that went away just closes the endpoint below.
                while sent.is_ok() {
                    let Ok(job) = inbox.recv() else { break };
                    match job {
                        Job::Admit { hash, done } => {
                            let result = (|| {
                                let store = DurableObjects::open(DurableConfig {
                                    objects: objects.clone(),
                                    state_dir: journal.clone(),
                                    fence_capacity: fence,
                                    clock_skew_ms: skew,
                                    now: Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
                                })
                                .map_err(|e| refusal(format!("source_store_failed:{}", e.0)))?;
                                let end = Instant::now() + Duration::from_millis(wall.millis());
                                store
                                    .verify_bounded(&hash, &|| {
                                        if Instant::now() < end {
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
                                let wrapped = joined_shard::JoinedSource::new(
                                    authority.clone(),
                                    offer,
                                    store,
                                )?;
                                let service = ArtifactService::new(
                                    Identity::from_secret(
                                        signer.endpoint_id(),
                                        &signer.secret_bytes(),
                                    )
                                    .map_err(|_| refusal("source_identity_failed"))?,
                                    authority.signer().public_key_record(),
                                    grant_limits().map_err(|_| refusal("budget_unavailable"))?,
                                    Arc::new(wrapped),
                                    serve_limits().map_err(|_| refusal("budget_unavailable"))?,
                                    Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
                                )
                                .map_err(|e| refusal(format!("source_service_failed:{e:?}")))?;
                                let connection = host
                                    .accept(&clock, until(wall)?)
                                    .map_err(|e| refusal(format!("source_accept_failed:{e:?}")))?;
                                rt.block_on(service.serve_connection(connection))
                                    .map_err(|e| refusal(format!("source_serve_failed:{e:?}")))
                            })();
                            if done.send(result).is_err() {
                                eprintln!("joined owner source: serve waiter gone");
                            }
                        }
                    }
                }
                host.close(&clock, until(close)?)
                    .map_err(|e| refusal(format!("source_close_failed:{e:?}")))?;
                // `host` drops before `rt` (reverse declaration order).
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
        self.jobs.take();
        match self.thread.take() {
            Some(t) => t.join().map_err(|_| refusal("source_thread_panicked"))?,
            None => Ok(()),
        }
    }
    /// Close the endpoint and join the thread.
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
