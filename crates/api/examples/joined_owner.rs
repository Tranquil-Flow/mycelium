// SPDX-License-Identifier: AGPL-3.0-only
//! Headless joined-route OWNER process (B16, gap row 6): the owner side of
//! admit member -> transfer -> member native prepare -> route -> chat ->
//! Stop/leave, composed ONLY of product code (`api::joined_owner`, which
//! reuses the unchanged seed, joined_shard, route, m16_route and member-control
//! documents). It is the owner counterpart of `shard_member`: an operator
//! drives it with JSON-line commands on private stdin; members are reached only
//! over the signed member control channel; chat is served on a loopback HTTP
//! gateway per single-use route epoch. route_ready stays false.
//!
//! Commands: `status`, `admit {member: "<control id>@<ip:port>[,..]"}`,
//! `compile {ranges}`, `transfer`, `prepare {tokens}`, `open {epoch}` ->
//! `{gateway, bearer, qualification, worker_pids}`, `settle`, `leave {stage}`,
//! `close`. Replies: `{ok:true,result}` or `{ok:false,code}`.
//! The owner work port (`joined_owner::work::OwnerWork`, PH3b S4a) instead of
//! transfer/prepare/open: `work {tokens, worker_wall_seconds}` (start serving,
//! activate) -> workflow state with the run's ONE epoch gateway (every chat of
//! the run on it, lane EP2); `work_stop` (teardown; member links come back).
//!
//! Rehearsal limits (named seams, not product claims): the swarm seed listens
//! on loopback and a bounded forwarder exposes it on the explicit LAN
//! interface (S1 product LAN seed bind/SAN is RESERVED); the compile step is
//! the loopback-qualification producer over the operator-selected local
//! snapshot (`compiler_inputs::emit_loopback_qualification`); this process is
//! not the installed/packaged host.
use api::{
    app_host, compiler_inputs,
    joined_owner::{
        codec::NativeCodec,
        epoch::Epoch,
        link::{self, MemberLink, Refusal, Stage},
        remote::RemoteMember,
        source::ShardSource,
        work::{self, Clocks, EpochStage, OwnerWork, Plan, SettleStage, WorkMember},
    },
    joined_shard::native,
    m16_route::{Codec, RouteHost},
};
use budgets::{BudgetId as B, Entries};
use runtime::iroh_host::LanPolicy;
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    io::{BufRead, Write},
    net::{IpAddr, SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, String>;
const DEPLOYMENT: &str = "12345678-1234-5678-9234-abcdefabcdef";
const MODEL_ID: &str = "Qwen/Qwen3-0.6B";
const RESOLVED_COMMIT: &str = "c1899de289a04d12100db370d81485cdf75e47ca";

fn main() {
    if let Err(code) = run() {
        eprintln!("joined_owner_failed:{code}");
        std::process::exit(1);
    }
}
fn span(id: B) -> Result<Duration> {
    id.span(Entries::ZERO)
        .map(|s| Duration::from_millis(s.millis()))
        .map_err(|_| "budget_unavailable".into())
}
fn code(r: Refusal) -> String {
    r.0
}
struct Args {
    root: PathBuf,
    swarm: String,
    lan: Option<IpAddr>,
    allow: Vec<IpAddr>,
    control_key: PathBuf,
    snapshot: PathBuf,
    native: PathBuf,
}
fn args() -> Result<Args> {
    let usage = "usage: --state-root PATH --swarm NAME --control-key FILE --snapshot DIR --native-binary PATH [--lan IP] [--lan-allow IP]...";
    let mut it = std::env::args().skip(1);
    let (mut root, mut swarm, mut key, mut snapshot, mut native) = (None, None, None, None, None);
    let (mut lan, mut allow) = (None, Vec::new());
    while let Some(flag) = it.next() {
        let value = it.next().ok_or(usage)?;
        match flag.as_str() {
            "--state-root" => root = Some(PathBuf::from(value)),
            "--swarm" => swarm = Some(value),
            "--control-key" => key = Some(PathBuf::from(value)),
            "--snapshot" => snapshot = Some(PathBuf::from(value)),
            "--native-binary" => native = Some(PathBuf::from(value)),
            "--lan" => lan = Some(value.parse().map_err(|_| "lan_ip_invalid")?),
            "--lan-allow" => allow.push(value.parse().map_err(|_| "lan_allow_invalid")?),
            _ => return Err(usage.into()),
        }
    }
    if lan.is_none() && !allow.is_empty() {
        return Err("lan_allow_requires_lan".into());
    }
    Ok(Args {
        root: root.ok_or(usage)?,
        swarm: swarm.ok_or(usage)?,
        lan,
        allow,
        control_key: key.ok_or(usage)?,
        snapshot: snapshot.ok_or(usage)?,
        native: native.ok_or(usage)?,
    })
}
/// The owner control secret (64 hex chars, private file).
fn control_secret(path: &Path) -> Result<iroh::SecretKey> {
    let text = std::fs::read_to_string(path).map_err(|_| "control_key_unreadable")?;
    let text = text.trim();
    if text.len() != 64 || !text.is_ascii() {
        return Err("control_key_invalid".into());
    }
    let mut bytes = [0u8; 32];
    for (i, b) in bytes.iter_mut().enumerate() {
        *b = u8::from_str_radix(&text[2 * i..2 * i + 2], 16).map_err(|_| "control_key_invalid")?;
    }
    Ok(iroh::SecretKey::from_bytes(&bytes))
}
/// `<endpoint id>@<ip:port>[,<ip:port>]` -> dial address.
fn member_addr(text: &str) -> Result<iroh::EndpointAddr> {
    let (id, addrs) = text.split_once('@').ok_or("member_address_invalid")?;
    let mut addr = iroh::EndpointAddr::new(id.parse().map_err(|_| "member_address_invalid")?);
    for s in addrs.split(',') {
        addr = addr.with_ip_addr(
            s.parse::<SocketAddr>()
                .map_err(|_| "member_address_invalid")?,
        );
    }
    Ok(addr)
}
/// Rehearsal seam (S1 RESERVED): a bounded TCP forwarder exposing the loopback
/// seed (TLS end to end, CA pinned by the member) on the explicit LAN
/// interface; only allow-listed peers; every pipe owned and joined on drop.
struct Forwarder {
    address: SocketAddr,
    stop: Arc<AtomicBool>,
    thread: Option<std::thread::JoinHandle<()>>,
}
impl Forwarder {
    fn start(interface: IpAddr, policy: LanPolicy, target: SocketAddr) -> Result<Self> {
        let listener = TcpListener::bind((interface, 0)).map_err(|_| "forwarder_bind_failed")?;
        let address = listener.local_addr().map_err(|_| "forwarder_bind_failed")?;
        let stop = Arc::new(AtomicBool::new(false));
        let flag = stop.clone();
        let thread = std::thread::spawn(move || {
            let mut pipes: Vec<(TcpStream, std::thread::JoinHandle<()>)> = Vec::new();
            for incoming in listener.incoming() {
                if flag.load(Ordering::SeqCst) {
                    break;
                }
                let Ok(client) = incoming else { continue };
                if !client.peer_addr().is_ok_and(|p| policy.allows(p.ip())) {
                    continue;
                }
                // Finished pipes are joined as new ones arrive (REVIEW-laneB16
                // F3): the owned list is bounded by the live pipes.
                let (finished, live): (Vec<_>, Vec<_>) =
                    pipes.drain(..).partition(|(_, copy)| copy.is_finished());
                pipes = live;
                for (_, copy) in finished {
                    if copy.join().is_err() {
                        eprintln!("seed forwarder copy panicked");
                    }
                }
                let Ok(server) = TcpStream::connect(target) else {
                    continue;
                };
                let (Ok(c2), Ok(s2)) = (client.try_clone(), server.try_clone()) else {
                    continue;
                };
                for (mut from, mut to) in [(c2, s2), (server, client)] {
                    let Ok(held) = from.try_clone() else { continue };
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
                if let Err(e) = stream.shutdown(std::net::Shutdown::Both) {
                    eprintln!("seed forwarder close: {e}");
                }
                if copy.join().is_err() {
                    eprintln!("seed forwarder copy panicked");
                }
            }
        });
        Ok(Self {
            address,
            stop,
            thread: Some(thread),
        })
    }
}
impl Drop for Forwarder {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Err(e) = TcpStream::connect(self.address) {
            eprintln!("seed forwarder wake: {e}");
        }
        if let Some(t) = self.thread.take()
            && t.join().is_err()
        {
            eprintln!("seed forwarder panicked");
        }
    }
}
struct Member {
    link: Box<dyn MemberLink + Send>,
    /// The link's `cancel_operation` (the work port interrupts with it).
    cancel: work::Cancel,
    registration: Value,
    node_id: String,
    pid: u64,
    offer: Option<Value>,
    range: [u64; 2],
}
struct Compiled {
    document: Value,
    objects: PathBuf,
    emitted: BTreeMap<String, PathBuf>,
    source: PathBuf,
    manifest_digest: String,
}
struct Owner {
    root: PathBuf,
    seed: app_host::SeedRuntime<app_host::Unmanaged>,
    seed_address: SocketAddr,
    secret: iroh::SecretKey,
    lan: Option<(IpAddr, LanPolicy)>,
    snapshot: PathBuf,
    codec: Arc<NativeCodec>,
    members: Vec<Member>,
    compiled: Option<Compiled>,
    challenges: native::Challenges,
    admitted: Vec<native::Admitted>,
    epoch: Option<Epoch>,
    /// The owner work port while it runs (`work` .. `work_stop`), and the
    /// handed-over members' local facts (node, pid, range) in stage order.
    work: Option<OwnerWork>,
    parked: Vec<(String, u64, [u64; 2])>,
    /// The last epoch's request host, kept for verified withdrawals.
    last_host: Option<Arc<RouteHost>>,
    wall: Duration,
    // Last: the forwarder closes after everything that used it.
    _forwarder: Option<Forwarder>,
}
impl Owner {
    fn status(&self) -> Value {
        json!({"swarm_id":self.seed.coordinator.swarm_id(),"seed_address":self.seed_address,
            "control_endpoint_id":self.secret.public().to_string(),
            "members":self.members.iter().map(|m| json!({"node_id":m.node_id,"pid":m.pid})).collect::<Vec<_>>(),
            "epoch_open":self.epoch.is_some(),"route_ready":false})
    }
    fn admit(&mut self, request: &Value) -> Result<Value> {
        let addr = member_addr(request["member"].as_str().ok_or("member_address_invalid")?)?;
        let mut link =
            RemoteMember::start(self.secret.clone(), addr, self.lan.clone()).map_err(code)?;
        let status = link
            .call(json!({"command":"status"}), self.wall)
            .map_err(code)?;
        if status["joined"] != false {
            return Err("member_not_fresh".into());
        }
        let registration = link::admit(
            &mut link,
            &self.root,
            &self.seed,
            self.seed_address,
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}),
            self.wall,
        )
        .map_err(code)?;
        let node_id = registration["message"]["member"]["node_id"]
            .as_str()
            .ok_or("registration_invalid")?
            .to_owned();
        let pid = status["pid"].as_u64().ok_or("member_status_invalid")?;
        self.members.push(Member {
            cancel: Arc::new(link.canceller()),
            link: Box::new(link),
            registration,
            node_id: node_id.clone(),
            pid,
            offer: None,
            range: [0, 0],
        });
        // Stages in node-id order, like the owner producer.
        self.members.sort_by(|a, b| a.node_id.cmp(&b.node_id));
        Ok(json!({"node_id":node_id,"pid":pid}))
    }
    fn compile(&mut self, request: &Value) -> Result<Value> {
        if self.members.len() != 2 || self.compiled.is_some() {
            return Err("owner_compile_not_ready".into());
        }
        let ranges: Vec<[usize; 2]> =
            serde_json::from_value(request["ranges"].clone()).map_err(|_| "ranges_invalid")?;
        if ranges.len() != self.members.len() {
            return Err("ranges_invalid".into());
        }
        let source = self.root.join("snapshot");
        std::fs::create_dir(&source).map_err(|_| "snapshot_copy_failed")?;
        for file in ["config.json", "model.safetensors"] {
            // APFS: std copy clones (copy-on-write); the model cache is read only.
            std::fs::copy(
                self.snapshot
                    .join(file)
                    .canonicalize()
                    .map_err(|_| "snapshot_missing")?,
                source.join(file),
            )
            .map_err(|_| "snapshot_copy_failed")?;
        }
        let manifest = api::local_manifest::compile_local_model_manifest(
            &api::local_manifest::LocalModelSource {
                root: source.clone(),
                model_id: MODEL_ID.into(),
                requested_revision: "main".into(),
                resolved_commit: RESOLVED_COMMIT.into(),
                tokenizer_assets: Ok(Vec::new()),
            },
        )
        .map_err(|e| format!("manifest_failed:{e:?}"))?;
        let prepared = api::serving_source::Prepared {
            source_root: source.clone(),
            manifest_digest: planner::bound::manifest_digest_ref(&manifest)
                .map_err(|_| "manifest_invalid")?,
            source_bytes: manifest["files"]
                .as_array()
                .ok_or("manifest_invalid")?
                .iter()
                .filter_map(|r| r["size_bytes"].as_u64())
                .sum(),
            manifest: manifest.as_object().ok_or("manifest_invalid")?.clone(),
        };
        api::preferences::resource_policy::write(&self.root, &json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"})).map_err(|e| e.code.to_owned())?;
        let ids: Vec<String> = self.members.iter().map(|m| m.node_id.clone()).collect();
        let build = planner::chain_compile::BuildRequest {
            model_id: MODEL_ID.into(),
            resolved_commit: RESOLVED_COMMIT.into(),
            deployment_id: DEPLOYMENT.into(),
            route_label: "a13",
            stage_sharded: true,
            compilation_cache_key: "b16-owner-process".into(),
            node_ids: ids.clone(),
            managed_layer_ranges: ranges.clone(),
            layer_split: json!({"stages":ids.iter().zip(&ranges).map(|(id,r)|json!({"node_id":id,"start":r[0],"end":r[1]})).collect::<Vec<_>>()}),
        };
        let mut rows = BTreeMap::new();
        for id in &ids {
            rows.insert(
                id.clone(),
                self.seed
                    .coordinator
                    .member(id)
                    .map_err(|_| "member_changed")?,
            );
        }
        let mut artifact = compiler_inputs::emit_loopback_qualification(
            &self.root,
            "b16-owner",
            &build.deployment_id,
            0,
            &build,
            &prepared,
            &json!({"protocol":"mycelium.qwen_live_topology.v1","nodes":ids.iter().map(|id|json!({"node_id":id,"staging_root":self.root,"runtime_backend":"mlx"})).collect::<Vec<_>>()}),
            &json!({"run_id":"b16-owner","deployment_id":build.deployment_id,"scope":"loopback_process_qualification","route_ready":false,
                "observations":self.members.iter().map(|m|json!({"node_id":m.node_id,"process_id":m.pid})).collect::<Vec<_>>()}),
            &json!({}),
            &rows.values().cloned().collect::<Vec<_>>(),
        )
        .map_err(|e| e.0.to_owned())?;
        artifact
            .produce_stage_shards(&rows, app_host::now())
            .map_err(|e| e.0.to_owned())?;
        let document: Value = serde_json::from_slice(
            &std::fs::read(artifact.root().join("source-shards.private.json"))
                .map_err(|_| "compiled_document_unreadable")?,
        )
        .map_err(|_| "compiled_document_invalid")?;
        let objects = self.root.join("objects");
        std::fs::create_dir(&objects).map_err(|_| "objects_dir_failed")?;
        let mut emitted = BTreeMap::new();
        for (m, r) in self.members.iter_mut().zip(&ranges) {
            let row = &document["node_files"][&m.node_id][0];
            let hash = row["content_digest"]
                .as_str()
                .ok_or("compiled_document_invalid")?;
            let path = artifact.root().join("stage-shards").join(
                row["path"]
                    .as_str()
                    .and_then(|p| p.strip_prefix("deployment/"))
                    .ok_or("compiled_document_invalid")?,
            );
            std::fs::hard_link(
                &path,
                objects.join(
                    hash.strip_prefix("sha256:")
                        .ok_or("compiled_document_invalid")?,
                ),
            )
            .map_err(|_| "objects_link_failed")?;
            emitted.insert(m.node_id.clone(), path);
            m.range = [r[0] as u64, r[1] as u64];
        }
        let digest = prepared.manifest_digest.clone();
        self.compiled = Some(Compiled {
            document,
            objects,
            emitted,
            source,
            manifest_digest: digest.clone(),
        });
        Ok(json!({"stages":ids,"ranges":ranges,"manifest_digest":digest,"route_ready":false}))
    }
    fn transfer(&mut self) -> Result<Value> {
        let compiled = self.compiled.as_ref().ok_or("owner_not_compiled")?;
        let source = ShardSource::start(
            self.seed.coordinator.clone(),
            compiled.objects.clone(),
            self.root.join("journal"),
            self.lan.clone(),
        )
        .map_err(code)?;
        let mut out = Vec::new();
        for (i, m) in self.members.iter_mut().enumerate() {
            let began = Instant::now();
            let done = work::transfer_stage(
                &source,
                &self.seed.coordinator,
                m.link.as_mut(),
                &m.registration,
                &m.node_id,
                &compiled.document,
                &format!("b16-transfer-{i}"),
                self.wall,
            )
            .map_err(code)?;
            // Rehearsal producer: the member now holds its own verified bytes;
            // the owner keeps no retained output (no claim, no refund).
            let hash = compiled.document["node_files"][&m.node_id][0]["content_digest"]
                .as_str()
                .and_then(|h| h.strip_prefix("sha256:"))
                .ok_or("compiled_document_invalid")?;
            std::fs::remove_file(compiled.objects.join(hash))
                .map_err(|_| "objects_cleanup_failed")?;
            if let Some(path) = compiled.emitted.get(&m.node_id) {
                std::fs::remove_file(path).map_err(|_| "emitted_cleanup_failed")?;
            }
            out.push(
                json!({"node_id":m.node_id,"received_bytes":done.received["received_bytes"],
                "ms":began.elapsed().as_secs_f64()*1000.0}),
            );
            m.offer = Some(done.offer);
        }
        source.close().map_err(code)?;
        Ok(json!(out))
    }
    fn prepare(&mut self, request: &Value) -> Result<Value> {
        if self.epoch.is_some() {
            return Err("owner_epoch_open".into());
        }
        let tokens = request["tokens"].as_u64().ok_or("tokens_invalid")? as usize;
        let source = &self.compiled.as_ref().ok_or("owner_not_compiled")?.source;
        let began = Instant::now();
        let mut stages = Vec::new();
        for m in self.members.iter_mut() {
            stages.push(Stage {
                offer: m.offer.as_ref().ok_or("owner_not_transferred")?,
                range: m.range,
                member: m.link.as_mut(),
            });
        }
        self.admitted = link::prepare(
            &self.challenges,
            &self.seed.coordinator,
            &mut stages,
            tokens,
            source,
            self.wall,
        )
        .map_err(code)?;
        Ok(json!({"prepared":self.admitted.len(),"ms":began.elapsed().as_secs_f64()*1000.0}))
    }
    fn open(&mut self, request: &Value) -> Result<Value> {
        if self.epoch.is_some() || self.admitted.len() != self.members.len() {
            return Err("owner_epoch_not_ready".into());
        }
        let epoch = request["epoch"].as_u64().ok_or("epoch_invalid")?;
        let compiled = self.compiled.as_ref().ok_or("owner_not_compiled")?;
        let model = json!({"model_id":MODEL_ID,"resolved_commit":RESOLVED_COMMIT,
            "manifest_digest":compiled.manifest_digest});
        let admitted = std::mem::take(&mut self.admitted);
        let mut stages = Vec::new();
        for m in self.members.iter_mut() {
            stages.push(EpochStage {
                node_id: &m.node_id,
                offer: m
                    .offer
                    .as_ref()
                    .ok_or(api::compiler_inputs::Error("owner_not_transferred"))
                    .map_err(|e| e.0.to_owned())?,
                link: m.link.as_mut(),
            });
        }
        let codec: Arc<dyn Codec> = self.codec.clone();
        let opened = work::open_epoch(
            &self.seed.coordinator,
            &model,
            DEPLOYMENT,
            epoch,
            &admitted,
            &mut stages,
            &self.lan,
            codec,
            self.wall,
            None,
        )
        .map_err(code)?;
        let reply = json!({"gateway":opened.addr(),"bearer":opened.bearer(),
            "qualification":opened.qualification().map_err(|e| e.code.to_owned())?,
            "worker_pids":opened.worker_pids(),"route_ready":false});
        self.last_host = Some(opened.host().clone());
        self.epoch = Some(opened);
        Ok(reply)
    }
    /// After the epoch's request settled: collect each member's signed serve
    /// report, prove its worker gone (member-side exact-pid probe), close the
    /// epoch (threads joined, port freed).
    fn settle(&mut self) -> Result<Value> {
        let epoch = self.epoch.take().ok_or("owner_epoch_absent")?;
        if let Err(e) = work::wait_settled(&epoch) {
            self.epoch = Some(epoch);
            return Err(code(e));
        }
        let mut stages: Vec<SettleStage<'_>> = self
            .members
            .iter_mut()
            .map(|m| SettleStage {
                node_id: &m.node_id,
                link: m.link.as_mut(),
            })
            .collect();
        let (reports, acks) = work::settle_members(&epoch, &mut stages, self.wall).map_err(code)?;
        let refusals = epoch.close();
        Ok(json!({"reports":reports,"stage_ack_ms":acks,"pump_refusals":refusals}))
    }
    /// The owner work port over this run: consent + transfer + native prepare
    /// per member (start serving), then activation (route -> serve -> epoch).
    fn work(&mut self, request: &Value) -> Result<Value> {
        if self.work.is_some() || self.epoch.is_some() || self.members.is_empty() {
            return Err("owner_work_not_ready".into());
        }
        let tokens = request["tokens"].as_u64().ok_or("tokens_invalid")? as usize;
        let wall = request["worker_wall_seconds"]
            .as_u64()
            .ok_or("worker_wall_invalid")?;
        let compiled = self.compiled.as_ref().ok_or("owner_not_compiled")?;
        let plan = Plan {
            run_id: "b16-owner".into(),
            deployment_id: DEPLOYMENT.into(),
            model: json!({"model_id":MODEL_ID,"resolved_commit":RESOLVED_COMMIT,
                "manifest_digest":compiled.manifest_digest}),
            document: compiled.document.clone(),
            objects: compiled.objects.clone(),
            journal: self.root.join("journal"),
            ranges: self
                .members
                .iter()
                .map(|m| (m.node_id.clone(), m.range))
                .collect(),
            tokens,
            forbidden_source: compiled.source.clone(),
            worker_walls: self
                .members
                .iter()
                .map(|m| (m.node_id.clone(), wall))
                .collect(),
            owner_root: self.root.clone(),
        };
        let members = std::mem::take(&mut self.members);
        let mut parked = Vec::new();
        let mut handed = Vec::new();
        for m in members {
            parked.push((m.node_id.clone(), m.pid, m.range));
            handed.push(WorkMember {
                node_id: m.node_id,
                link: m.link,
                registration: m.registration,
                cancel: m.cancel,
            });
        }
        self.parked = parked;
        let origin = Instant::now();
        let codec: Arc<dyn Codec> = self.codec.clone();
        let mut port = OwnerWork::new(
            self.seed.coordinator.clone(),
            handed,
            plan,
            self.lan.clone(),
            codec,
            Clocks {
                mono: Arc::new(move || origin.elapsed().as_secs_f64()),
                wall: Arc::new(app_host::now),
            },
        )
        .map_err(code)?;
        let proposal = json!({"max_wall_seconds":wall});
        let begun = Instant::now();
        // The slices step pushes every stage shard before publish (S4aF F5).
        let pushed = owner_run::steps::slices::SlicesPort::node_slice_plans(&mut port).map_err(
            |e| match e {
                owner_run::chain::StepError::Refused(e) => e.code,
                owner_run::chain::StepError::Stopped => "managed_workflow_stopped".into(),
                owner_run::chain::StepError::Exception { class } => class,
            },
        );
        let pushed_ms = begun.elapsed().as_secs_f64() * 1000.0;
        let started = pushed.and_then(|_| {
            owner_run::steps::publish::WorkPort::start_serving(&mut port, &proposal)
                .map_err(|e| e.code_or("serving_start_failed"))
        });
        self.work = Some(port);
        started?;
        let port = self.work.as_mut().ok_or("owner_work_absent")?;
        let staged = port.wait_state(
            |s| matches!(s["state"].as_str(), Some("awaiting_activation" | "failed")),
            self.wall * 2,
        );
        if staged["state"] != "awaiting_activation" {
            return Err(staged["error_code"]
                .as_str()
                .unwrap_or("owner_work_not_staged")
                .into());
        }
        let staged_ms = begun.elapsed().as_secs_f64() * 1000.0;
        owner_run::steps::activate::ActivationPort::activate(port, &proposal)
            .map_err(|e| e.code_or("activation_failed"))?;
        let serving = port.wait_state(
            |s| matches!(s["state"].as_str(), Some("api_running" | "failed")),
            self.wall,
        );
        if serving["state"] != "api_running" {
            return Err(serving["error_code"]
                .as_str()
                .unwrap_or("owner_work_not_serving")
                .into());
        }
        Ok(
            json!({"state":serving,"staged_ms":staged_ms,"pushed_ms":pushed_ms,
            "ms":begun.elapsed().as_secs_f64()*1000.0}),
        )
    }
    /// Teardown: close the open epoch and the source; member links come back.
    fn work_stop(&mut self) -> Result<Value> {
        let mut port = self.work.take().ok_or("owner_work_absent")?;
        port.teardown()?;
        let state = port.state();
        let mut back = port.take_members();
        for (node_id, pid, range) in std::mem::take(&mut self.parked) {
            let i = back
                .iter()
                .position(|m| m.node_id == node_id)
                .ok_or("owner_work_member_lost")?;
            let m = back.remove(i);
            self.members.push(Member {
                cancel: m.cancel,
                link: m.link,
                registration: m.registration,
                node_id,
                pid,
                offer: None,
                range,
            });
        }
        Ok(state)
    }
    fn leave(&mut self, request: &Value) -> Result<Value> {
        if self.epoch.is_some() {
            return Err("owner_epoch_open".into());
        }
        let stage = request["stage"].as_u64().ok_or("stage_invalid")? as usize;
        let m = self.members.get_mut(stage).ok_or("stage_invalid")?;
        let left = m
            .link
            .call(json!({"command":"leave","mode":"smooth"}), self.wall)
            .map_err(code)?;
        let host = self.last_host.as_ref().ok_or("owner_route_absent")?;
        let withdrawn = host
            .withdraw(
                m.offer.as_ref().ok_or("owner_not_transferred")?,
                &left["withdrawal"],
            )
            .map_err(|e| e.code.to_owned())?;
        let after = api::m16_requests::Host::qualification(host.as_ref())
            .err()
            .map(|e| e.code);
        Ok(json!({"left":left,"withdrawn":withdrawn,"qualification_after":after}))
    }
    fn close(&mut self) -> Result<Value> {
        if self.work.is_some() {
            self.work_stop()?;
        }
        if let Some(epoch) = self.epoch.take() {
            epoch.close();
        }
        let mut closed = Vec::new();
        for m in self.members.iter_mut() {
            closed.push(
                m.link
                    .call(json!({"command":"close"}), self.wall)
                    .map_err(code)?,
            );
        }
        self.codec.close().map_err(|e| e.code.to_owned())?;
        Ok(json!({"members_closed":closed.len()}))
    }
}
fn run() -> Result<()> {
    let a = args()?;
    let wall = span(B::MemberChosenWall)?;
    let authority =
        app_host::Authority::<app_host::Unmanaged>::load(a.root).map_err(str::to_owned)?;
    let root = authority.root;
    let seed = app_host::start_seed::<app_host::Unmanaged>(&root, &a.swarm, None)
        .map_err(|e| format!("seed_start_failed:{e:?}"))?;
    let lan = match a.lan {
        Some(ip) => Some((
            ip,
            LanPolicy::with_explicit(a.allow).map_err(|_| "lan_allow_invalid")?,
        )),
        None => None,
    };
    let forwarder = match &lan {
        Some((ip, policy)) => Some(Forwarder::start(*ip, policy.clone(), seed.address)?),
        None => None,
    };
    let seed_address = forwarder.as_ref().map_or(seed.address, |f| f.address);
    let codec = Arc::new(
        NativeCodec::start(&a.native, &a.snapshot.join("tokenizer.json"))
            .map_err(|e| e.code.to_owned())?,
    );
    let mut owner = Owner {
        root,
        seed,
        seed_address,
        secret: control_secret(&a.control_key)?,
        lan,
        snapshot: a.snapshot,
        codec,
        members: Vec::new(),
        compiled: None,
        challenges: native::Challenges::new(),
        admitted: Vec::new(),
        epoch: None,
        work: None,
        parked: Vec::new(),
        last_host: None,
        wall,
        _forwarder: forwarder,
    };
    let limit =
        api::preferences::private_state::private_json_limit().map_err(|e| e.0.to_owned())?;
    let stdin = std::io::stdin();
    let mut out = std::io::stdout();
    let mut closing = false;
    let mut input = stdin.lock();
    loop {
        // Bounded before buffering (REVIEW-laneB16 F3): at most limit+1 bytes
        // of one operator line are ever held.
        let mut line = Vec::new();
        let read = std::io::Read::take(std::io::Read::by_ref(&mut input), limit + 1)
            .read_until(b'\n', &mut line)
            .map_err(|_| "operator_read_failed")?;
        if read == 0 {
            break;
        }
        if line.len() as u64 > limit {
            return Err("operator_frame_invalid".into());
        }
        let request: Value = serde_json::from_slice(&line).map_err(|_| "operator_json_invalid")?;
        let command = request["command"].as_str().unwrap_or_default();
        let reply = match command {
            "status" => Ok(owner.status()),
            "admit" => owner.admit(&request),
            "compile" => owner.compile(&request),
            "transfer" => owner.transfer(),
            "prepare" => owner.prepare(&request),
            "open" => owner.open(&request),
            "settle" => owner.settle(),
            "work" => owner.work(&request),
            "work_stop" => owner.work_stop(),
            "leave" => owner.leave(&request),
            "close" => {
                closing = true;
                owner.close()
            }
            _ => Err("operator_command_unknown".into()),
        };
        let reply = match reply {
            Ok(result) => json!({"ok":true,"result":result}),
            Err(code) => json!({"ok":false,"code":code}),
        };
        writeln!(out, "{reply}").map_err(|_| "operator_write_failed")?;
        out.flush().map_err(|_| "operator_write_failed")?;
        if closing {
            break;
        }
    }
    // EOF without close: drop closes the epoch, member links, codec, seed.
    owner.seed.close();
    Ok(())
}
