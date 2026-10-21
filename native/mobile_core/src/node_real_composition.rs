// SPDX-License-Identifier: AGPL-3.0-only
// Software-only composition witness. Synthetic 64-wide CPU fixture; no model assets/devices.
// Included under node::tests to exercise the real private node service, not a mock ControlEnv.
use super::*;
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use crate::transport::{Reachability, parse_endpoint_id};

struct ChildGuard(std::process::Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_some() { return; }
        // Let Python unwind its finally block and reap its owned sidecar.
        unsafe { libc::kill(self.0.id() as i32, libc::SIGTERM); }
        for _ in 0..70 {
            if self.0.try_wait().ok().flatten().is_some() { return; }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.0.kill(); let _ = self.0.wait();
    }
}

/// How the golden router frames are addressed for one composition: the
/// deployment they name and the node ids of its three placements.
#[derive(Clone)]
struct FrameSpec { deployment_id: String, topology_version: u64, nodes: Vec<(String, String)> }

impl Default for FrameSpec {
    fn default() -> Self { Self { deployment_id: "deployment-1".into(), topology_version: 1, nodes: Vec::new() } }
}

/// The applied stage of the qualification composition: the exact model bytes
/// and seed-signed manifest the disposable Python seed issued and recorded.
struct AppliedFixture { model: std::path::PathBuf, manifest: Value, assignment_id: String }

impl AppliedFixture {
    /// Load, golden-verify and return the stage runtime, as the node would
    /// after acquiring the manifest's objects. Acquisition itself, onboarding
    /// and seed HTTP are NOT exercised here.
    fn load(&self) -> Qwen2Runtime {
        let m = &self.manifest;
        let cfg: Value = serde_json::from_slice(&std::fs::read(self.model.join("config.json")).unwrap()).unwrap();
        let blob = std::fs::read(self.model.join("model.safetensors")).unwrap();
        let stage = crate::qwen2::Qwen2Stage::load(crate::qwen2::Qwen2Config::from_hf_config(&cfg).unwrap(),
            m["start_layer"].as_u64().unwrap() as usize, m["end_layer_exclusive"].as_u64().unwrap() as usize,
            &mut crate::qwen2::SafetensorsBytes::new(&blob).unwrap()).unwrap();
        let golden = crate::stage_manifest::Golden { seed: m["golden"]["seed"].as_u64().unwrap(),
            seq: m["golden"]["seq"].as_u64().unwrap() as usize, output_digest: m["golden"]["output_digest"].as_str().unwrap().into() };
        Qwen2Runtime::new_verified(&self.assignment_id, m["stage_id"].as_str().unwrap(), stage, &golden).unwrap()
    }
}

fn composition_frame(name: &str, spec: &FrameSpec) -> Vec<u8> {
    // Adapt only the synthetic fixture to the real tiny Qwen runtime's width.
    // Original golden files stay untouched; this is not model/artifact evidence.
    let mut frame = wire::decode_frame(&golden(name)).unwrap();
    if !spec.nodes.is_empty() {
        // Readdress the same golden frame to this composition's node ids.
        let mut text = frame.body.to_string();
        for (from, to) in &spec.nodes { text = text.replace(&format!("\"{from}\""), &format!("\"{to}\"")); }
        frame.body = serde_json::from_str(&text).unwrap();
    }
    if frame.message_type == "ManifestLocked" {
        frame.body["build"]["graph"]["hidden_size"] = json!(64);
        frame.body["build"]["graph"]["topology_version"] = json!(spec.topology_version);
        frame.body["build"]["graph"]["deployment_id"] = json!(spec.deployment_id);
        frame.body["manifest"]["topology_version"] = json!(spec.topology_version);
        frame.body["manifest"]["deployment_id"] = json!(spec.deployment_id);
    } else if frame.message_type == "HopHeader" {
        frame.body["topology_version"] = json!(spec.topology_version);
        let (sequence, _, _) = wire::Activation::decode(&frame.payload).unwrap().to_flat_f32().unwrap();
        frame.payload = wire::Activation::from_flat_f32(sequence,64,&vec![0.125;sequence*64]).encode().unwrap();
    }
    wire::encode_frame(&frame.message_type,&frame.body,&frame.payload).unwrap()
}

