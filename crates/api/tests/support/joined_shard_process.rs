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
    pub(super) child: Child,
    replies: mpsc::Receiver<Value>,
    reader: Option<std::thread::JoinHandle<()>>,
    done: bool,
    last_op: u64,
}
impl Node {
    pub(super) fn start(root: &std::path::Path) -> Self {
        let exe = std::env::var_os("B7_MEMBER_BINARY")
            .expect("build api --example shard_member and set B7_MEMBER_BINARY");
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
        Self {
            child,
            replies,
            reader: Some(reader),
            done: false,
            last_op: 0,
        }
    }
    /// Long operations get the next strictly increasing operation_id unless the
    /// caller supplied one (the member refuses an id-less long operation).
    pub(super) fn send(&mut self, mut v: Value) {
        if let Some(id) = v["operation_id"].as_u64() {
            self.last_op = self.last_op.max(id);
        } else if ["receive", "prepare_native", "native_step", "serve"]
            .contains(&v["command"].as_str().unwrap_or_default())
        {
            self.last_op += 1;
            v["operation_id"] = json!(self.last_op);
        }
        self.send_raw(v);
    }
    pub(super) fn send_raw(&mut self, v: Value) {
        let input = self.child.stdin.as_mut().unwrap();
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
    pub(super) fn close(&mut self) {
        self.call(json!({"command":"close"}));
        self.child.stdin.take();
        let end = Instant::now() + allowance();
        loop {
            if let Some(status) = self.child.try_wait().unwrap() {
                assert!(status.success());
                break;
            }
            assert!(Instant::now() < end, "node reap deadline");
            std::thread::yield_now();
        }
        self.done = true;
        self.reader.take().unwrap().join().unwrap();
    }
}
impl Drop for Node {
    fn drop(&mut self) {
        if !self.done {
            if self.child.try_wait().unwrap().is_none() {
                self.child.kill().unwrap();
            }
            self.child.wait().unwrap();
        }
        if let Some(r) = self.reader.take() {
            r.join().unwrap();
        }
    }
}
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real product node process"]
fn product_member_process_pairs_receives_verifies_and_refunds() {
    process_transfer(false);
}
#[test]
#[ignore = "requires explicitly built B7_MEMBER_BINARY; real stalled QUIC transfer"]
fn product_member_cancels_stalled_transfer_and_retains_debt_until_discard() {
    process_transfer(true);
}
fn process_transfer(stall: bool) {
    let root = std::env::temp_dir().join(format!("b7b-node-{}-{stall}", std::process::id()));
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
    node.call(json!({"command":"consent","device_memory_bytes":16_000_000_000u64,
        "policy":{"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"},
        "consent":{"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}}));
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
            Arc::new(|| (app_host::now() * 1000.0).floor() as u64),
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
    node.send(json!({"command":"receive","operation_id":1,"offer":offer,"source_id":address.id.to_string(),"source_socket":address.ip_addrs().next().unwrap()}));
    if stall {
        stalled_rx.recv_timeout(allowance()).unwrap();
        let wrong = node.call(json!({"command":"cancel_operation","operation_id":2}));
        assert_eq!(wrong["cancel_requested"], false);
        let began = Instant::now();
        node.send(json!({"command":"cancel_operation","operation_id":1}));
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
        node.send(json!({"command":"pair_request","operation_id":1}));
        assert_eq!(node.refusal(), "operator_operation_id_invalid");
        node.call(json!({"command":"discard","operation_id":2}));
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
    let result = node.result();
    assert_eq!(result["pid"], node.child.id());
    assert_ne!(node.child.id(), std::process::id());
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
