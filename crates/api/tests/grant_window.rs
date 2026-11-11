// SPDX-License-Identifier: AGPL-3.0-only
//! Lane GR: a joined shard transfer outlives the member lease that was current when its grant
//! was issued, and still completes while the member keeps renewing (v0.1.4 sizes artifact grants,
//! `mycelium_seed/route_assignment.py:826-842`, `mycelium_live/member_transport.py:1016-1022`, and
//! authorizes every chunk against the CURRENT live membership, `peer_model_source.py:256-262`).
//! Real seed (TLS, coordinator on an injected clock), real pairing and join, real iroh loopback
//! carrier, `ArtifactService` + `JoinedSource` + durable journal on the same injected clock. No
//! sleeps: time moves only when the test advances it.
use api::{
    app_host, handoff, joined_shard,
    joined_shard::JoinedShard,
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
    seed::SeedOptions,
};
use budgets::{BudgetId as B, Entries};
use protocol::{artifact_grant::ChunkRequest, identity::Identity};
use runtime::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact::ChunkSession,
    iroh_artifact_source::ArtifactService,
    iroh_host::LoopbackHost,
    sidecar_artifact::{grant_limits, serve_limits},
};
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::Duration,
};
use transport::{Channel, Clock};

static NEXT: AtomicU64 = AtomicU64::new(0);
const SWARM: &str = "gr-grant-window";

/// The one clock every party reads (milliseconds since the epoch).
#[derive(Clone)]
struct Time(Arc<AtomicU64>);
impl Time {
    fn start() -> Self {
        Self(Arc::new(AtomicU64::new(
            (app_host::now() * 1000.0).floor() as u64
        )))
    }
    fn now(&self) -> f64 {
        self.0.load(Ordering::SeqCst) as f64 / 1000.0
    }
    fn ms(&self) -> u64 {
        self.0.load(Ordering::SeqCst)
    }
    fn advance(&self, seconds: u64) {
        self.0.fetch_add(seconds * 1000, Ordering::SeqCst);
    }
}
fn seconds(id: B) -> u64 {
    id.span(Entries::ZERO).unwrap().millis() / 1000
}
fn allowance() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis())
}
fn deadline(clock: &api::managed_carrier::HostClock) -> budgets::MonoInstant {
    clock
        .monotonic()
        .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
        .unwrap()
}

