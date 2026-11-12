// SPDX-License-Identifier: AGPL-3.0-only
//! EP2H real NativeCodec IPC under the public Work Stop chain. No model/GPU.
use super::*;
use tokenizer_helper::HelperFixture;

fn held_helper(mode: &str) {
    let mut helper = HelperFixture::new(mode);
    let mut swarm = swarm(2);
    let plan = swarm.serving_plan();
    let (mut work, members) = swarm.serving_with_codec(plan, stage_scripts(), helper.codec.clone());
    activate(&mut work);
    let gateway = work.gateway().unwrap();
    let writing = mode == "write";
    let submit = std::thread::spawn(move || {
        let prompt = if writing { "p".repeat(api::requests::MAX_REQUEST_BODY / 2) } else { "p".into() };
        let doc = json!({"protocol":"mycelium.request_gateway.v2","prompt":prompt,"max_new_tokens":CHAT_TOKENS,
            "workload_profile_id":"interactive","qos_class":"interactive","qualification":gateway["qualification"]});
        sse_client::http(gateway_addr(&gateway), "POST", "/v1/inference", &bearer(&gateway), &doc.to_string())
    });
    assert!(helper.entered(), "real helper consumed request/partial write under callback lock");
    let pid = helper.pid();
    let allowance = stop_allowance(60, 2);
    let began = Instant::now();
    let (done, thread) = stop(work);
    let result = done.recv_timeout(allowance + span(B::OwnerPoll) * 2);
    let elapsed = began.elapsed();
    // Explicit release and join BEFORE assertion. A rescue is not a GREEN.
    helper.release();
    let mut work = thread.join().unwrap();
    let state = work.state();
    if let Err(e) = submit.join() { eprintln!("submit ended after gateway close: {e:?}"); }
    work.teardown().unwrap();
    drop(work.take_members());
    helper.codec.close().unwrap();
    let gone = rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()).is_err();
    eprintln!("EP2H_NATIVE_HELPER mode={mode} pid={pid} before_release={} elapsed={elapsed:?} allowance={allowance:?} cleanup={} gone={gone}", result.is_ok(), state["cleanup_error"]);
    assert!(result.is_ok(), "real NativeCodec {mode} IPC hid whole Stop beyond its first deadline");
    assert!(gone, "owned helper must be reaped after explicit cleanup");
    assert!(members.iter().all(|m| m.0.lock().unwrap().discard_at.is_some()), "healthy siblings cleaned");
}
#[test]
fn ep2h_native_helper_read_cannot_hide_work_stop() { held_helper("read"); }
#[test]
fn ep2h_native_helper_partial_write_cannot_hide_work_stop() { held_helper("write"); }
#[test]
fn ep2h_native_helper_normal_protocol_positive_control() {
    let helper = HelperFixture::new("normal");
    assert_eq!(helper.codec.encode("actual JSON encode request").unwrap(), [0,1,2,3]);
    assert_eq!(helper.codec.decode(11).unwrap(), "<11>");
    let pid = helper.pid();
    helper.codec.close().unwrap();
    assert!(rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()).is_err());
}
