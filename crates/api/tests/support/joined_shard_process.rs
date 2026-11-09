// SPDX-License-Identifier: AGPL-3.0-only
//! Exact-owned product member executable; owner test drives real pairing + QUIC.
use super::*;
use budgets::{BudgetId as B, Entries};
use runtime::{
    artifact_journal::{DurableConfig, DurableObjects},
    iroh_artifact_source::ArtifactService,
    iroh_host::LoopbackHost,
    sidecar_artifact::{grant_limits, serve_limits},
};
use std::{
    io::{BufRead, BufReader, Write},
    net::UdpSocket,
    process::{Child, Command, Stdio},
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};
use transport::{Channel, Clock};
fn allowance() -> Duration {
    Duration::from_millis(B::RouterClose.span(Entries::ZERO).unwrap().millis())
}
pub(super) struct Node {
    /// `None` = a member an operator started on another host (attached).
    pub(super) child: Option<Child>,
    pid: u32,
    pub(super) replies: mpsc::Receiver<Value>,
    reader: Option<std::thread::JoinHandle<()>>,
    remote: Option<super::member_remote::Link>,
    /// The member's control endpoint when driven over the signed channel.
    pub(super) control: Option<iroh::EndpointAddr>,
    done: bool,
    last_op: u64,
}
impl Node {
    /// Spawn the real member executable. With `B8_MEMBER_CONTROL=remote` it is
    /// driven ONLY over the signed remote control channel (stdin closed).
    pub(super) fn start(root: &std::path::Path) -> Self {
        let exe = std::env::var_os("B7_MEMBER_BINARY")
            .expect("build api --example shard_member and set B7_MEMBER_BINARY");
        if super::member_remote::remote_mode() {
            // The spawned member's operator decides consent at launch (C1).
            let decision = consent_file(root.parent().unwrap());
            let child = Command::new(exe)
                .arg("--state-root")
                .arg(root)
                .args(super::member_remote::member_args())
                .arg("--consent-file")
                .arg(&decision)
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap();
            let notice = root.join("member-control.json");
            let end = Instant::now() + allowance();
            while !notice.exists() {
                assert!(
                    Instant::now() < end,
                    "member control endpoint not published"
                );
                std::thread::yield_now();
            }
            let info: Value = serde_json::from_slice(&std::fs::read(&notice).unwrap()).unwrap();
            let c = &info["member_control"];
            let addr = c["addrs"].as_array().unwrap().iter().fold(
                iroh::EndpointAddr::new(c["endpoint_id"].as_str().unwrap().parse().unwrap()),
                |a, s| a.with_ip_addr(s.as_str().unwrap().parse().unwrap()),
            );
            assert_eq!(c["pid"], child.id());
            let pid = child.id();
            return Self::remote(Some(child), pid, addr);
        }
        let mut child = Command::new(exe)
            .arg("--state-root")
            .arg(root)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap();
        let out = child.stdout.take().unwrap();
        let (tx, replies) = mpsc::channel();
        let reader = std::thread::spawn(move || {
            for line in BufReader::new(out).lines() {
                let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
                if tx.send(v).is_err() {
                    return;
                }
            }
        });
        let pid = child.id();
        Self {
            child: Some(child),
            pid,
            replies,
            reader: Some(reader),
            remote: None,
            control: None,
            done: false,
            last_op: 0,
        }
    }
    /// A member started by an operator on another Mac (`B8_REMOTE_MEMBER`):
    /// its pid comes from its own signed status over the pinned channel.
    pub(super) fn attach(addr: iroh::EndpointAddr) -> Self {
        let status = super::member_remote::status(&addr, None)
            .unwrap_or_else(|code| panic!("remote member unreachable: {code}"));
        assert_eq!(status["ok"], true, "{status}");
        assert_eq!(
            status["result"]["joined"], false,
            "attach a fresh member root"
        );
        let pid = status["result"]["pid"].as_u64().unwrap() as u32;
        eprintln!("B8 attached remote member pid {pid} at {addr:?}");
        Self::remote(None, pid, addr)
    }
    fn remote(child: Option<Child>, pid: u32, addr: iroh::EndpointAddr) -> Self {
        let (link, replies) = super::member_remote::Link::start(addr.clone());
        Self {
            child,
            pid,
            replies,
            reader: None,
            remote: Some(link),
            control: Some(addr),
            done: false,
            last_op: 0,
        }
    }
    /// The next operation id this driver will use (strictly increasing).
    pub(super) fn next_op(&self) -> u64 {
        self.last_op + 1
    }
    pub(super) fn pid(&self) -> u32 {
        self.pid
    }
    pub(super) fn local(&self) -> bool {
        self.child.is_some()
    }
    /// Long operations (and every remote operation) get the next strictly
    /// increasing operation_id unless the caller supplied one.
    pub(super) fn send(&mut self, mut v: Value) {
        if let Some(id) = v["operation_id"].as_u64() {
            self.last_op = self.last_op.max(id);
        } else if v["command"] != "cancel_operation"
            && (self.remote.is_some()
                || ["receive", "prepare_native", "native_step", "serve"]
                    .contains(&v["command"].as_str().unwrap_or_default()))
        {
            self.last_op += 1;
            v["operation_id"] = json!(self.last_op);
        }
        self.send_raw(v);
    }
    pub(super) fn send_raw(&mut self, v: Value) {
        if let Some(link) = &self.remote {
            link.send(v);
            return;
        }
        let input = self.child.as_mut().unwrap().stdin.as_mut().unwrap();
        writeln!(input, "{v}").unwrap();
        input.flush().unwrap();
    }
    pub(super) fn result_with(&self, timeout: Duration) -> Value {
        let v = self.replies.recv_timeout(timeout).unwrap();
        assert_eq!(v["ok"], true, "{v}");
        v["result"].clone()
    }
    pub(super) fn refusal(&self) -> String {
        let value = self.replies.recv_timeout(allowance()).unwrap();
        assert_eq!(value["ok"], false, "{value}");
        value["code"].as_str().unwrap().to_owned()
    }
    fn result(&self) -> Value {
        self.result_with(allowance())
    }
    pub(super) fn call(&mut self, v: Value) -> Value {
        self.send(v);
        self.result()
    }
    /// Wait for the exact spawned process to exit (death phase).
    pub(super) fn wait_exit(&mut self) -> std::process::ExitStatus {
        let status = self.child.as_mut().expect("local member").wait().unwrap();
        self.done = true;
        status
    }
    pub(super) fn close(&mut self) {
        self.call(json!({"command":"close"}));
        self.remote.take();
        if let Some(child) = self.child.as_mut() {
            child.stdin.take();
            let end = Instant::now() + allowance();
            loop {
                if let Some(status) = child.try_wait().unwrap() {
                    assert!(status.success());
                    break;
                }
                assert!(Instant::now() < end, "node reap deadline");
                std::thread::yield_now();
            }
        }
        self.done = true;
        if let Some(r) = self.reader.take() {
            r.join().unwrap();
        }
    }
}
impl Drop for Node {
    fn drop(&mut self) {
        self.remote.take();
        if !self.done
            && let Some(child) = self.child.as_mut()
        {
            if child.try_wait().unwrap().is_none() {
                child.kill().unwrap();
            }
            child.wait().unwrap();
        }
        if let Some(r) = self.reader.take() {
            r.join().unwrap();
        }
    }
}
/// Consent on the owner's product path (B16 S3, C1): a stdin member's local
/// operator decides through its private channel; a remote-controlled member's
/// operator decided at launch (`--consent-file`). Either way the owner only
/// requests the member's own consent and admits it via `joined_owner::consent`.
pub(super) fn member_consent(
    node: &mut Node,
    registration: &Value,
    coordinator: &seed::Coordinator,
) {
    if node.remote.is_none() {
        node.call(json!({"command":"consent","device_memory_bytes":16_000_000_000u64,
            "policy":{"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"},
            "consent":{"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}}));
    }
    let status = node.call(json!({"command":"consent_status"}));
    api::joined_owner::consent::require(coordinator, registration, &status, 1).unwrap();
}
/// Two-Mac runbook helper: prints the PUBLIC owner control pin for the
/// `B8_OWNER_CONTROL_KEY` file (the secret never leaves the owner Mac).
#[test]
#[ignore = "runbook helper: needs B8_OWNER_CONTROL_KEY"]
fn print_owner_control_pin() {
    if std::env::var_os("B8_OWNER_CONTROL_KEY").is_none() {
        eprintln!("print_owner_control_pin: set B8_OWNER_CONTROL_KEY to the owner key file");
        return;
    }
    println!(
        "B8_OWNER_CONTROL_PIN={}",
        super::member_remote::owner_secret().public()
    );
}
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real product node process"]
fn member_control_channel_is_pinned_signed_and_replay_safe() {
    use super::member_remote::{Link, owner_secret, raw, status, with_owner};
    let root = std::env::temp_dir().join(format!("b8-control-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    // Exact owned child, killed and reaped on any failure path.
    struct Reap(Child);
    impl Drop for Reap {
        fn drop(&mut self) {
            if self.0.try_wait().unwrap().is_none() {
                self.0.kill().unwrap();
            }
            self.0.wait().unwrap();
        }
    }
    let mut child = Reap(
        Command::new(std::env::var_os("B7_MEMBER_BINARY").unwrap())
            .arg("--state-root")
            .arg(&root)
            .args(super::member_remote::member_args())
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::inherit())
            .spawn()
            .unwrap(),
    );
    let notice = root.join("member-control.json");
    let end = Instant::now() + allowance();
    while !notice.exists() {
        assert!(
            Instant::now() < end,
            "member control endpoint not published"
        );
        std::thread::yield_now();
    }
    let info: Value = serde_json::from_slice(&fs::read(&notice).unwrap()).unwrap();
    let c = &info["member_control"];
    let addr = c["addrs"].as_array().unwrap().iter().fold(
        iroh::EndpointAddr::new(c["endpoint_id"].as_str().unwrap().parse().unwrap()),
        |a, s| a.with_ip_addr(s.as_str().unwrap().parse().unwrap()),
    );
    // Signed status, verified owner-side (pin + command digest).
    let began = Instant::now();
    let s = status(&addr, None).unwrap();
    let status_ms = began.elapsed().as_secs_f64() * 1000.0;
    assert_eq!(s["ok"], true, "{s}");
    assert_eq!(s["result"]["pid"], child.0.id());
    assert_eq!(s["result"]["joined"], false);
    // Exact-pid liveness on the member's host is bound to the exact worker
    // incarnation the member itself started. A pid it never owned is not a
    // liveness answer either way (`null`): the test process (alive, another
    // owner), launchd (pid 1, another user's process: EPERM to signal 0) and a
    // reaped pid that may be reused.
    let mut gone = Command::new("/usr/bin/true").spawn().unwrap();
    let gone_pid = gone.id() as i32;
    gone.wait().unwrap();
    let me = std::process::id() as i32;
    let probed = super::member_remote::probe(&addr, None, &[me, 1, gone_pid]).unwrap();
    for pid in [me, 1, gone_pid] {
        assert_eq!(
            probed["result"]["probe_alive"][pid.to_string()],
            Value::Null,
            "pid {pid} is not this member's worker: {probed}"
        );
    }
    assert_eq!(probed["result"]["worker_alive"], Value::Null, "{probed}");
    // An unpinned owner key: the member closes the connection, no reply.
    let stranger = iroh::SecretKey::generate();
    let refused = with_owner(&stranger, |owner, client, until| {
        api::member_control::Remote {
            addr: addr.clone(),
            enrolled: None,
        }
        .call(
            owner,
            client,
            json!({"command":"status"}),
            until(),
            app_host::now(),
        )
        .err()
        .map(|e| e.code)
    });
    assert!(refused.is_some(), "unpinned owner must get no reply");
    // Replay and misbinding over the pinned transport.
    let (replayed, misbound) = with_owner(owner_secret(), |owner, client, until| {
        let command = owner
            .command(&addr.id, json!({"command":"status"}), app_host::now())
            .unwrap();
        let bytes = serde_json::to_vec(&command).unwrap();
        let first = raw(client, &addr, &bytes, until()).unwrap();
        assert_eq!(first["message"]["reply"]["ok"], true, "{first}");
        let again = raw(client, &addr, &bytes, until()).unwrap();
        let other = iroh::SecretKey::generate().public();
        let wrong = owner
            .command(&other, json!({"command":"status"}), app_host::now())
            .unwrap();
        let wrong = raw(client, &addr, &serde_json::to_vec(&wrong).unwrap(), until()).unwrap();
        (
            again["message"]["reply"].clone(),
            wrong["message"]["reply"].clone(),
        )
    });
    assert_eq!(replayed["code"], "member_control_replayed", "{replayed}");
    assert_eq!(
        misbound["code"], "member_control_wrong_member",
        "{misbound}"
    );
    // Remote operations require an id; the signed `pair_request` result is
    // retrieved by id; remote `close` ends the member though stdin stays open.
    let (link, replies) = Link::start(addr.clone());
    link.send(json!({"command":"pair_request"}));
    let refusal = replies.recv_timeout(allowance()).unwrap();
    assert_eq!(
        refusal["code"], "operator_operation_id_required",
        "{refusal}"
    );
    link.send(json!({"command":"pair_request","operation_id":7}));
    let paired = replies.recv_timeout(allowance()).unwrap();
    assert_eq!(paired["ok"], true, "{paired}");
    assert!(!paired["result"].is_null(), "{paired}");
    link.send(json!({"command":"close","operation_id":8}));
    assert_eq!(replies.recv_timeout(allowance()).unwrap()["ok"], true);
    drop(link);
    let end = Instant::now() + allowance();
    let exit = loop {
        if let Some(status) = child.0.try_wait().unwrap() {
            break status;
        }
        assert!(Instant::now() < end, "member did not close");
        std::thread::yield_now();
    };
    assert!(exit.success(), "{exit:?}");
    eprintln!(
        "B8 control: status {status_ms:.1} ms; unpinned={refused:?}; replay and misbinding refused"
    );
    fs::remove_dir_all(&root).unwrap();
}
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real product node process"]
fn product_member_process_pairs_receives_verifies_and_refunds() {
    process_transfer(false, false);
}
/// Owner ruling 11: grant renewal during long transfers is deferred, so an
/// expired grant must fail the transfer clearly (typed code), never silently.
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real product node process"]
fn product_member_transfer_fails_clearly_when_the_grant_expires() {
    process_transfer(false, true);
}
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real stalled QUIC transfer"]
fn product_member_cancels_stalled_transfer_and_retains_debt_until_discard() {
    process_transfer(true, false);
}
fn process_transfer(stall: bool, expired: bool) {
    let root =
        std::env::temp_dir().join(format!("b7b-node-{}-{stall}-{expired}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b7b-node-swarm", None).unwrap();
    let member_root = root.join("member");
    let mut node = Node::start(&member_root);
    let request = node.call(json!({"command":"pair_request"}));
    let (signer, _, _) = app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let coordinator = handoff::Coordinator {
        signer,
        swarm_id: "b7b-node-swarm".into(),
        seed_url: seed.seed_url.clone(),
    };
    let ttl = B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    let handoff=handoff::approve_pairing(&owner,true,Some(&coordinator),&request,&json!(ttl),&json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),app_host::now(),&handoff::Entropy::os).unwrap();
    let options = seed.options();
    let registration=node.call(json!({"command":"accept","handoff":handoff.to_value(),"ca_pem":String::from_utf8(options.ca_pem.unwrap()).unwrap(),"seed_address":seed.address}));
    let member = registration["message"]["member"].clone();
    let row = seed
        .coordinator
        .member(member["node_id"].as_str().unwrap())
        .unwrap();
    assert_eq!(member["generation"], row["generation"]);
    member_consent(&mut node, &registration, &seed.coordinator);
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../planner/tests/fixtures/source-slices.json"
    ))
    .unwrap();
    let case = &fixture["cases"][0];
    let mut recipe = case["node_plans"]["node-0"].clone();
    recipe["node_id"] = member["node_id"].clone();
    recipe.as_object_mut().unwrap().remove("node_plan_digest");
    recipe["node_plan_digest"] = json!(protocol::canonical::digest(&recipe).unwrap());
    let transfer = case["node_files"]["node-0"][0].clone();
    let encoded = case["shard_files"][transfer["path"]
        .as_str()
        .unwrap()
        .strip_prefix("deployment/")
        .unwrap()]
    .as_str()
    .unwrap();
    let bytes: Vec<u8> = (0..encoded.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&encoded[i..i + 2], 16).unwrap())
        .collect();
    let objects = owner.join("objects");
    fs::create_dir(&objects).unwrap();
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
    let journal = owner.join("journal");
    let seed_owner = seed.coordinator.clone();
    let (ready_tx, ready_rx) = mpsc::channel();
    let (offer_tx, offer_rx) = tokio::sync::oneshot::channel();
    let (finished_tx, finished_rx) = mpsc::channel();
    let (stalled_tx, stalled_rx) = mpsc::channel();
    let source = std::thread::spawn(move || {
        let rt = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let deadline = || {
            clock
                .monotonic()
                .checked_add(B::RouterClose.span(Entries::ZERO).unwrap())
                .unwrap()
        };
        let mut host = LoopbackHost::bind_protocols(
            &rt,
            &clock,
            deadline(),
            iroh::SecretKey::generate(),
            &[Channel::ArtifactChunk],
        )
        .unwrap();
        let address = host.address().unwrap();
        let socket = *address.ip_addrs().next().unwrap();
        let source = Identity::from_secret(
            &address.id.to_string(),
            &app_host::random(32).unwrap().try_into().unwrap(),
        )
        .unwrap();
        let objects = DurableObjects::open(DurableConfig {
            objects,
            state_dir: journal,
            fence_capacity: B::ArtifactMaxFence.count().unwrap().get() as usize,
            clock_skew_ms: grant_limits().unwrap().clock_skew_ms,
            now: Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
        })
        .unwrap();
        ready_tx
            .send((address, source.public_key_record()))
            .unwrap();
        let offer = rt.block_on(offer_rx).unwrap();
        let wrapped = joined_shard::JoinedSource::new(seed_owner.clone(), offer, objects).unwrap();
        let service = ArtifactService::new(
            source,
            seed_owner.signer().public_key_record(),
            grant_limits().unwrap(),
            Arc::new(wrapped),
            serve_limits().unwrap(),
            // `expired`: the source's clock is past any grant the seed can sign.
            Arc::new(move || {
                let limits = grant_limits().unwrap();
                let late = if expired {
                    limits.max_grant_ttl_ms + 2 * limits.clock_skew_ms + 1
                } else {
                    0
                };
                (app_host::now() * 1000.0).floor() as u64 + late
            }),
        )
        .unwrap();
        let connection = host.accept(&clock, deadline()).unwrap();
        if stall {
            rt.block_on(async {
                let (_send, mut recv) = connection.accept_bi().await.unwrap();
                let raw = recv
                    .read_to_end(serve_limits().unwrap().wire.request_bytes.get() as usize)
                    .await
                    .unwrap();
                assert!(!raw.is_empty());
                stalled_tx.send(()).unwrap();
                tokio::time::timeout(allowance(), connection.closed())
                    .await
                    .unwrap();
            });
            drop(connection);
        } else {
            rt.block_on(service.serve_connection(connection)).unwrap();
        }
        finished_tx.send(()).unwrap();
        host.close(&clock, deadline()).unwrap();
        drop(host);
        drop(rt);
        assert!(UdpSocket::bind(socket).is_ok());
    });
    let (address, key) = ready_rx.recv_timeout(allowance()).unwrap();
    // Public source key only: no source secret crosses into the seed issuer.
    let source_identity_endpoint = address.id.to_string();
    let offer = joined_shard::issue_with_source(
        &seed.coordinator,
        &registration,
        "b7b-node-transfer",
        &transfer,
        &recipe,
        &source_identity_endpoint,
        &key,
    )
    .unwrap();
    offer_tx.send(offer.clone()).unwrap();
    // F6: a long operation without an operation_id cannot be cancelled: refused.
    node.send_raw(json!({"command":"receive","offer":offer,"source_id":address.id.to_string(),"source_socket":address.ip_addrs().next().unwrap()}));
    assert_eq!(node.refusal(), "operator_operation_id_required");
    let op = node.next_op();
    node.send(json!({"command":"receive","operation_id":op,"offer":offer,"source_id":address.id.to_string(),"source_socket":address.ip_addrs().next().unwrap()}));
    if stall {
        stalled_rx.recv_timeout(allowance()).unwrap();
        let wrong = node.call(json!({"command":"cancel_operation","operation_id":op + 1}));
        assert_eq!(wrong["cancel_requested"], false);
        let began = Instant::now();
        node.send(json!({"command":"cancel_operation","operation_id":op}));
        // Acknowledgment and operation result may race; inspect both identities.
        let replies: Vec<_> = (0..2)
            .map(|_| node.replies.recv_timeout(allowance()).unwrap())
            .collect();
        assert!(
            replies
                .iter()
                .any(|v| v["result"]["cancel_requested"] == true)
        );
        assert!(
            replies
                .iter()
                .any(|v| v["code"] == "member_operation_cancelled")
        );
        assert!(began.elapsed() < allowance());
        eprintln!("B7 stalled receive cancel={:?}", began.elapsed());
        finished_rx.recv_timeout(allowance()).unwrap();
        let ledger: Value = serde_json::from_slice(
            &fs::read(member_root.join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(
            ledger["entries"].as_object().unwrap().len(),
            1,
            "cancel is not refund"
        );
        node.send(json!({"command":"pair_request","operation_id":op}));
        assert_eq!(node.refusal(), "operator_operation_id_invalid");
        let next = node.next_op();
        node.call(json!({"command":"discard","operation_id":next}));
        let ledger: Value = serde_json::from_slice(
            &fs::read(member_root.join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(ledger["entries"], json!({}));
        node.close();
        source.join().unwrap();
        seed.close();
        fs::remove_dir_all(root).unwrap();
        return;
    }
    if expired {
        let began = Instant::now();
        let code = node.refusal();
        eprintln!("B8 expired grant: {code} after {:?}", began.elapsed());
        assert!(code.contains("artifact_grant_ineligible"), "{code}");
        // The failed transfer keeps its byte debt owned until an explicit discard.
        let ledger: Value = serde_json::from_slice(
            &fs::read(member_root.join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(ledger["entries"].as_object().unwrap().len(), 1);
        let next = node.next_op();
        node.call(json!({"command":"discard","operation_id":next}));
        finished_rx.recv_timeout(allowance()).unwrap();
        node.close();
        source.join().unwrap();
        seed.close();
        fs::remove_dir_all(root).unwrap();
        return;
    }
    let result = node.result();
    assert_eq!(result["pid"], node.pid());
    assert_ne!(node.pid(), std::process::id());
    assert_eq!(result["node_id"], member["node_id"]);
    assert_eq!(result["generation"], member["generation"]);
    assert_eq!(result["route_ready"], false);
    let path = PathBuf::from(result["path"].as_str().unwrap());
    assert_eq!(fs::read(&path).unwrap(), bytes);
    // A received shard must release its transfer connection while the member
    // remains alive, not rely on process exit or the source's idle timeout.
    let released = finished_rx.recv_timeout(allowance());
    let discarded = node.call(json!({"command":"discard"}));
    assert_eq!(discarded["inert_bytes_discarded"], true);
    assert!(!path.exists());
    // F4: a second discard owns nothing and must say so.
    let empty = node.call(json!({"command":"discard"}));
    assert_eq!(
        (
            empty["inert_bytes_discarded"].clone(),
            empty["nothing_owned"].clone()
        ),
        (json!(false), json!(true))
    );
    let ledger: Value = serde_json::from_slice(
        &fs::read(member_root.join("chain-runs/compiler-storage.private.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(ledger["entries"], json!({}));
    node.close();
    source.join().unwrap();
    seed.close();
    fs::remove_dir_all(root).unwrap();
    assert!(
        released.is_ok(),
        "transfer release while member alive: {released:?}"
    );
}

/// The member operator's own consent decision for a remote-controlled member
/// (`--consent-file`, member-side; TWO-MAC-TEST C1).
pub(super) fn consent_file(dir: &std::path::Path) -> std::path::PathBuf {
    let path = dir.join(format!(
        "member-consent-{}-{}.json",
        std::process::id(),
        app_host::hex(&app_host::random(4).unwrap())
    ));
    fs::write(
        &path,
        json!({"device_memory_bytes":16_000_000_000u64,
            "policy":{"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"},
            "consent":{"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}})
        .to_string(),
    )
    .unwrap();
    path
}
/// B16 S3 (gap row 12): consent stays member-side on the product control
/// channel. The pinned owner may not write a member's consent remotely; the
/// member operator decides (`--consent-file`), the member binds it to the swarm
/// it joins, and the owner reads it back member-signed (`consent_status`) and
/// admits the member only through `api::joined_owner::consent::require`.
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real product node process"]
fn product_consent_is_member_side_and_reaches_the_owner_signed() {
    use super::member_remote::{Link, member_args};
    struct Reap(Child);
    impl Drop for Reap {
        fn drop(&mut self) {
            if self.0.try_wait().unwrap().is_none() {
                self.0.kill().unwrap();
            }
            self.0.wait().unwrap();
        }
    }
    let root = std::env::temp_dir().join(format!("b16-consent-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let root = fs::canonicalize(root).unwrap();
    let owner = root.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b16-consent", None).unwrap();
    let (seed_signer, _, _) = app_host::identity_in(&owner, app_host::SEED_IDENTITY_FILE).unwrap();
    let issuer = handoff::Coordinator {
        signer: seed_signer,
        swarm_id: seed.coordinator.swarm_id().into(),
        seed_url: seed.seed_url.clone(),
    };
    let ttl = B::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    let file = consent_file(&root);
    let mut outcomes = Vec::new();
    for (name, operator_consent) in [("no-consent", false), ("member-consent", true)] {
        let member_root = root.join(name);
        let mut command = Command::new(std::env::var_os("B7_MEMBER_BINARY").unwrap());
        command
            .arg("--state-root")
            .arg(&member_root)
            .args(member_args());
        if operator_consent {
            command.arg("--consent-file").arg(&file);
        }
        let mut child = Reap(
            command
                .stdin(Stdio::null())
                .stdout(Stdio::null())
                .stderr(Stdio::inherit())
                .spawn()
                .unwrap(),
        );
        let notice = member_root.join("member-control.json");
        let end = Instant::now() + allowance();
        while !notice.exists() {
            assert!(
                Instant::now() < end,
                "member control endpoint not published"
            );
            assert!(child.0.try_wait().unwrap().is_none(), "member exited early");
            std::thread::yield_now();
        }
        let info: Value = serde_json::from_slice(&fs::read(&notice).unwrap()).unwrap();
        let c = &info["member_control"];
        let addr = c["addrs"].as_array().unwrap().iter().fold(
            iroh::EndpointAddr::new(c["endpoint_id"].as_str().unwrap().parse().unwrap()),
            |a, s| a.with_ip_addr(s.as_str().unwrap().parse().unwrap()),
        );
        let (link, replies) = Link::start(addr);
        let call = |command: Value| -> Value {
            link.send(command);
            replies.recv_timeout(allowance()).unwrap()
        };
        let request = call(json!({"command":"pair_request","operation_id":1}));
        let approved = handoff::approve_pairing(&owner, true, Some(&issuer), &request["result"], &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
            app_host::now(), &handoff::Entropy::os).unwrap();
        let registration = call(
            json!({"command":"accept","operation_id":2,"handoff":approved.to_value(),
            "ca_pem":String::from_utf8(seed.options().ca_pem.unwrap()).unwrap(),"seed_address":seed.address}),
        );
        assert_eq!(registration["ok"], true, "{registration}");
        let registration = registration["result"].clone();
        // The pinned owner may only request: a remote consent write is refused.
        let written = call(
            json!({"command":"consent","operation_id":3,"device_memory_bytes":16_000_000_000u64,
            "policy":{"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"},
            "consent":{"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}}),
        );
        assert_eq!(
            (written["ok"].clone(), written["code"].clone()),
            (json!(false), json!("member_consent_member_side")),
            "{name}: {written}"
        );
        // Member-signed consent status (enrolled key, verified by the link).
        let status = call(json!({"command":"consent_status","operation_id":4}));
        assert_eq!(status["ok"], true, "{status}");
        let admitted = api::joined_owner::consent::require(
            &seed.coordinator,
            &registration,
            &status["result"],
            1_000_000,
        );
        if operator_consent {
            let consent = admitted.unwrap();
            assert_eq!(consent["state"], "granted");
            assert_eq!(consent["binding"]["swarm_id"], seed.coordinator.swarm_id());
            assert_eq!(
                consent["binding"]["node_id"],
                registration["message"]["member"]["node_id"]
            );
        } else {
            assert_eq!(admitted.unwrap_err().code, "member_consent_required");
        }
        assert_eq!(
            call(json!({"command":"close","operation_id":5}))["ok"],
            true
        );
        drop(link);
        let end = Instant::now() + allowance();
        let exit = loop {
            if let Some(status) = child.0.try_wait().unwrap() {
                break status;
            }
            assert!(Instant::now() < end, "member did not close");
            std::thread::yield_now();
        };
        assert!(exit.success(), "{exit:?}");
        outcomes.push(json!({"member":name,"remote_write":written["code"],"status":status["result"]["consent"]["state"]}));
    }
    eprintln!("B16 consent: {outcomes:?}");
    seed.close();
    fs::remove_dir_all(&root).unwrap();
}
