// SPDX-License-Identifier: AGPL-3.0-only
//! Real two-process shard loader/challenge qualification. Owner remains a test
//! driver; observation placement is controlled, native proofs/tokens are not.
use super::process::Node;
use super::*;
use api::{
    compiler_inputs,
    joined_owner::source::{ShardSource, transfer},
    joined_shard::native,
};
use base64::{Engine, engine::general_purpose::STANDARD};
use budgets::{BudgetId as B, Entries};
use std::{
    collections::BTreeMap,
    path::Path,
    sync::Arc,
    time::{Duration, Instant},
};
pub(super) fn allowance() -> Duration {
    Duration::from_millis(B::MemberChosenWall.span(Entries::ZERO).unwrap().millis())
}
pub(super) struct Root(pub(super) PathBuf);
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).unwrap();
    }
}
fn clone_file(source: &Path, target: &Path) {
    // APFS copy-on-write: never write model cache or allocate another full copy.
    assert!(
        std::process::Command::new("/bin/cp")
            .arg("-c")
            .arg(source.canonicalize().unwrap())
            .arg(target)
            .status()
            .unwrap()
            .success()
    );
}
/// LAN rehearsal: a bounded TCP forwarder that exposes the owner's loopback
/// seed (TLS end to end, installation CA pinned by the member, certificate name
/// 127.0.0.1) on this host's private LAN interface. Only allow-listed peers.
pub(super) struct Forwarder {
    pub(super) address: std::net::SocketAddr,
    stop: Arc<std::sync::atomic::AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Forwarder {
    fn start(
        interface: std::net::IpAddr,
        policy: runtime::iroh_host::LanPolicy,
        target: std::net::SocketAddr,
    ) -> Self {
        use std::sync::atomic::{AtomicBool, Ordering};
        let listener = std::net::TcpListener::bind((interface, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = std::thread::spawn(move || {
            // Every pipe is owned: on stop both sockets are shut down, which ends
            // the copies, and each copy thread is joined.
            let mut pipes: Vec<(std::net::TcpStream, std::thread::JoinHandle<()>)> = Vec::new();
            for incoming in listener.incoming() {
                if flag.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(client) = incoming else { continue };
                if !client.peer_addr().is_ok_and(|p| policy.allows(p.ip())) {
                    continue;
                }
                let Ok(server) = std::net::TcpStream::connect(target) else {
                    continue;
                };
                for (mut from, mut to) in [
                    (client.try_clone().unwrap(), server.try_clone().unwrap()),
                    (server, client),
                ] {
                    let held = from.try_clone().unwrap();
                    let copy = std::thread::spawn(move || {
                        if let Err(e) = std::io::copy(&mut from, &mut to) {
                            eprintln!("seed forwarder copy ended: {e}");
                        }
                        if let Err(e) = to.shutdown(std::net::Shutdown::Write) {
                            eprintln!("seed forwarder shutdown: {e}");
                        }
                    });
                    pipes.push((held, copy));
                }
            }
            for (stream, copy) in pipes {
                // NotConnected means the copy already finished both directions.
                if let Err(e) = stream.shutdown(std::net::Shutdown::Both) {
                    eprintln!("seed forwarder close: {e}");
                }
                copy.join().unwrap();
            }
        });
        Self {
            address,
            stop,
            thread: Some(thread),
        }
    }
}
impl Drop for Forwarder {
    fn drop(&mut self) {
        self.stop.store(true, std::sync::atomic::Ordering::SeqCst);
        // Wake the blocking accept; a refused connect means it already ended.
        if let Err(e) = std::net::TcpStream::connect(self.address) {
            eprintln!("seed forwarder wake: {e}");
        }
        if let Some(t) = self.thread.take() {
            t.join().unwrap();
        }
    }
}
/// Owner admission step = PRODUCT code (api::joined_owner::link::admit), then
/// the member's own consent is requested (member-side, C1).
fn pair(
    node: &mut Node,
    root: &Path,
    seed: &app_host::SeedRuntime<app_host::Unmanaged>,
    seed_address: std::net::SocketAddr,
) -> Value {
    let registration = api::joined_owner::link::admit(
        node,
        root,
        seed,
        seed_address,
        &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
        allowance(),
    )
    .unwrap();
    super::process::member_consent(node, &registration, &seed.coordinator);
    registration
}
#[test]
#[ignore = "real Metal + local Qwen3; explicit B7_MEMBER_BINARY and native binary pins required"]
fn two_member_processes_prepare_received_shards_and_match_reference() {
    qualify(false);
}
#[test]
#[ignore = "real Metal received-shard prepare only; not two-node challenge"]
fn member_process_prepares_received_shard_and_verifies_enrolled_proof() {
    qualify(true);
}
/// Paired members holding verified received shards; native workers not started.
pub(super) struct Fixture {
    pub(super) seed: app_host::SeedRuntime<app_host::Unmanaged>,
    pub(super) member_roots: [PathBuf; 2],
    pub(super) nodes: [Node; 2],
    pub(super) registrations: Vec<Value>,
    pub(super) ids: Vec<String>,
    pub(super) source: PathBuf,
    pub(super) offers: Vec<Value>,
    pub(super) paths: Vec<PathBuf>,
    pub(super) ranges: Vec<[usize; 2]>,
    pub(super) manifest_digest: String,
    pub(super) resource_gate: std::ffi::OsString,
    pub(super) began: Instant,
    /// LAN rehearsal/physical: the seed forwarder (dropped with the fixture).
    pub(super) forwarder: Option<Forwarder>,
    // Last: removed after every owned process above was reaped.
    pub(super) root: Root,
}
/// Refresh host admission immediately before EACH native load.
pub(super) fn admit_native_load(gate: &std::ffi::OsStr, label: &str) {
    let gate = std::process::Command::new("python3")
        .arg(gate)
        .args(["--memory-gb", "6", "--duration-s"])
        .arg(allowance().as_secs().to_string())
        .arg("--json")
        .output()
        .unwrap();
    eprintln!(
        "B7 pre-native resource gate {label}: {}",
        String::from_utf8_lossy(&gate.stdout)
    );
    assert!(
        gate.status.success(),
        "resource admission refused before native launch"
    );
}
fn qualify(prepare_only: bool) {
    let count = if prepare_only { 1 } else { 2 };
    let fx = setup(count, "b7c-model");
    let Fixture {
        root: _root,
        seed,
        member_roots,
        mut nodes,
        registrations,
        ids: _,
        source,
        offers,
        paths,
        ranges,
        manifest_digest: _,
        resource_gate,
        began,
        forwarder: _forwarder,
    } = fx;
    qualify_received(
        prepare_only,
        count,
        seed,
        member_roots,
        &mut nodes,
        registrations,
        source,
        offers,
        paths,
        ranges,
        resource_gate,
        began,
    );
}
pub(super) fn setup(count: usize, label: &str) -> Fixture {
    let resource_gate = std::env::var_os("B7_RESOURCE_GATE")
        .expect("explicit local resource preflight script required");
    let prepare_only = count == 1;
    let began = Instant::now();
    let path = std::env::temp_dir().join(format!("{label}-{}", std::process::id()));
    fs::create_dir(&path).unwrap();
    let root = Root(path.canonicalize().unwrap());
    let owner = root.0.join("owner");
    fs::create_dir(&owner).unwrap();
    let seed = app_host::start_seed::<app_host::Unmanaged>(&owner, "b7c-model", None).unwrap();
    let lan = super::member_remote::lan();
    let forwarder = lan
        .clone()
        .map(|(ip, policy)| Forwarder::start(ip, policy, seed.address));
    let seed_address = forwarder.as_ref().map_or(seed.address, |f| f.address);
    let mut member_roots = [root.0.join("member0"), root.0.join("member1")];
    // Physical two-Mac run: member 1 was started by an operator on the other Mac.
    let second = match std::env::var("B8_REMOTE_MEMBER") {
        Ok(remote) => Node::attach(super::member_remote::parse_remote(&remote)),
        Err(_) => Node::start(&member_roots[1]),
    };
    let mut nodes = [Node::start(&member_roots[0]), second];
    let mut registrations: Vec<_> = nodes
        .iter_mut()
        .map(|n| pair(n, &owner, &seed, seed_address))
        .collect();
    if registrations[0]["message"]["member"]["node_id"]
        .as_str()
        .unwrap()
        > registrations[1]["message"]["member"]["node_id"]
            .as_str()
            .unwrap()
    {
        registrations.swap(0, 1);
        nodes.swap(0, 1);
        member_roots.swap(0, 1);
    }
    let ids: Vec<String> = registrations
        .iter()
        .map(|r| r["message"]["member"]["node_id"].as_str().unwrap().into())
        .collect();
    eprintln!(
        "B7c paired {} {} elapsed={:?}",
        nodes[0].pid(),
        nodes[1].pid(),
        began.elapsed()
    );
    let cache = PathBuf::from(
        std::env::var_os("MYCELIUM_QWEN3_SNAPSHOT").expect("explicit local snapshot"),
    );
    let source = root.0.join("snapshot");
    fs::create_dir(&source).unwrap();
    for file in ["config.json", "model.safetensors"] {
        clone_file(&cache.join(file), &source.join(file));
    }
    let manifest =
        api::local_manifest::compile_local_model_manifest(&api::local_manifest::LocalModelSource {
            root: source.clone(),
            model_id: "Qwen/Qwen3-0.6B".into(),
            requested_revision: "main".into(),
            resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
            tokenizer_assets: Ok(Vec::new()),
        })
        .unwrap();
    let prepared = api::serving_source::Prepared {
        source_root: source.clone(),
        manifest_digest: planner::bound::manifest_digest_ref(&manifest).unwrap(),
        source_bytes: manifest["files"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["size_bytes"].as_u64().unwrap())
            .sum(),
        manifest: manifest.as_object().unwrap().clone(),
    };
    resource_policy::write(&owner,&json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"})).unwrap();
    let ranges = vec![[0, 14], [14, 28]];
    let build = planner::chain_compile::BuildRequest {
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
        route_label: "a13",
        stage_sharded: true,
        compilation_cache_key: "b7c-source-qualification".into(),
        node_ids: ids.clone(),
        managed_layer_ranges: ranges.clone(),
        layer_split: json!({"stages":ids.iter().zip(&ranges).map(|(id,r)|json!({"node_id":id,"start":r[0],"end":r[1]})).collect::<Vec<_>>()}),
    };
    let members: BTreeMap<_, _> = ids
        .iter()
        .map(|id| (id.clone(), seed.coordinator.member(id).unwrap()))
        .collect();
    let mut artifact=compiler_inputs::emit_loopback_qualification(&owner,"b7c-source",&build.deployment_id,0,&build,&prepared,
        &json!({"protocol":"mycelium.qwen_live_topology.v1","nodes":ids.iter().map(|id|json!({"node_id":id,"staging_root":owner,"runtime_backend":"mlx"})).collect::<Vec<_>>()}),
        &json!({"run_id":"b7c-source","deployment_id":build.deployment_id,"scope":"loopback_process_qualification","route_ready":false,"observations":ids.iter().zip(&nodes).map(|(id,node)|json!({"node_id":id,"process_id":node.pid()})).collect::<Vec<_>>()}),
        &json!({}), &members.values().cloned().collect::<Vec<_>>()).unwrap();
    artifact
        .produce_stage_shards(&members, app_host::now())
        .unwrap();
    let document: Value = serde_json::from_slice(
        &fs::read(artifact.root().join("source-shards.private.json")).unwrap(),
    )
    .unwrap();
    eprintln!("B7c source shards elapsed={:?}", began.elapsed());
    let objects = owner.join("objects");
    fs::create_dir(&objects).unwrap();
    let mut hashes = Vec::new();
    let mut emitted_paths = Vec::new();
    if prepare_only {
        // Unused emitted file belongs solely to this disposable producer fixture.
        // Its ledger is NOT refunded; no retained BuildOutput is claimed here.
        let unused = document["node_files"][&ids[1]][0]["path"]
            .as_str()
            .unwrap()
            .strip_prefix("deployment/")
            .unwrap();
        fs::remove_file(artifact.root().join("stage-shards").join(unused)).unwrap();
    }
    for id in ids.iter().take(count) {
        let row = &document["node_files"][id][0];
        let hash = row["content_digest"].as_str().unwrap();
        fs::hard_link(
            artifact.root().join("stage-shards").join(
                row["path"]
                    .as_str()
                    .unwrap()
                    .strip_prefix("deployment/")
                    .unwrap(),
            ),
            objects.join(hash.strip_prefix("sha256:").unwrap()),
        )
        .unwrap();
        emitted_paths.push(
            artifact.root().join("stage-shards").join(
                row["path"]
                    .as_str()
                    .unwrap()
                    .strip_prefix("deployment/")
                    .unwrap(),
            ),
        );
        hashes.push(hash.to_owned());
    }
    // Owner transfer step = PRODUCT code (api::joined_owner::source, B16): the
    // member's own consent is requested first, then the compiled bytes are
    // verified, the seed-signed offer issued and exactly that offer served.
    let shard_source = ShardSource::start(
        seed.coordinator.clone(),
        objects.clone(),
        owner.join("journal"),
        lan.clone(),
    )
    .unwrap();
    let mut offers = Vec::new();
    let mut paths = Vec::new();
    for (i, node) in nodes.iter_mut().take(count).enumerate() {
        let done = transfer(
            &shard_source,
            &seed.coordinator,
            node,
            &registrations[i],
            &format!("b7c-transfer-{i}"),
            &document["node_files"][&ids[i]][0],
            &document["node_slice_plans"][&ids[i]],
            allowance(),
        )
        .unwrap();
        // Disposable producer only: the received file now has its own verified
        // bytes. Do not claim a retained output or refund the producer ledger.
        fs::remove_file(objects.join(hashes[i].strip_prefix("sha256:").unwrap())).unwrap();
        fs::remove_file(&emitted_paths[i]).unwrap();
        paths.push(PathBuf::from(done.received["path"].as_str().unwrap()));
        eprintln!(
            "B7c received node={i} bytes={} elapsed={:?}",
            done.received["received_bytes"],
            began.elapsed()
        );
        offers.push(done.offer);
    }
    shard_source.close().unwrap();
    Fixture {
        root,
        seed,
        member_roots,
        nodes,
        registrations,
        ids,
        source,
        offers,
        paths,
        ranges,
        manifest_digest: prepared.manifest_digest.clone(),
        resource_gate,
        began,
        forwarder,
    }
}
#[allow(clippy::too_many_arguments)]
fn qualify_received(
    prepare_only: bool,
    count: usize,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    member_roots: [PathBuf; 2],
    nodes: &mut [Node; 2],
    registrations: Vec<Value>,
    source: PathBuf,
    offers: Vec<Value>,
    paths: Vec<PathBuf>,
    ranges: Vec<[usize; 2]>,
    resource_gate: std::ffi::OsString,
    began: Instant,
) {
    // Owner-issued, single-use, per-member challenges (never shared by stages).
    let challenges = native::Challenges::new();
    let ids: Vec<String> = offers
        .iter()
        .map(|o| {
            o["message"]["member"]["node_id"]
                .as_str()
                .unwrap()
                .to_owned()
        })
        .collect();
    let assigned = |i: usize| [ranges[i][0] as u64, ranges[i][1] as u64];
    let mut nonces = Vec::new();
    let reference: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let prompt: Vec<u32> =
        serde_json::from_value(reference["recorded"]["workload"]["prompt_ids"].clone()).unwrap();
    let mut proofs = Vec::new();
    for (i, node) in nodes.iter_mut().take(count).enumerate() {
        // Source emission/transport can take minutes; refresh admission here,
        // immediately before EACH native load, not only at harness launch.
        admit_native_load(&resource_gate, &format!("node={i}"));
        let nonce = challenges.issue(&ids[i], assigned(i)).unwrap();
        nonces.push(nonce.clone());
        node.send(json!({"command":"prepare_native","range":ranges[i],"tokens":prompt.len()+64,"nonce":nonce,"forbidden_source":source}));
        let signed = node.result_with(allowance());
        // Wrong assigned range is refused before the nonce is consumed.
        assert_eq!(
            native::verify_resident(&seed.coordinator, &offers[i], &nonce, [0, 28], &signed)
                .unwrap_err()
                .0,
            "joined_native_range_unassigned"
        );
        let proof = challenges
            .admit(&seed.coordinator, &offers[i], &signed)
            .unwrap()
            .proof()["message"]["proof"]
            .clone();
        assert_eq!(
            challenges
                .admit(&seed.coordinator, &offers[i], &signed)
                .unwrap_err()
                .0,
            "joined_native_nonce_unknown",
            "a consumed nonce is single-use"
        );
        assert_ne!(proof["worker_pid"], node.pid());
        assert_eq!(signed["message"]["member_pid"], node.pid());
        eprintln!(
            "B7c native node={i} pid={} rows={} elapsed={:?}",
            proof["worker_pid"],
            proof["weights"].as_object().unwrap().len(),
            began.elapsed()
        );
        let stranger = Identity::from_secret(
            "unrelated-endpoint",
            &app_host::random(32).unwrap().try_into().unwrap(),
        )
        .unwrap();
        let mut wrong = signed.clone();
        wrong["verification_key"] = stranger.public_key_record();
        wrong["signature"] = stranger.sign(&wrong["message"]).unwrap();
        assert!(
            native::verify_resident(&seed.coordinator, &offers[i], &nonce, assigned(i), &wrong)
                .is_err(),
            "wrong identity proof"
        );
        let mut wrong_nonce = nonce.clone();
        wrong_nonce.replace_range(..1, if nonce.starts_with('0') { "1" } else { "0" });
        assert_eq!(
            native::verify_resident(
                &seed.coordinator,
                &offers[i],
                &wrong_nonce,
                assigned(i),
                &signed
            )
            .unwrap_err()
            .0,
            "native_proof_binding_mismatch"
        );
        node.send(json!({"command":"discard"}));
        assert_eq!(node.refusal(), "joined_native_cleanup_required");
        assert!(paths[i].exists(), "live compute must retain shard custody");
        proofs.push(signed);
    }
    if !prepare_only {
        let mut input = prompt;
        let mut tokens = Vec::new();
        for _ in 0..stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS {
            let mut payload = Vec::new();
            for (i, node) in nodes.iter_mut().take(count).enumerate() {
                node.send(json!({"command":"native_step","tokens":if i==0 {json!(input)}else{Value::Null},"payload":STANDARD.encode(&payload)}));
                let response = node.result_with(allowance());
                payload = STANDARD
                    .decode(response["payload"].as_str().unwrap())
                    .unwrap();
            }
            let token = native::greedy(&payload).unwrap();
            tokens.push(token);
            input = vec![token];
        }
        assert_eq!(
            json!(tokens),
            json!(
                &reference["recorded"]["tokens"].as_array().unwrap()
                    [..stage_contract::native_proof::CHALLENGE_OUTPUT_TOKENS]
            )
        );
        eprintln!("B7c cross-process challenge={tokens:?} == independent reference");
    }
    // Stale native proof is tested against an ACTUAL seed generation transition,
    // not a hand-edited signed message or a fabricated enrollment record.
    let resumed = nodes[0].call(json!({"command":"resume"}));
    assert!(
        resumed["generation"].as_u64().unwrap()
            > registrations[0]["message"]["member"]["generation"]
                .as_u64()
                .unwrap()
    );
    assert_eq!(
        native::verify_resident(
            &seed.coordinator,
            &offers[0],
            &nonces[0],
            assigned(0),
            &proofs[0]
        )
        .unwrap_err()
        .0,
        "joined_shard_member_changed"
    );
    nodes[0].send(json!({"command":"native_step","tokens":[1],"payload":""}));
    assert_eq!(nodes[0].refusal(), "joined_shard_member_changed");
    eprintln!("B7c stale native proof and stale worker step refused after real resume");
    if let Some(path) = std::env::var_os("B7_NATIVE_EVIDENCE") {
        fs::write(
            path,
            serde_json::to_vec_pretty(
                &json!({"prepare_only":prepare_only,"proofs":proofs,"offers":offers}),
            )
            .unwrap(),
        )
        .unwrap();
    }
    for (i, node) in nodes.iter_mut().take(count).enumerate() {
        let start = Instant::now();
        let cleanup = node.call(json!({"command":"native_close"}));
        assert_eq!(cleanup["context_closed"], true);
        node.call(json!({"command":"discard"}));
        assert!(!paths[i].exists());
        let ledger: Value = serde_json::from_slice(
            &fs::read(member_roots[i].join("chain-runs/compiler-storage.private.json")).unwrap(),
        )
        .unwrap();
        assert_eq!(ledger["entries"], json!({}));
        let pid = proofs[i]["message"]["proof"]["worker_pid"]
            .as_i64()
            .unwrap() as i32;
        assert!(
            !std::process::Command::new("/bin/kill")
                .args(["-0", &pid.to_string()])
                .output()
                .unwrap()
                .status
                .success(),
            "worker must be reaped"
        );
        eprintln!(
            "B7c native close + inert refund node={i} elapsed={:?}",
            start.elapsed()
        );
        node.close();
    }
    for node in nodes.iter_mut().skip(count) {
        node.close();
    }
    seed.close();
    eprintln!(
        "B7c total elapsed={:?}; operator IPC challenge only, NOT M16 chat/Stop",
        began.elapsed()
    );
}