/// Owner seed + one joined, consenting member, all on `time`.
struct Rig {
    root: PathBuf,
    member_root: PathBuf,
    time: Time,
    coordinator: Arc<seed::Coordinator>,
    server: Option<seed::server::Server>,
    options: SeedOptions,
    signer: Identity,
    session: Session,
}
impl Rig {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "gr-window-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        for dir in [&owner, &member_root] {
            fs::create_dir(dir).unwrap();
            fs::set_permissions(dir, std::os::unix::fs::PermissionsExt::from_mode(0o700)).unwrap();
        }
        let time = Time::start();
        let material = seed::tls::ensure_local_tls_material(&owner.join("tls")).unwrap();
        let seed_key = [31u8; 32];
        let seed_signer = Identity::from_secret("endpoint-gr-seed", &seed_key).unwrap();
        let options = seed::Options::new(SWARM, "seed-gr", "boot-gr").unwrap();
        let ids = AtomicU64::new(1);
        let clock = time.clone();
        let coordinator = Arc::new(
            seed::Coordinator::open(
                &owner.join("seed.sqlite"),
                seed_signer,
                options,
                Box::new(move || clock.now()),
                Box::new(move || {
                    format!(
                        "{:08x}-0000-4000-8000-{:012x}",
                        ids.fetch_add(1, Ordering::SeqCst),
                        31
                    )
                }),
            )
            .unwrap(),
        );
        let server = seed::server::Server::bind_tls(coordinator.clone(), &material, None).unwrap();
        let options = SeedOptions {
            ca_pem: Some(fs::read(&material.ca_certificate).unwrap()),
            connect_to: Some(server.address()),
            server_name: Some("127.0.0.1".into()),
        };
        let handoff_coordinator = handoff::Coordinator {
            signer: Identity::from_secret("endpoint-gr-seed", &seed_key).unwrap(),
            swarm_id: SWARM.into(),
            seed_url: server.base_url().to_owned(),
        };
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let ttl = seconds(B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37);
        let quota = json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false});
        let handoff = handoff::approve_pairing(
            &owner,
            true,
            Some(&handoff_coordinator),
            &request.to_value(),
            &json!(ttl),
            &quota,
            time.now(),
            &handoff::Entropy::os,
        )
        .unwrap();
        let accepted = with_env(&time, &signer, &options, |env| {
            peer::accept_handoff(&member_root, true, &handoff.to_value(), env).unwrap()
        });
        let mut rig = Self {
            root,
            member_root,
            time,
            coordinator,
            server: Some(server),
            options,
            signer,
            session: accepted.session.unwrap(),
        };
        rig.renew().unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&rig.member_root, &policy).unwrap();
        standing_consent::grant(&rig.member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = rig.session.binding().unwrap();
        standing_consent::bind(&rig.member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        rig
    }
    /// One pass of the member's own renewal (`peer.renew`) through the real seed.
    fn renew(&mut self) -> Result<(), String> {
        let (time, signer, options) = (self.time.clone(), &self.signer, &self.options);
        with_env(&time, signer, options, |env| {
            peer::renew(&self.member_root, true, Some(&mut self.session), env).map(drop)
        })
        .map_err(|e| e.code.to_owned())
    }
    /// The member resumes: the seed advances its generation.
    fn resume(&mut self) {
        let (time, signer, options) = (self.time.clone(), &self.signer, &self.options);
        self.session = with_env(&time, signer, options, |env| {
            peer::start(&self.member_root, signer, true, env)
                .unwrap()
                .session
                .unwrap()
        });
        self.renew().unwrap();
    }
    fn lease(&self) -> f64 {
        self.session.lease_expires_at().unwrap()
    }
    fn debt(&self) -> usize {
        let ledger: Value = serde_json::from_slice(
            &fs::read(
                self.member_root
                    .join("chain-runs/compiler-storage.private.json"),
            )
            .unwrap(),
        )
        .unwrap();
        ledger["entries"].as_object().unwrap().len()
    }
}
fn with_env<R>(
    time: &Time,
    signer: &Identity,
    options: &SeedOptions,
    f: impl FnOnce(&peer::JoinEnv) -> R,
) -> R {
    let random = |n| app_host::hex(&app_host::random(n).unwrap());
    let clock = || time.now();
    let env = peer::JoinEnv {
        signer,
        clock: &clock,
        random_hex: &random,
        seed: options,
    };
    f(&env)
}
impl Drop for Rig {
    fn drop(&mut self) {
        if let Some(server) = self.server.take() {
            drop(server);
        }
        if !std::thread::panicking() {
            fs::remove_dir_all(&self.root).unwrap();
        }
    }
}

fn material(node: &str) -> (Value, Value, Vec<u8>) {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let mut recipe = case["node_plans"]["node-0"].clone();
    recipe["node_id"] = json!(node);
    recipe.as_object_mut().unwrap().remove("node_plan_digest");
    recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
    let transfer = case["node_files"]["node-0"][0].clone();
    let name = transfer["path"]
        .as_str()
        .unwrap()
        .strip_prefix("deployment/")
        .unwrap();
    let encoded = case["shard_files"][name].as_str().unwrap();
    let bytes = (0..encoded.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
        .collect();
    (recipe, transfer, bytes)
}

/// What happens to the transfer between its chunks.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum Between {
    /// The member keeps renewing its lease through the seed.
    Renew,
    /// The owner (seed) is gone: the member's renewals fail.
    OwnerGone,
    /// The member resumed: its generation changed at the seed.
    GenerationChanged,
}
struct Outcome {
    /// Each chunk's fetch result, in order (a refusal ends the transfer).
    chunks: Vec<Result<(), String>>,
    verified: Option<Result<(), String>>,
    received: Option<Vec<u8>>,
    debt_after: usize,
    expected: Vec<u8>,
    lease_at_issue: f64,
    last_fetch_at: f64,
}

