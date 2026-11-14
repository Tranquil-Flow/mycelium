// SPDX-License-Identifier: AGPL-3.0-only
//! The owner's shard source serves a job only to that job's recipient (REVIEW-laneLAN LAN-A1 / F4).
//! Real seed, pairing, member consent, seed-signed offer and the product `transfer`; the member is
//! an in-process `MemberLink` whose `receive` dials the source itself, once with a key the offer
//! does not name and once with the recipient's carrier key. Direct LAN on 127.0.0.1 (an explicit
//! policy entry, as the runtime LAN suites do) and LocalOnly loopback, so no host interface is
//! needed.
use api::{
    app_host, handoff,
    joined_owner::{
        link::{MemberLink, Refusal},
        source::{ShardSource, transfer},
    },
    joined_shard,
    member::Session,
    peer,
    preferences::{resource_policy, standing_consent},
};
use budgets::{BudgetId as B, Entries};
use iroh::endpoint::{Connection, ConnectionError};
use protocol::identity::Identity;
use runtime::iroh_host::{LAN_KEY_REFUSED, LanPolicy, LoopbackHost};
use serde_json::{Value, json};
use std::{
    fs,
    net::IpAddr,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc,
    },
    time::Duration,
};
use transport::{Channel, Clock};
#[path = "../../runtime/tests/support/udp_hold.rs"]
mod udp_hold;

static NEXT: AtomicU64 = AtomicU64::new(0);
fn span(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
fn loopback() -> IpAddr {
    IpAddr::from([127, 0, 0, 1])
}
fn direct_lan() -> Option<(IpAddr, LanPolicy)> {
    Some((
        loopback(),
        LanPolicy::with_explicit(vec![loopback()]).unwrap(),
    ))
}

/// A paired, consenting member whose carrier key (the offer's recipient) this test holds.
struct Pair {
    root: PathBuf,
    owner: PathBuf,
    member_root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    recipient: iroh::SecretKey,
    registration: Value,
}
impl Pair {
    fn new() -> Self {
        let root = std::env::temp_dir().join(format!(
            "lan-source-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::SeqCst)
        ));
        fs::create_dir(&root).unwrap();
        let root = fs::canonicalize(root).unwrap();
        let owner = root.join("owner");
        let member_root = root.join("member");
        fs::create_dir(&owner).unwrap();
        fs::create_dir(&member_root).unwrap();
        let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "lan-swarm", None).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) =
            app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
        let coordinator = handoff::Coordinator {
            signer: seed_signer,
            swarm_id: "lan-swarm".into(),
            seed_url: seed.seed_url.clone(),
        };
        let ttl = B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
            .span(Entries::ZERO)
            .unwrap()
            .millis()
            / 1000;
        let handoff = handoff::approve_pairing(&owner, true, Some(&coordinator), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}), app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv {
            signer: &signer,
            clock: &app_host::now,
            random_hex: &random,
            seed: &options,
        };
        let mut accepted =
            peer::accept_handoff(&member_root, true, &handoff.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let session: Session = accepted.session.unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let binding = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":binding.swarm_id,"node_id":binding.node_id,"seed_key_digest":binding.seed_key_digest}), app_host::now()).unwrap();
        let recipient = iroh::SecretKey::generate();
        let carrier =
            Identity::from_secret(&recipient.public().to_string(), &signer.secret_bytes()).unwrap();
        let registration =
            joined_shard::register(&session, &signer, &carrier, app_host::now()).unwrap();
        Self {
            root,
            owner,
            member_root,
            seed,
            recipient,
            registration,
        }
    }
    /// The compiled stage-0 shard of the planner fixture, written into the owner's object store.
    fn material(&self) -> (Value, Value, PathBuf) {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../planner/tests/fixtures/source-slices.json"
        ))
        .unwrap();
        let case = &fixture["cases"][0];
        let mut recipe = case["node_plans"]["node-0"].clone();
        recipe["node_id"] = self.registration["message"]["member"]["node_id"].clone();
        recipe.as_object_mut().unwrap().remove("node_plan_digest");
        recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
        let file = case["node_files"]["node-0"][0].clone();
        let name = file["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        let encoded = case["shard_files"][name].as_str().unwrap();
        let bytes: Vec<u8> = (0..encoded.len())
            .step_by(2)
            .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
            .collect();
        let objects = self.owner.join("objects");
        fs::create_dir_all(&objects).unwrap();
        let hash = file["content_digest"].as_str().unwrap();
        fs::write(objects.join(hash.strip_prefix("sha256:").unwrap()), &bytes).unwrap();
        (recipe, file, objects)
    }
    fn source(&self, lan: Option<(IpAddr, LanPolicy)>) -> (ShardSource, Value, Value) {
        let (recipe, file, objects) = self.material();
        let source = ShardSource::start(
            self.seed.coordinator.clone(),
            objects,
            self.owner.join("journal"),
            lan,
        )
        .unwrap();
        (source, recipe, file)
    }
}
impl Drop for Pair {
    fn drop(&mut self) {
        self.seed.close();
        fs::remove_dir_all(&self.root).unwrap();
    }
}

