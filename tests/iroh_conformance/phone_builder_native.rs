// SPDX-License-Identifier: AGPL-3.0-only
// Software-only builder witness adapted from 2fd3cff node_real_composition.rs.
// Included beneath node::tests in a separate export of native 359ee19.
// Actual serve_control, signed observations, verified tiny Qwen weights; no devices.
use super::*;
use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};
use crate::transport::{Reachability, parse_endpoint_id};

struct ChildGuard(std::process::Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        if self.0.try_wait().ok().flatten().is_some() { return; }
        unsafe { libc::kill(self.0.id() as i32, libc::SIGTERM); }
        for _ in 0..70 {
            if self.0.try_wait().ok().flatten().is_some() { return; }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = self.0.kill(); let _ = self.0.wait();
    }
}

#[tokio::test(flavor="multi_thread", worker_threads=4)]
async fn builder_review_native_configure() {
    let phone = NodeTransport::bind(&[111;32], Reachability::LocalOnly).await.unwrap();
    let identity = Arc::new(Identity::from_secret(&phone.endpoint_id(), &[114;32]).unwrap());
    let root=std::env::var("BUILDER_REVIEW_ROOT").unwrap();
    let mut child=ChildGuard(Command::new(std::env::var("MYCELIUM_TEST_PYTHON").unwrap())
        .arg(format!("{root}/tests/iroh_conformance/phone_builder_native_client.py"))
        .env_remove("PYTHONPATH").env_remove("PYTHONHOME")
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit()).spawn().unwrap());
    let stdout=child.0.stdout.take().unwrap();
    let (tx,incoming)=std::sync::mpsc::channel();
    let reader=std::thread::spawn(move || {
        for line in BufReader::new(stdout).lines() { if tx.send(line.unwrap()).is_err() { break; } }
    });
    let mut input=child.0.stdin.take().unwrap();
    writeln!(input,"{}",json!({"endpoint_id":phone.endpoint_id(),"key":identity.public_key_record(),"native_pid":std::process::id()})).unwrap();
    input.flush().unwrap();
    let line=incoming.recv_timeout(Duration::from_secs(40)).unwrap();
    let setup:Value=serde_json::from_str(&line).unwrap();
    assert_eq!(setup["action"],"apply");
    let m=&setup["manifest"]["message"];
    let model=std::path::Path::new(setup["model"].as_str().unwrap());
    let cfg:Value=serde_json::from_slice(&std::fs::read(model.join("config.json")).unwrap()).unwrap();
    let blob=std::fs::read(model.join("model.safetensors")).unwrap();
    let stage=crate::qwen2::Qwen2Stage::load(crate::qwen2::Qwen2Config::from_hf_config(&cfg).unwrap(),
        m["start_layer"].as_u64().unwrap() as usize,m["end_layer_exclusive"].as_u64().unwrap() as usize,
        &mut crate::qwen2::SafetensorsBytes::new(&blob).unwrap()).unwrap();
    let golden=crate::stage_manifest::Golden { seed:m["golden"]["seed"].as_u64().unwrap(),
        seq:m["golden"]["seq"].as_u64().unwrap() as usize,output_digest:m["golden"]["output_digest"].as_str().unwrap().into() };
    let runtime=Qwen2Runtime::new_verified(setup["record"]["assignment_id"].as_str().unwrap(),m["stage_id"].as_str().unwrap(),stage,&golden).unwrap();
    let shared=lifecycle_shared(Box::new(runtime));
    {
        let mut s=shared.lock().unwrap();
        // Actual production issuance/applied acknowledgement happened in the
        // disposable Python seed. This fixture installs that exact applied row;
        // it does NOT claim to test phone acquisition/onboarding or seed HTTP.
        s.membership=Some(ActiveMembership {
            cfg:MemberConfig { swarm_id:"synthetic-swarm".into(),node_id:"node-1".into(),seed_node_id:"seed".into(),
                incarnation:setup["member"]["incarnation"].as_str().unwrap().into(),software_version:"fixture".into(),
                peer_class:"android_native_qwen2_iroh".into(),runtime_capability:json!({}),ttl_seconds:300.0 },
            generation:setup["member"]["generation"].as_u64().unwrap(),seed_url:"http://127.0.0.1:1".into(),pinned:json!({}),needs_resume:false,pending_resume:None });
        let peer=setup["record"]["peer_endpoint_id"].as_str().unwrap();
        s.route=Some(AppliedRoute { assignment:RouteAssignment {
            assignment_id:setup["record"]["assignment_id"].as_str().unwrap().into(),stage_pack_digest:m["pack_digest"].as_str().unwrap().into(),
            peer_endpoint_id:peer.into(),route_generation:1,forward_to:false,expires_at:now_s()+300.0,controller_endpoint_ids:vec![setup["controller"].as_str().unwrap().into()] },
            peer:parse_endpoint_id(peer).unwrap() });
    }
    phone.configure_controllers([parse_endpoint_id(setup["controller"].as_str().unwrap()).unwrap()]).await;
    let (tx,rx)=mpsc::channel(8);
    let accept=phone.spawn_accept_dispatch(tx);
    let inbox=tokio::spawn(serve_inbox(rx,shared.clone(),phone.clone()));
    let (ctx,crx)=mpsc::channel(8);
    phone.set_control_handler(Some(ctx)).await;
    let control=tokio::spawn(serve_control(crx,shared.clone(),phone.clone(),identity,setup["member"]["incarnation"].as_str().unwrap().into()));
    writeln!(input,"{}",json!({"phone":phone.addr(),"runtime_build_digest":runtime_build_digest()})).unwrap(); input.flush().unwrap();
    let result:Value=serde_json::from_str(&incoming.recv_timeout(Duration::from_secs(120)).unwrap()).unwrap();
    drop(input);
    let status=child.0.wait().unwrap();
    reader.join().unwrap();
    control.abort();inbox.abort();accept.abort();phone.close().await;
    assert_eq!(result["ok"],true,"{result}");
    assert!(status.success());
    let s=shared.lock().unwrap();
    assert!(s.control.is_none());
    assert_eq!(s.stage.as_ref().unwrap().replay_result_count(),Some(0));
}