/// One transfer of the fixture shard in three chunks; `step` seconds pass before chunks 2 and 3.
fn transfer(rig: &mut Rig, step: u64, between: Between) -> Outcome {
    let node = rig.session.node_id().to_owned();
    let (recipe, transfer, bytes) = material(&node);
    let size = bytes.len() as u64;
    let objects = rig.root.join("objects");
    fs::create_dir_all(&objects).unwrap();
    fs::write(
        objects.join(
            transfer["content_digest"]
                .as_str()
                .unwrap()
                .strip_prefix("sha256:")
                .unwrap(),
        ),
        &bytes,
    )
    .unwrap();
    // The source: its own endpoint, journal and service, all on the injected clock.
    let (ready_tx, ready_rx) = mpsc::channel();
    let (offer_tx, offer_rx) = mpsc::channel::<Value>();
    let (done_tx, done_rx) = mpsc::channel();
    let coordinator = rig.coordinator.clone();
    let journal = rig.root.join("journal");
    let source_time = rig.time.clone();
    let source = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let mut host = LoopbackHost::bind_protocols(
            &rt,
            &clock,
            deadline(&clock),
            iroh::SecretKey::generate(),
            &[Channel::ArtifactChunk],
        )
        .unwrap();
        let address = host.address().unwrap();
        let signer = Identity::from_secret(&address.id.to_string(), &[32u8; 32]).unwrap();
        let journal_time = source_time.clone();
        let store = DurableObjects::open(DurableConfig {
            objects,
            state_dir: journal,
            fence_capacity: B::ArtifactMaxFence.count().unwrap().get() as usize,
            clock_skew_ms: grant_limits().unwrap().clock_skew_ms,
            now: Arc::new(move || journal_time.ms()),
        })
        .unwrap();
        ready_tx
            .send((address, signer.public_key_record()))
            .unwrap();
        let offer = offer_rx.recv().unwrap();
        let wrapped = joined_shard::JoinedSource::new(coordinator.clone(), offer, store).unwrap();
        let service = ArtifactService::new(
            signer,
            coordinator.signer().public_key_record(),
            grant_limits().unwrap(),
            Arc::new(wrapped),
            serve_limits().unwrap(),
            Arc::new(move || source_time.ms()),
        )
        .unwrap();
        let connection = host.accept(&clock, deadline(&clock)).unwrap();
        // A refused chunk may end the connection: the outcome is the member's to judge.
        let served = rt.block_on(service.serve_connection(connection));
        done_tx.send(served.map_err(|e| e.0)).unwrap();
        host.close(&clock, deadline(&clock)).unwrap();
        drop(host);
        drop(rt);
    });
    let (address, source_key) = ready_rx.recv_timeout(allowance()).unwrap();
    // The member side: its carrier endpoint, signed into the registration.
    let rt = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(1)
        .enable_all()
        .build()
        .unwrap();
    let clock = api::managed_carrier::HostClock::default();
    let mut member_host = LoopbackHost::bind(&rt, &clock, deadline(&clock)).unwrap();
    let member_addr = member_host.address().unwrap();
    let carrier =
        Identity::from_secret(&member_addr.id.to_string(), &rig.signer.secret_bytes()).unwrap();
    let registration =
        joined_shard::register(&rig.session, &rig.signer, &carrier, rig.time.now()).unwrap();
    let lease_at_issue = rig.lease();
    let offer = joined_shard::issue_with_source(
        &rig.coordinator,
        &registration,
        "gr-transfer",
        &transfer,
        &recipe,
        &address.id.to_string(),
        &source_key,
    )
    .unwrap();
    offer_tx.send(offer.clone()).unwrap();
    let mut shard = JoinedShard::begin(
        &rig.member_root,
        &rig.session,
        &rig.signer,
        &carrier,
        &offer,
        rig.time.now(),
    )
    .unwrap();
    let connection = member_host
        .connect_channel(
            &address
                .ip_addrs()
                .fold(iroh::EndpointAddr::new(address.id), |a, s| {
                    a.with_ip_addr(*s)
                }),
            Channel::ArtifactChunk,
            &clock,
            deadline(&clock),
        )
        .unwrap();
    let mut link = ChunkSession::new(
        connection,
        &rt,
        &clock,
        member_addr.id,
        serve_limits().unwrap().wire,
    )
    .unwrap();
    let part = size / 3;
    let ranges = [(0, part), (part, part), (2 * part, size - 2 * part)];
    let mut chunks = Vec::new();
    let mut last_fetch_at = rig.time.now();
    for (i, (offset, length)) in ranges.into_iter().enumerate() {
        if i > 0 {
            rig.time.advance(step);
            match between {
                Between::Renew => rig.renew().unwrap(),
                Between::OwnerGone => {
                    if let Some(server) = rig.server.take() {
                        drop(server);
                    }
                    assert!(rig.renew().is_err(), "no seed: the renewal must fail");
                }
                Between::GenerationChanged => {
                    if i == 1 {
                        rig.resume();
                    } else {
                        rig.renew().unwrap();
                    }
                }
            }
        }
        last_fetch_at = rig.time.now();
        let request = ChunkRequest {
            request_id: format!("chunk-{offset}"),
            source_endpoint_id: address.id.to_string(),
            object_digest: transfer["content_digest"].as_str().unwrap().into(),
            offset_bytes: offset,
            length_bytes: length,
        };
        let now = || rig.time.now();
        let fetched = shard
            .fetch_chunk_checked(
                &mut link,
                &request,
                &rig.session,
                &rig.signer,
                &carrier,
                &now,
                deadline(&clock),
                &|| Ok(()),
            )
            .map_err(|e| e.0);
        let failed = fetched.is_err();
        chunks.push(fetched);
        if failed {
            break;
        }
    }
    let complete = chunks.len() == ranges.len() && chunks.iter().all(Result::is_ok);
    link.finish();
    let (verified, received) = if complete {
        let now = || rig.time.now();
        let verified = shard
            .verify(&rig.session, &rig.signer, &carrier, &now)
            .map_err(|e| e.0.to_owned());
        let received = verified.is_ok().then(|| fs::read(shard.path()).unwrap());
        (Some(verified), received)
    } else {
        (None, None)
    };
    let debt_after = rig.debt();
    drop(shard);
    // The source ends when the member's connection goes.
    member_host.close(&clock, deadline(&clock)).unwrap();
    drop(member_host);
    drop(rt);
    let served = done_rx.recv_timeout(allowance()).unwrap();
    if complete {
        served.unwrap();
    }
    source.join().unwrap();
    Outcome {
        chunks,
        verified,
        received,
        debt_after,
        expected: bytes,
        lease_at_issue,
        last_fetch_at,
    }
}