async fn composition(mode: &str) {
    let phone = NodeTransport::bind(&[111;32], Reachability::LocalOnly).await.unwrap();
    let entry = NodeTransport::bind(&[112;32], Reachability::LocalOnly).await.unwrap();
    let last = NodeTransport::bind(&[113;32], Reachability::LocalOnly).await.unwrap();
    let identity = Arc::new(Identity::from_secret(&phone.endpoint_id(), &[114;32]).unwrap());
    let shared = lifecycle_shared(Box::new(verified_synthetic_stage()));
    let qualification = mode == "qualification";
    if qualification {
        // Nothing is applied until the Python side has issued the assignment
        // through its disposable seed: the node starts with no stage, no
        // route and a load count of zero, and counts its own installs.
        let mut s = shared.lock().unwrap();
        s.stage = None;
        s.stage_node = None;
        s.stage_load_generation = 0;
    } else {
        let mut s = shared.lock().unwrap();
        s.membership = Some(ActiveMembership {
            cfg: MemberConfig { swarm_id:"synthetic".into(), node_id:"node-phone".into(),
                seed_node_id:"synthetic-seed".into(), incarnation:"inc-phone".into(),
                software_version:"composition-test".into(), peer_class:"android_native_qwen2_iroh".into(),
                runtime_capability:json!({}), ttl_seconds:300.0 }, generation:1,
            seed_url:"http://127.0.0.1:1".into(), pinned:json!({}), needs_resume:false, pending_resume:None,
        });
        // Explicit synthetic already-applied seed fixture; no onboarding/acquisition claim.
        s.route = Some(AppliedRoute { assignment: RouteAssignment {
            assignment_id:"assignment-1".into(), stage_pack_digest:format!("sha256:{}", "a".repeat(64)),
            peer_endpoint_id:entry.endpoint_id(), route_generation:1, forward_to:false,
            expires_at:now_s()+300.0, controller_endpoint_ids:Vec::new(),
        }, peer:parse_endpoint_id(&entry.endpoint_id()).unwrap() });
    }
    entry.configure_peer(parse_endpoint_id(&phone.endpoint_id()).unwrap(),1).await;
    last.configure_peer(parse_endpoint_id(&phone.endpoint_id()).unwrap(),1).await;
    let (etx, _erx) = mpsc::channel(8);
    let (ltx, mut lrx) = mpsc::channel(8);
    let ea = entry.spawn_accept(etx);
    let la = last.spawn_accept(ltx);
    let (tx, rx) = mpsc::channel(8);
    let pa = phone.spawn_accept_dispatch(tx);
    let inbox = tokio::spawn(serve_inbox(rx, shared.clone(), phone.clone()));
    let (ctx, crx) = mpsc::channel(8);
    phone.set_control_handler(Some(ctx)).await;
    let control = tokio::spawn(serve_control(crx, shared.clone(), phone.clone(), identity.clone(), "inc-phone".into()));
    let root = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).parent().unwrap().parent().unwrap();
    let binary = std::path::PathBuf::from(std::env::var("CARGO_TARGET_DIR").expect("source-aware shared target required")).join("debug/mycelium-iroh-sidecar");
    let mut child = ChildGuard(Command::new(std::env::var("MYCELIUM_TEST_PYTHON").expect("canonical test Python required"))
        .arg(root.join("tests/iroh_conformance/real_phone_composition_client.py"))
        .arg(binary).arg(mode).env("PYTHONPATH", root).env_remove("PYTHONHOME")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn().unwrap());
    let stdout = child.0.stdout.take().unwrap();
    let (lines, incoming) = std::sync::mpsc::channel();
    let reader = std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() {
            if lines.send(line.unwrap()).is_err() { break; }
        }
    });
    let mut input = child.0.stdin.take().unwrap();
    // The qualification case runs the production builder between phases.
    let phase = Duration::from_secs(if qualification { 300 } else { 40 });
    let read = || -> Value {
        let line = incoming.recv_timeout(phase).expect("bounded Python phase");
        serde_json::from_str(&line).unwrap_or_else(|_| panic!("non-protocol stdout: {line}"))
    };
    let ready = read();
    phone.configure_controllers([parse_endpoint_id(ready["controller"].as_str().unwrap()).unwrap()]).await;
    writeln!(input,"{}",json!({"phone":phone.addr(), "entry":entry.addr(), "final":last.addr(),
        "key":identity.public_key_record(), "native_monotonic_s":monotonic_s(),
        "runtime_build_digest":runtime_build_digest(),
        "path_manifest":wire::decode_frame(&composition_frame("01-manifest-locked.bin", &FrameSpec::default())).unwrap().body["manifest"]})).unwrap();
    input.flush().unwrap();
    let mut first_deadline = None;
    let mut spec = FrameSpec::default();
    let mut applied: Option<AppliedFixture> = None;
    let mut drives = 0u64;
    let result = loop {
        let request = read();
        let response = match request["action"].as_str() {
            Some("apply") => {
                // The seed-issued assignment the member acknowledged as applied.
                // Membership and the applied route row are installed as the
                // route sync would; the stage goes through the node's own
                // install path, which is what counts the load.
                assert!(qualification && applied.is_none());
                let fixture = AppliedFixture { model: request["model"].as_str().unwrap().into(),
                    manifest: request["manifest"]["message"].clone(),
                    assignment_id: request["record"]["assignment_id"].as_str().unwrap().into() };
                let runtime = fixture.load();
                let load_generation = {
                    let mut s = shared.lock().unwrap();
                    s.membership = Some(ActiveMembership {
                        cfg: MemberConfig { swarm_id:"synthetic-swarm".into(), node_id:request["node_id"].as_str().unwrap().into(),
                            seed_node_id:"seed".into(), incarnation:request["member"]["incarnation"].as_str().unwrap().into(),
                            software_version:"composition-test".into(), peer_class:"android_native_qwen2_iroh".into(),
                            runtime_capability:json!({}), ttl_seconds:300.0 },
                        generation:request["member"]["generation"].as_u64().unwrap(),
                        seed_url:"http://127.0.0.1:1".into(), pinned:json!({}), needs_resume:false, pending_resume:None,
                    });
                    s.route = Some(AppliedRoute { assignment: RouteAssignment {
                        assignment_id:fixture.assignment_id.clone(),
                        stage_pack_digest:fixture.manifest["pack_digest"].as_str().unwrap().into(),
                        peer_endpoint_id:entry.endpoint_id(), route_generation:request["record"]["route_generation"].as_u64().unwrap(),
                        forward_to:false, expires_at:now_s()+300.0, controller_endpoint_ids:Vec::new(),
                    }, peer:parse_endpoint_id(&entry.endpoint_id()).unwrap() });
                    s.install_stage(Box::new(runtime))
                };
                spec = FrameSpec { deployment_id: request["deployment_id"].as_str().unwrap().into(),
                    topology_version: request["topology_version"].as_u64().unwrap_or(1),
                    nodes: vec![("node-mac-entry".into(), request["entry_node_id"].as_str().unwrap().into()),
                        ("node-phone".into(), request["node_id"].as_str().unwrap().into()),
                        ("node-mac-final".into(), request["final_node_id"].as_str().unwrap().into())] };
                applied = Some(fixture);
                json!({"load_generation":load_generation})
            },
            Some("frames") => {
                // The emitted plan's own graph identity, known only after the build.
                spec.topology_version = request["topology_version"].as_u64().unwrap();
                json!({"topology_version":spec.topology_version})
            },
            Some("reload") => {
                // The node loads its applied assignment again: same bytes, same
                // assignment, a new runtime and therefore a new load.
                let runtime = applied.as_ref().expect("applied before reload").load();
                let mut s = shared.lock().unwrap();
                assert!(s.control.is_none(), "reload between control sessions");
                json!({"load_generation":s.install_stage(Box::new(runtime))})
            },
            Some("replace") => {
                // The same install while a control session is RUNNING: the node
                // ends that session with the load it was configured for.
                let runtime = applied.as_ref().expect("applied before replace").load();
                let (load_generation, was_running, control_gone) = {
                    let mut s = shared.lock().unwrap();
                    let was_running = s.control.as_ref().is_some_and(|c| c.state() == "RUNNING");
                    let load_generation = s.install_stage(Box::new(runtime));
                    (load_generation, was_running, s.control.is_none())
                };
                reconcile_control(&shared, &phone).await;
                // A hop the old session's entry peer sends now is not executed.
                let before = shared.lock().unwrap().status.frames_executed;
                let refused = entry.send(phone.addr(),1,&composition_frame("04-hop-decode-in.bin", &spec)).await.is_err();
                json!({"load_generation":load_generation,"was_running":was_running,"control_gone":control_gone,
                    "hop_refused":refused,"executed_after":shared.lock().unwrap().status.frames_executed - before})
            },
            Some("drive") => {
                entry.send(phone.addr(),1,&composition_frame("01-manifest-locked.bin", &spec)).await.unwrap();
                entry.send(phone.addr(),1,&composition_frame("02-hop-prefill-in.bin", &spec)).await.unwrap();
                let out = tokio::time::timeout(Duration::from_secs(8),lrx.recv()).await.unwrap().unwrap();
                assert_eq!(out.frame.message_type,"HopHeader");
                assert_eq!(out.from_node,phone.endpoint_id());
                drives += 1;
                assert_eq!(shared.lock().unwrap().status.frames_executed,drives);
                // A duplicate frame must not execute or republish.
                entry.send(phone.addr(),1,&composition_frame("02-hop-prefill-in.bin", &spec)).await.unwrap();
                assert!(tokio::time::timeout(Duration::from_millis(80),lrx.recv()).await.is_err());
                json!({"executed":1,"forwarded":true,"duplicate_suppressed":true})
            },
            Some("deadline") => {
                let s = shared.lock().unwrap();
                let deadline = *s.deadlines.get("request-phone-golden").expect("native mapped deadline");
                if let Some(first) = first_deadline { assert!(deadline <= first,"repeat bind renewed native deadline"); }
                else { first_deadline = Some(deadline); }
                let remaining = deadline.saturating_duration_since(std::time::Instant::now()).as_millis();
                assert!(remaining > 0 && remaining <= 3000, "host clock was compared to native wall time: {remaining}");
                json!({"remaining_ms":remaining,"native_monotonic_s":monotonic_s()})
            },
            Some("expired") => {
                let deadline = first_deadline.expect("first native deadline observed");
                tokio::time::sleep_until(tokio::time::Instant::from_std(deadline + Duration::from_millis(30))).await;
                assert!(shared.lock().unwrap().request_blocked("request-phone-golden"));
                let before = shared.lock().unwrap().status.frames_executed;
                let err = entry.send(phone.addr(),1,&composition_frame("04-hop-decode-in.bin", &spec)).await.unwrap_err();
                assert_eq!(err.0,"request_cancelled_or_expired");
                assert_eq!(shared.lock().unwrap().status.frames_executed,before);
                assert!(tokio::time::timeout(Duration::from_millis(80),lrx.recv()).await.is_err());
                json!({"expired":true,"no_execution_or_publication":true})
            },
            Some("stopped") => {
                let s = shared.lock().unwrap();
                json!({"control_gone":s.control.is_none(),"deadlines":s.deadlines.len(),
                    "replay_results":s.stage.as_ref().unwrap().replay_result_count(),
                    "active_work":s.inflight.values().sum::<usize>(),"revision":s.stage_revision,
                    "live_paths":s.stage_node.as_ref().unwrap().live_paths_of("request-phone-golden").len()})
            },
            Some("done") => break request,
            _ => panic!("unknown driver request: {request}"),
        };
        writeln!(input,"{response}").unwrap(); input.flush().unwrap();
    };
    drop(input);
    let status = child.0.wait().unwrap();
    reader.join().unwrap();
    control.abort(); inbox.abort(); pa.abort(); ea.abort(); la.abort();
    phone.close().await; entry.close().await; last.close().await;
    assert!(status.success() && result["ok"] == true,"{mode}: {result}");
}

#[tokio::test(flavor="multi_thread", worker_threads=4)]
async fn real_phone_composition_startup_bound_cancel() { composition("startup").await; }
#[tokio::test(flavor="multi_thread", worker_threads=4)]
async fn real_phone_composition_host_monotonic_deadline() { composition("deadline").await; }
// The build receipt this case validates names the source commit the test binary
// was compiled from, which only the composition runner compiles in. A plain
// `cargo test` therefore reports it as ignored instead of running it without
// that identity.
#[tokio::test(flavor="multi_thread", worker_threads=4)]
#[ignore = "needs the source-bound build identity: run tests/iroh_conformance/run_real_phone_composition.py"]
async fn real_phone_composition_native_observed_load_qualifies_and_stale_or_tampered_records_refuse() { composition("qualification").await; }
#[tokio::test(flavor="multi_thread", worker_threads=4)]
async fn real_phone_composition_stop_releases_runtime() { composition("stop").await; }