/// What the test runs as the member's `receive`.
type Receive<'a> = Box<dyn FnMut(&Value) -> Result<Value, Refusal> + 'a>;
/// One intruder attempt: the source's answer (or why there is none) and its close code.
type Attempt = (Result<Vec<u8>, String>, Option<u64>);
/// The member side of `transfer`: its own consent answer, and `receive` run by the test.
struct Member<'a> {
    root: PathBuf,
    receive: Receive<'a>,
    reply: Option<Result<Value, Refusal>>,
}
impl MemberLink for Member<'_> {
    fn begin(&mut self, command: Value) -> Result<(), Refusal> {
        self.reply = Some(match command["command"].as_str() {
            Some("consent_status") => Ok(json!({
                "consent": standing_consent::load(&self.root).unwrap(),
                "policy": resource_policy::read(&self.root).unwrap(),
            })),
            Some("receive") => (self.receive)(&command),
            _ => Err(Refusal("unexpected_command".into())),
        });
        Ok(())
    }
    fn finish(&mut self, _: Duration) -> Result<Value, Refusal> {
        self.reply
            .take()
            .unwrap_or_else(|| Err(Refusal("no_command".into())))
    }
}

/// A client endpoint on loopback under `secret`, dialing the source's artifact channel.
struct Client {
    rt: tokio::runtime::Runtime,
}
impl Client {
    fn new() -> Self {
        Self {
            rt: tokio::runtime::Builder::new_multi_thread()
                .worker_threads(1)
                .enable_all()
                .build()
                .unwrap(),
        }
    }
    /// Dial, then run `body` on the connection; the host is closed before returning.
    fn with<T>(
        &self,
        secret: iroh::SecretKey,
        source: &iroh::EndpointAddr,
        body: impl FnOnce(&tokio::runtime::Runtime, &Connection) -> T,
    ) -> Result<T, String> {
        let clock = api::managed_carrier::HostClock::default();
        let until = |id: B| {
            clock
                .monotonic()
                .checked_add(id.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = LoopbackHost::bind_protocols(
            &self.rt,
            &clock,
            until(B::SidecarLocalHandshake),
            secret,
            &[Channel::ArtifactChunk],
        )
        .unwrap();
        let out = host
            .connect_channel(
                source,
                Channel::ArtifactChunk,
                &clock,
                until(B::SidecarLocalHandshake),
            )
            .map(|connection| body(&self.rt, &connection))
            .map_err(|e| format!("dial:{e:?}"));
        host.close(&clock, until(B::RouterClose)).unwrap();
        out
    }
}
/// One artifact request on its own stream; the source's framed answer, or why there is none.
fn exchange(
    rt: &tokio::runtime::Runtime,
    connection: &Connection,
    body: &[u8],
) -> Result<Vec<u8>, String> {
    rt.block_on(async {
        tokio::time::timeout(span(B::RouterClose), async {
            let (mut send, mut recv) = connection.open_bi().await.map_err(|e| e.to_string())?;
            send.write_all(body).await.map_err(|e| e.to_string())?;
            send.finish().map_err(|e| e.to_string())?;
            recv.read_to_end(1 << 20).await.map_err(|e| e.to_string())
        })
        .await
        .map_err(|_| "no answer".to_owned())?
    })
}
/// The application close code the source ended `connection` with, if it did.
fn close_code(connection: &Connection) -> Option<u64> {
    match connection.close_reason() {
        Some(ConnectionError::ApplicationClosed(close)) => Some(close.error_code.into_inner()),
        _ => None,
    }
}
fn source_address(command: &Value) -> iroh::EndpointAddr {
    let id: iroh::EndpointId = command["source_id"].as_str().unwrap().parse().unwrap();
    let socket = command["source_socket"].as_str().unwrap().parse().unwrap();
    iroh::EndpointAddr::new(id).with_ip_addr(socket)
}

fn an_unrelated_key_cannot_consume_the_job(lan: Option<(IpAddr, LanPolicy)>) {
    let pair = Pair::new();
    let (source, recipe, file) = pair.source(lan);
    let mut intruder: Option<Attempt> = None;
    let mut served: Option<Result<(), String>> = None;
    let mut member = Member {
        root: pair.member_root.clone(),
        receive: Box::new(|command| {
            let source = source_address(command);
            let client = Client::new();
            // A device on the network with its own key: it may complete a handshake, but it must
            // not get the job's connection (no answer to a request), and the job must go on.
            let attempt = client.with(iroh::SecretKey::generate(), &source, |rt, connection| {
                let answer = exchange(rt, connection, b"{\"message\":{}}");
                (answer, close_code(connection))
            });
            intruder = Some(match attempt {
                Ok(seen) => seen,
                Err(dial) => (Err(dial), None),
            });
            // The recipient's own carrier key is then served.
            served = Some(
                client
                    .with(pair.recipient.clone(), &source, |rt, connection| {
                        let answer = exchange(rt, connection, b"{\"message\":{}}")?;
                        connection.close(0u32.into(), b"done");
                        assert!(!answer.is_empty());
                        Ok(())
                    })
                    .and_then(|r| r),
            );
            Ok(json!({"received_bytes": 0}))
        }),
        reply: None,
    };
    let done = transfer(
        &source,
        &pair.seed.coordinator,
        &mut member,
        &pair.registration,
        "lan-source-0",
        &file,
        &recipe,
        span(B::RouterClose),
    );
    drop(member);
    source.close().unwrap();
    let (answer, code) = intruder.expect("the member's receive ran");
    assert!(
        answer.is_err(),
        "a key the offer does not name was served: {:?}",
        answer.map(|a| String::from_utf8_lossy(&a).into_owned())
    );
    if let Some(code) = code {
        assert_eq!(
            code,
            u64::from(LAN_KEY_REFUSED),
            "refused as an unadmitted key"
        );
    }
    assert_eq!(
        served,
        Some(Ok(())),
        "the recipient is served after the intruder"
    );
    assert!(
        done.is_ok(),
        "the job ends with the recipient: {:?}",
        done.err()
    );
}

/// Proves: LAN-A1 = F4 (direct LAN: only the offer's recipient key is admitted; an unrelated key's
/// handshake is refused, it gets no answer, and the job still serves the recipient).
#[test]
fn on_the_lan_an_unrelated_key_cannot_consume_the_shard_job() {
    an_unrelated_key_cannot_consume_the_job(direct_lan());
}

/// Proves: LAN-A1 (LocalOnly: the same rule by the accepted connection's key).
#[test]
fn on_loopback_an_unrelated_key_cannot_consume_the_shard_job() {
    an_unrelated_key_cannot_consume_the_job(None);
}

/// Proves: LAN-A1 (Stop: a peer that keeps a served connection busy with invalid requests cannot
/// hold the source's `close`; the connection is closed and `close` returns within its own bound).
#[test]
fn a_peer_sending_invalid_requests_cannot_outlive_stop() {
    let pair = Pair::new();
    let (source, recipe, file) = pair.source(direct_lan());
    let (busy_tx, busy) = mpsc::channel::<()>();
    let mut flooder: Option<std::thread::JoinHandle<Result<u64, String>>> = None;
    let mut member = Member {
        root: pair.member_root.clone(),
        receive: Box::new(|command| {
            let source = source_address(command);
            let secret = pair.recipient.clone();
            let busy_tx = busy_tx.clone();
            flooder = Some(std::thread::spawn(move || {
                Client::new().with(secret, &source, |rt, connection| {
                    let mut answered = 0u64;
                    // Invalid requests, each answered with a refusal, until the source ends
                    // the connection.
                    while exchange(rt, connection, b"not a request").is_ok() {
                        answered += 1;
                        if answered == 1 {
                            busy_tx.send(()).unwrap();
                        }
                    }
                    answered
                })
            }));
            busy.recv_timeout(span(B::RouterClose))
                .expect("the source is serving the flooder");
            // The member gives up (as a cancelled receive does): the owner stops the source.
            Err(Refusal("member_receive_cancelled".into()))
        }),
        reply: None,
    };
    let refused = transfer(
        &source,
        &pair.seed.coordinator,
        &mut member,
        &pair.registration,
        "lan-source-stop",
        &file,
        &recipe,
        span(B::RouterClose),
    )
    .err()
    .unwrap();
    drop(member);
    assert_eq!(refused.0, "member_receive_cancelled");
    let (closed_tx, closed) = mpsc::channel();
    let closer = std::thread::spawn(move || closed_tx.send(source.close()).unwrap());
    let bound = span(B::RouterClose) + span(B::RouterClose);
    let result = closed
        .recv_timeout(bound)
        .expect("close returned within its bound while the peer kept sending");
    assert!(result.is_ok(), "{result:?}");
    closer.join().unwrap();
    let answered = flooder.unwrap().join().unwrap().unwrap();
    assert!(answered >= 1, "the flooder was served before Stop");
}

/// `ShardSource::close`'s stated bound, the same in every state (`source.rs` module note): one
/// accept slice (`OwnerPoll`) to see Stop, then one `RouterClose` for the endpoint's close and
/// for storage work still running; asserted with one more `RouterClose` of scheduling margin.
fn close_bound() -> Duration {
    span(B::OwnerPoll) + span(B::RouterClose) + span(B::RouterClose)
}
/// Close `source` on its own thread: its result if it returned within [`close_bound`].
fn close_within_bound(
    source: ShardSource,
) -> (
    Result<Result<(), Refusal>, mpsc::RecvTimeoutError>,
    std::thread::JoinHandle<bool>,
) {
    let (closed_tx, closed) = mpsc::channel();
    // `true` if the test was still waiting when `close` returned.
    let closer = std::thread::spawn(move || closed_tx.send(source.close()).is_ok());
    (closed.recv_timeout(close_bound()), closer)
}
/// Replace the content-addressed object at `path` with a FIFO: a read of it blocks in `open(2)`
/// until a writer appears, as storage work that cannot observe Stop does (a stalled disk).
fn hold_object(path: &Path) {
    fs::remove_file(path).unwrap();
    let made = std::process::Command::new("/usr/bin/mkfifo")
        .arg(path)
        .status()
        .unwrap();
    assert!(made.success(), "mkfifo {}", path.display());
}
/// Release a held object: true if a reader was still blocked opening it (a non-blocking writer
/// open succeeds only then, `ENXIO` otherwise), which it then lets through (to a failed read).
fn release_object(path: &Path) -> bool {
    fs::OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_NONBLOCK)
        .open(path)
        .is_ok()
}

fn a_stalled_handshake_cannot_hold_close(lan: Option<(IpAddr, LanPolicy)>) {
    let pair = Pair::new();
    let (source, recipe, file) = pair.source(lan);
    let mut stalled: Option<(udp_hold::Hold, std::thread::JoinHandle<bool>)> = None;
    let mut member = Member {
        root: pair.member_root.clone(),
        receive: Box::new(|command| {
            let target = source_address(command);
            let at = *target.ip_addrs().next().unwrap();
            // A local process that starts a handshake and never finishes it: every answer
            // of the source is held, so the handshake stays in flight.
            let hold = udp_hold::Hold::start(at, span(B::SidecarReceivePollWait));
            let parked = iroh::EndpointAddr::new(target.id).with_ip_addr(hold.address());
            let dialer = std::thread::spawn(move || {
                Client::new()
                    .with(iroh::SecretKey::generate(), &parked, |_, _| ())
                    .is_err()
            });
            assert!(
                hold.answered(span(B::RouterClose)),
                "the source took the stalled handshake"
            );
            // From now on the stalled peer sends nothing either: not even its own close.
            hold.freeze();
            stalled = Some((hold, dialer));
            // The member gives up (a cancelled receive): the owner stops the source.
            Err(Refusal("member_receive_cancelled".into()))
        }),
        reply: None,
    };
    let refused = transfer(
        &source,
        &pair.seed.coordinator,
        &mut member,
        &pair.registration,
        "lan-source-stalled",
        &file,
        &recipe,
        span(B::RouterClose),
    )
    .err()
    .unwrap();
    drop(member);
    assert_eq!(refused.0, "member_receive_cancelled");
    let (closed, closer) = close_within_bound(source);
    let (hold, dialer) = stalled.expect("the member's receive ran");
    let result = closed.unwrap_or_else(|_| {
        panic!(
            "a handshake in flight held the source's close past {:?}",
            close_bound()
        )
    });
    assert!(result.is_ok(), "{result:?}");
    assert!(closer.join().unwrap(), "close answered the waiting test");
    drop(hold);
    assert!(dialer.join().unwrap(), "the stalled dial never completed");
}

/// Proves: R2-2 / LAN-R2-A1 (LocalOnly: a local process that starts a handshake with the shard
/// source and never finishes it cannot hold `close` past its bound).
#[test]
fn on_loopback_a_stalled_handshake_cannot_hold_the_sources_close() {
    a_stalled_handshake_cannot_hold_close(None);
}

/// Proves: LAN-R2-A1 (direct LAN: the same; in-flight handshakes end with the host's close).
#[test]
fn on_the_lan_a_stalled_handshake_cannot_hold_the_sources_close() {
    a_stalled_handshake_cannot_hold_close(direct_lan());
}

/// Proves: LAN-R2-A1 (verifying at admission: object verification whose storage read cannot
/// observe Stop is left to finish in the background; `close` returns within its bound and does
/// not wait for it).
#[test]
fn held_storage_while_verifying_at_admission_cannot_hold_the_sources_close() {
    let pair = Pair::new();
    let (recipe, file, objects) = pair.material();
    let digest = file["content_digest"].as_str().unwrap();
    let object = objects.join(digest.strip_prefix("sha256:").unwrap());
    hold_object(&object);
    let source = ShardSource::start(
        pair.seed.coordinator.clone(),
        objects,
        pair.owner.join("journal"),
        direct_lan(),
    )
    .unwrap();
    let mut member = Member {
        root: pair.member_root.clone(),
        receive: Box::new(|_| panic!("no offer is issued for an object still being verified")),
        reply: None,
    };
    // The admission job does not finish while its read is held: that is the job's own wait.
    let refused = transfer(
        &source,
        &pair.seed.coordinator,
        &mut member,
        &pair.registration,
        "lan-source-held-admit",
        &file,
        &recipe,
        span(B::RouterClose),
    )
    .err()
    .unwrap();
    drop(member);
    assert_eq!(refused.0, "source_job_timeout");
    let (closed, closer) = close_within_bound(source);
    let blocked = release_object(&object);
    let result = match closed {
        Ok(result) => result,
        Err(_) => {
            closer.join().unwrap();
            panic!(
                "held storage work held the source's close past {:?}",
                close_bound()
            )
        }
    };
    assert!(closer.join().unwrap(), "close answered the waiting test");
    assert!(result.is_ok(), "{result:?}");
    assert!(
        blocked,
        "the admission read was not held across close (the test did not reach its state)"
    );
}

/// Proves: LAN-R2-A1 (serving: a chunk read the artifact service started on the blocking pool,
/// after its admission was journaled, is left to finish in the background; `close` returns
/// within its bound instead of the runtime's drop waiting for it).
#[test]
fn held_storage_while_serving_a_chunk_cannot_hold_the_sources_close() {
    let pair = Pair::new();
    let (source, recipe, file) = pair.source(direct_lan());
    let digest = file["content_digest"].as_str().unwrap().to_owned();
    let object = pair
        .owner
        .join("objects")
        .join(digest.strip_prefix("sha256:").unwrap());
    let journal = pair
        .owner
        .join("journal")
        .join(runtime::artifact_journal::LEDGER_FILE);
    let (signer, _, _) = app_host::identity(&pair.member_root).unwrap();
    let carrier =
        Identity::from_secret(&pair.recipient.public().to_string(), &signer.secret_bytes())
            .unwrap();
    let mut answered: Option<Result<Vec<u8>, String>> = None;
    let mut journaled = 0;
    let mut member = Member {
        root: pair.member_root.clone(),
        receive: Box::new(|command| {
            // Admitted and verified at admission; now its bytes are held on disk.
            hold_object(&object);
            let limits = runtime::sidecar_artifact::grant_limits().unwrap();
            let now = (app_host::now() * 1000.0).floor() as u64;
            let offer = &command["offer"];
            let grant = protocol::artifact_grant::verify_grant(
                &offer["message"]["grant"],
                &offer["verification_key"],
                now,
                limits,
            )
            .unwrap();
            let request = protocol::artifact_grant::sign_chunk_request(
                &carrier,
                &grant,
                "held-chunk",
                "held-chunk-nonce",
                command["source_id"].as_str().unwrap(),
                &digest,
                0,
                1,
                now,
                limits,
            )
            .unwrap();
            let body = serde_json::to_vec(&request).unwrap();
            let target = source_address(command);
            answered = Some(
                Client::new()
                    .with(pair.recipient.clone(), &target, |rt, connection| {
                        exchange(rt, connection, &body)
                    })
                    .and_then(|r| r),
            );
            journaled = fs::read_to_string(&journal).unwrap().lines().count();
            Err(Refusal("member_receive_cancelled".into()))
        }),
        reply: None,
    };
    let refused = transfer(
        &source,
        &pair.seed.coordinator,
        &mut member,
        &pair.registration,
        "lan-source-held-serve",
        &file,
        &recipe,
        span(B::RouterClose),
    )
    .err()
    .unwrap();
    drop(member);
    assert_eq!(refused.0, "member_receive_cancelled");
    let (closed, closer) = close_within_bound(source);
    let blocked = release_object(&object);
    let result = match closed {
        Ok(result) => result,
        Err(_) => {
            closer.join().unwrap();
            panic!(
                "a held chunk read held the source's close past {:?}",
                close_bound()
            )
        }
    };
    assert!(closer.join().unwrap(), "close answered the waiting test");
    assert!(result.is_ok(), "{result:?}");
    assert_eq!(
        answered,
        Some(Err("no answer".to_owned())),
        "the chunk request was answered although its read was held"
    );
    assert_eq!(
        journaled, 2,
        "the request's admission (genesis + one line) was journaled before its read"
    );
    assert!(
        blocked,
        "the chunk read was not held across close (the test did not reach its state)"
    );
}