fn lease_seconds() -> u64 {
    seconds(B::PyFinalMyceliumSeedCoordinatorLeaseSecondsL180C31)
}

/// RED (GR a): the member keeps renewing; the transfer runs past the lease that was current when
/// the grant was issued and must still complete, byte-exact.
#[test]
fn a_transfer_outliving_the_lease_at_issue_completes_while_the_member_renews() {
    let mut rig = Rig::new();
    // Two steps of two thirds of a lease each: the last chunk is fetched after that first lease ran out.
    let step = lease_seconds() * 2 / 3;
    let out = transfer(&mut rig, step, Between::Renew);
    assert!(
        out.last_fetch_at > out.lease_at_issue,
        "the transfer must outlive the lease at issue ({} <= {})",
        out.last_fetch_at,
        out.lease_at_issue
    );
    assert_eq!(
        out.chunks,
        vec![Ok(()), Ok(()), Ok(())],
        "every chunk must be served while the member stays current"
    );
    assert_eq!(out.verified, Some(Ok(())));
    assert_eq!(out.received.as_deref(), Some(out.expected.as_slice()));
}

/// Negative: the owner (seed) is gone, so the member cannot renew; once its lease has run out the
/// next chunk is refused with the existing typed code and the byte debt stays owned.
#[test]
fn owner_gone_so_no_renewal_refuses_after_the_lease_and_keeps_the_debt() {
    let mut rig = Rig::new();
    let step = lease_seconds() + 1;
    let out = transfer(&mut rig, step, Between::OwnerGone);
    assert_eq!(
        out.chunks.len(),
        2,
        "refused at the first chunk after expiry"
    );
    assert_eq!(out.chunks[0], Ok(()));
    assert_eq!(out.chunks[1], Err("joined_shard_lease_expired".to_owned()));
    assert_eq!(out.debt_after, 1, "a failed transfer keeps its byte debt");
}

/// Negative: the member's generation changed (it resumed); the held offer no longer names the
/// current member and no further byte is accepted. Debt kept.
#[test]
fn a_changed_generation_refuses_the_held_offer_and_keeps_the_debt() {
    let mut rig = Rig::new();
    let out = transfer(&mut rig, 1, Between::GenerationChanged);
    assert_eq!(out.chunks.len(), 2);
    assert_eq!(out.chunks[0], Ok(()));
    assert_eq!(out.chunks[1], Err("joined_shard_member_changed".to_owned()));
    assert_eq!(out.debt_after, 1);
}

/// Negative: the grant's own sized window is outlived while the member keeps renewing: refused
/// with the existing typed code before any byte, debt kept. The window is v0.1.4's
/// `DEFAULT_GRANT_TTL_MS` (15 min, `mycelium_mobile/artifact_grant.py:37`).
#[test]
fn outliving_the_sized_grant_window_refuses_and_keeps_the_debt() {
    let mut rig = Rig::new();
    let node = rig.session.node_id().to_owned();
    let (recipe, transfer, _) = material(&node);
    let endpoint = iroh::SecretKey::generate().public().to_string();
    let carrier = Identity::from_secret(&endpoint, &rig.signer.secret_bytes()).unwrap();
    let registration =
        joined_shard::register(&rig.session, &rig.signer, &carrier, rig.time.now()).unwrap();
    let source = iroh::SecretKey::generate().public().to_string();
    let source = Identity::from_secret(&source, &[33u8; 32]).unwrap();
    let offer = joined_shard::issue(
        &rig.coordinator,
        &registration,
        "gr-old-grant",
        &transfer,
        &recipe,
        &source,
    )
    .unwrap();
    let expires = offer["message"]["grant"]["message"]["expires_at_unix_ms"]
        .as_u64()
        .unwrap();
    let mut shard = JoinedShard::begin(
        &rig.member_root,
        &rig.session,
        &rig.signer,
        &carrier,
        &offer,
        rig.time.now(),
    )
    .unwrap();
    let step = lease_seconds() * 2 / 3;
    while rig.time.ms() < expires {
        rig.time.advance(step);
        rig.renew().unwrap();
    }
    let refused = shard
        .write_chunk(0, &[0], &rig.session, &rig.signer, &carrier, rig.time.now())
        .map_err(|e| e.0.to_owned());
    assert_eq!(refused, Err("joined_shard_grant_invalid".to_owned()));
    assert_eq!(rig.debt(), 1, "a refused transfer keeps its byte debt");
    drop(shard);
}
