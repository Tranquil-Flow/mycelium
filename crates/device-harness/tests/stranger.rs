// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use budgets::Span;
use device_harness::{
    Fault, FaultKind, Host, Result,
    admission::*,
    effects::Clock,
    http::{Chat, Method},
    observe::{Cpu, Mailbox, Metrics, Observer, SnapshotStore},
    preflight::*,
    stranger::*,
};
use serde_json::{Value, json};
#[test]
fn preflight_checks_both_homes_and_retains_exact_host_specific_reason() {
    for (host, name) in [(Host::Owner, "owner"), (Host::Member, "laptop")] {
        let mut io = Fake {
            stray_host: Some(host),
            ..Fake::default()
        };
        let mut store = Store::default();
        let e = run(
            &mut io,
            &mut store,
            &mut Tick::default(),
            candidate(),
            limits(),
        );
        assert_eq!(e.outcome, "incomplete");
        assert_eq!(
            e.first_failure.as_deref(),
            Some("preflight_target_version_stray")
        );
        assert_eq!(e.admissions.len(), 2);
        assert_eq!(e.preflight_errors.len(), 1);
        assert_eq!(
            e.preflight_errors[0]["message"],
            format!("preflight_target_version_stray:{name}")
        );
        assert_eq!(e.preflight_errors[0]["kind"], "admission");
        assert!(io.actions.contains(&"preflight:Owner".into()));
        assert!(io.actions.contains(&"preflight:Member".into()));
        assert!(!io.actions.iter().any(|a| a.starts_with("install:")));
    }
}
fn candidate() -> Candidate {
    Candidate {
        version: "0.2.0".into(),
        source_sha: "c".repeat(40),
        zip_sha256: "a".repeat(64),
        exe_sha256: "b".repeat(64),
    }
}
fn limits() -> Limits {
    Limits {
        total: Span::from_millis(3600000),
        action: Span::from_millis(20000),
        pair: Span::from_millis(300000),
        serving: Span::from_millis(900000),
        configuration: Span::from_millis(20000),
        poll: Span::from_millis(1000),
        relay_window: Span::from_millis(1800000),
        member_wall: Span::from_millis(600000),
        pair_window: Span::from_millis(600000),
    }
}
fn admission() -> Preflight {
    Preflight {
        power: Power {
            source: PowerSource::Ac,
            percent: None,
        },
        network: NetworkClass::Metered,
        consent: NetworkConsent::MeteredAllowed,
        free_bytes: 99 * 1024 * 1024 * 1024,
        memory_level: 1,
        current: Some("0.1.3".into()),
        previous: Some("0.2.0".into()),
        program_directories: ["0.1.3".into(), "0.2.0".into()].into(),
        busy: vec![],
        owned_marker: None,
        attempt_spent: false,
        state_readable: true,
    }
}
fn identity() -> FreshIdentity {
    let p = ProcessIdentity {
        pid: 1234,
        birth: "fixture birth".into(),
        executable: "/fixture/app/exe".into(),
    };
    FreshIdentity {
        launch_epoch_ms: 1000,
        control_modified_ms: 1001,
        expected: p.clone(),
        listener: Some(p),
        control_port: 1234,
        status_port: 1234,
        status_http: 200,
        status_observed_ms: 1002,
        expected_exe_sha256: candidate().exe_sha256.clone(),
        observed_exe_sha256: candidate().exe_sha256,
    }
}
fn status() -> Value {
    json!({"run_model":{"selected":"a9-route","locked":false},"fast_mode":{"enabled":true,"locked":false},"acquisition":{"state":"closed","downloaded_bytes":999604126,"total_bytes":999604126},"authority":{"membership_state":"current","lease_state":"fresh","capability_state":"self_report_accepted"},"owner_run":{"run_id":"r1","state":"serving","route_ready":true,"participants":[{},{}]},"join_flow":{"standing_disclosure":{"digest":"PRIVATE-DIGEST"},"pairing_windows":[{"window_id":"fixture","state":"claimed","join_link":"PRIVATE-LINK","pending":{"request_digest":"PRIVATE-REQUEST"}}]}})
}
#[derive(Default)]
struct Tick(u64);
impl Clock for Tick {
    fn now(&self) -> u64 {
        self.0
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.0 += s.millis();
        Ok(())
    }
}
#[derive(Default)]
struct Store(Option<Value>);
impl SnapshotStore for Store {
    fn exists(&mut self) -> Result<bool> {
        Ok(self.0.is_some())
    }
    fn create_first(&mut self, v: &Value) -> Result<()> {
        assert!(self.0.is_none());
        self.0 = Some(v.clone());
        Ok(())
    }
}
#[derive(Default)]
struct Fake {
    actions: Vec<String>,
    bodies: Vec<(String, Option<Value>)>,
    operator_stop: bool,
    aside: [bool; 2],
    inspections: [usize; 2],
    fail_at: Option<&'static str>,
    low_battery: bool,
    stray_host: Option<Host>,
    network_mismatch: bool,
    unmetered_consent: bool,
    runtime_stop_after_demo: bool,
    demo_started: bool,
    missing_cache: bool,
    wrong_model: bool,
    selected: bool,
    chat_failure: bool,
    stale_launch: bool,
    join_polls: usize,
    member_reads: usize,
}
fn index(h: Host) -> usize {
    if h == Host::Owner { 0 } else { 1 }
}
impl Observer for Fake {
    fn status(&mut self, h: Host) -> Result<(u16, Value)> {
        self.actions.push(format!("snapshot:{h:?}"));
        Ok((200, status()))
    }
    fn metrics(&mut self, _: Host) -> Result<Metrics> {
        Ok(Metrics {
            load: [1., 2., 3.],
            top_cpu: (1..=5)
                .map(|pid| Cpu {
                    pid,
                    cpu_percent: 1.,
                    command: "worker".into(),
                })
                .collect(),
        })
    }
    fn mailbox(&mut self, h: Host) -> Result<Mailbox> {
        Ok(Mailbox {
            database_bytes: Some(if h == Host::Owner { 1024 } else { 0 }),
            storage_bytes: 1024,
            local_files: 1,
            state: "local_db_observed".into(),
        })
    }
}
impl Adapter for Fake {
    fn battery_percentages(&self) -> [Option<u8>; 2] {
        [
            Some(61),
            Some(if self.runtime_stop_after_demo && self.demo_started {
                24
            } else {
                61
            }),
        ]
    }
    fn runtime_guard(&mut self) -> Result<()> {
        if self.operator_stop && self.demo_started {
            return Err(Fault::new("operator_stop", FaultKind::Observation));
        }
        if self.runtime_stop_after_demo && self.demo_started {
            return Err(Fault::new("battery_floor", FaultKind::Admission));
        }
        Ok(())
    }
    fn perform(&mut self, e: Effect, _bound: Span) -> Result<Reply> {
        match e {
            Effect::OwnerPower => {
                self.actions.push("owner_power".into());
                Ok(Reply::Power(if self.low_battery {
                    Power {
                        source: PowerSource::Battery,
                        percent: Some(34),
                    }
                } else {
                    admission().power
                }))
            }
            Effect::VerifyPackage => {
                self.actions.push("verify".into());
                Ok(Reply::Package(candidate()))
            }
            Effect::Preflight(h) => {
                self.actions.push(format!("preflight:{h:?}"));
                let mut p = admission();
                if self.stray_host == Some(h) {
                    p.previous = None;
                }
                if self.low_battery {
                    p.power = Power {
                        source: PowerSource::Battery,
                        percent: Some(34),
                    };
                }
                if self.network_mismatch && h == Host::Member {
                    p.consent = NetworkConsent::UnmeteredOnly;
                }
                if self.unmetered_consent {
                    p.network = NetworkClass::Unmetered;
                    p.consent = NetworkConsent::UnmeteredOnly;
                }
                Ok(Reply::Preflight(p))
            }
            Effect::InspectInstall(h) => {
                self.actions.push(format!("inspect:{h:?}"));
                self.inspections[index(h)] += 1;
                Ok(Reply::InstallState(InstallState {
                    target: "0.2.0".into(),
                    current: "0.1.3".into(),
                    previous: Some("0.2.0".into()),
                    expected: candidate().exe_sha256,
                    existing: if self.aside[index(h)] {
                        None
                    } else {
                        Some("d".repeat(64))
                    },
                    app_running: false,
                }))
            }
            Effect::SetAside {
                host,
                version,
                old_digest,
            } => {
                self.actions.push(format!("set_aside:{host:?}"));
                assert_eq!(old_digest, "d".repeat(64));
                self.aside[index(host)] = true;
                Ok(Reply::SetAside {
                    version,
                    digest: old_digest,
                    outside_programs: true,
                    versions_backup: true,
                    current_unchanged: true,
                })
            }
            Effect::Install(h) => {
                assert!(self.aside[index(h)]);
                assert!(self.inspections[index(h)] >= 2);
                self.actions.push(format!("install:{h:?}"));
                Ok(Reply::Installed {
                    version: candidate().version,
                    exe_sha256: candidate().exe_sha256,
                })
            }
            Effect::Quit(h) => {
                self.actions.push(format!("apple_event_quit:{h:?}"));
                Ok(Reply::Quit {
                    clean: true,
                    listener_gone: true,
                })
            }
            Effect::MoveFresh(h) => {
                self.actions.push(format!("fresh:{h:?}"));
                Ok(Reply::Fresh { moved: true })
            }
            Effect::Launch(h) => {
                self.actions.push(format!("launch:{h:?}"));
                let mut id = identity();
                if self.stale_launch {
                    id.control_modified_ms = 999;
                }
                Ok(Reply::Launch(id))
            }
            Effect::Seed { host, role } => {
                self.actions.push(format!("seed:{host:?}:{role:?}"));
                if role == CacheRole::Target {
                    assert!(self.join_polls > 0);
                }
                Ok(Reply::Seed { cloned: 1 })
            }
            Effect::Api { host, request } => {
                let path = request.path;
                self.bodies.push((path.into(), request.body.clone()));
                if path == "/api/demo/start" {
                    self.demo_started = true;
                }
                if self.unmetered_consent && matches!(path, "/api/create" | "/api/join/window") {
                    assert_eq!(
                        request.body.as_ref().unwrap()["consent"]["network"],
                        "unmetered_only"
                    );
                }
                self.actions.push(format!("{host:?}:{path}"));
                if self.fail_at == Some(path) {
                    return Err(Fault::new("ssh_observation_lost", FaultKind::Observation));
                }
                assert!(!path.contains("download") && !path.contains("capability/report"));
                assert_eq!(
                    request.method,
                    if path == "/api/status" {
                        Method::Get
                    } else {
                        Method::Post
                    }
                );
                if path == "/api/run-model" {
                    self.selected = true;
                    assert_eq!(request.body, Some(json!({"choice":"a9-route"})));
                }
                if path == "/api/create" {
                    assert!(
                        request
                            .body
                            .as_ref()
                            .unwrap()
                            .get("standing_consent")
                            .is_none()
                    );
                }
                if path == "/api/join/window" {
                    assert_eq!(
                        request.body.as_ref().unwrap()["standing_consent"]["caps"]["max_wall_seconds"],
                        600
                    );
                    assert_eq!(request.body.as_ref().unwrap()["join"], "PRIVATE-LINK");
                }
                if path == "/api/join/window/poll" {
                    self.join_polls += 1;
                    assert_eq!(self.join_polls, 1, "consumed join must not be polled again");
                    return Ok(Reply::Http(
                        200,
                        json!({"state":"joined","membership_state":"current"}),
                    ));
                }
                let mut s = status();
                if self.missing_cache {
                    s["acquisition"]["downloaded_bytes"] = json!(0);
                }
                if self.wrong_model && !self.selected {
                    s["run_model"]["selected"] = json!("other");
                }
                if host == Host::Member && path == "/api/status" && self.join_polls > 0 {
                    self.member_reads += 1;
                    if self.member_reads == 1 {
                        s["authority"]["membership_state"] = json!("absent");
                    }
                }
                Ok(Reply::Http(200, s))
            }
            Effect::Chat { index } => {
                self.actions.push(format!("chat:{index}"));
                Ok(Reply::Chat(Chat {
                    done: true,
                    terminal: Some("completed".into()),
                    n_tokens: 3,
                    error: self.chat_failure.then_some("redacted_product_error".into()),
                    ..Chat::default()
                }))
            }
        }
    }
}
#[test]
fn demo_stranger_and_restart_use_the_same_exact_relay_body() {
    let mut io = Fake::default();
    let e = run_with_relay(
        &mut io,
        &mut Store::default(),
        &mut Tick::default(),
        candidate(),
        limits(),
        Relay::Demo,
    );
    assert_eq!(e.outcome, "passed");
    let create = io
        .bodies
        .iter()
        .find(|(p, _)| p == "/api/create")
        .unwrap()
        .1
        .as_ref()
        .unwrap();
    assert_eq!(create.as_object().unwrap().len(), 1);
    assert!(create.get("consent").is_some());
    let relay = io
        .bodies
        .iter()
        .find(|(p, _)| p == "/api/network/start")
        .unwrap()
        .1
        .as_ref()
        .unwrap();
    let expected = json!({"relay_acknowledged":true,"max_seconds":1800,"provider":"external","public_origin":"https://demo.mycelium.now","proxy_port":4385,"cloudflare_front":true,"long_lived":true});
    assert_eq!(*relay, expected);
    assert_eq!(
        device_harness::resume::relay_body("demo", limits().relay_window).unwrap(),
        expected
    );
}
#[test]
fn owned_stranger_operator_stop_writes_snapshot_result_and_releases_marker() {
    use device_harness::{
        attempt::{Attempt, Parallel},
        private_fs::FileSnapshot,
    };
    use std::fs;
    let scratch = support::PrivateRoot::new("stranger-owned");
    let out = scratch.join("out");
    let markers = scratch.join("markers");
    let attempt = Attempt::open(
        &out,
        &markers,
        "stranger",
        ProcessIdentity {
            pid: std::process::id(),
            birth: "fixture".into(),
            executable: "fixture".into(),
        },
        Parallel::Quiet,
        scratch.uid(),
    )
    .unwrap();
    let mut io = Fake {
        operator_stop: true,
        ..Fake::default()
    };
    let e = run_owned(
        &mut io,
        &mut FileSnapshot {
            path: out.join("failure-snapshot.json"),
        },
        &mut Tick::default(),
        candidate(),
        limits(),
        attempt,
    )
    .unwrap();
    assert_eq!(e.outcome, "incomplete");
    assert_eq!(e.first_failure.as_deref(), Some("operator_stop"));
    assert_eq!(e.product_failed, Some(false));
    assert!(e.chats.is_empty());
    assert!(out.join("failure-snapshot.json").is_file());
    assert!(!markers.join("DEVICE.busy").exists());
    let saved: Value = serde_json::from_slice(&fs::read(out.join("RESULT.json")).unwrap()).unwrap();
    assert_eq!(saved["first_failure"], "operator_stop");
    assert_eq!(saved["release_qualified"], false);
    assert_eq!(saved["error"], "operator_stop");
    assert_eq!(saved["incomplete"], true);
    assert_eq!(saved["stranger_path_passed"], false);
    assert_eq!(saved["product_failed"], false);
    assert!(io.actions.contains(&"Owner:/api/demo/stop".into()));
}
#[test]
fn local_power_admission_precedes_remote_preflight_and_preserves_initial_reading() {
    let mut io = Fake {
        low_battery: true,
        ..Fake::default()
    };
    let e = run(
        &mut io,
        &mut Store::default(),
        &mut Tick::default(),
        candidate(),
        limits(),
    );
    assert!(!io.actions.contains(&"preflight:Member".into()));
    assert_eq!(e.initial_owner_power.unwrap().percent, Some(34));
    assert!(io.actions.contains(&"snapshot:Member".into())); // diagnostics remain independent
}
#[test]
fn runtime_battery_guard_stops_demo_incomplete_without_chat_or_force_kill() {
    let mut io = Fake {
        runtime_stop_after_demo: true,
        ..Fake::default()
    };
    let e = run(
        &mut io,
        &mut Store::default(),
        &mut Tick::default(),
        candidate(),
        limits(),
    );
    assert_eq!(e.first_failure.as_deref(), Some("battery_floor"));
    assert_eq!(e.battery_stop_percentages[1], Some(24));
    assert_eq!(e.outcome, "incomplete");
    assert_eq!(e.product_failed, Some(false));
    assert!(e.chats.is_empty());
    assert!(io.actions.contains(&"Owner:/api/demo/stop".into()));
}
#[test]
fn admitted_unmetered_only_consent_is_never_silently_broadened_in_gui_body() {
    let mut io = Fake {
        unmetered_consent: true,
        ..Fake::default()
    };
    let e = run(
        &mut io,
        &mut Store::default(),
        &mut Tick::default(),
        candidate(),
        limits(),
    );
    assert_eq!(e.outcome, "passed");
}
#[test]
fn complete_stranger_runs_exact_gui_path_no_download_reconsent_or_capability_repair() {
    let mut io = Fake {
        wrong_model: true,
        ..Fake::default()
    };
    let mut store = Store::default();
    let e = run(
        &mut io,
        &mut store,
        &mut Tick::default(),
        candidate(),
        limits(),
    );
    assert_eq!(e.outcome, "passed", "{:?}", e.first_failure);
    assert!(evaluate(&candidate(), &e));
    assert_eq!(e.chats.len(), 3);
    assert!(store.0.is_none());
    assert_eq!(io.join_polls, 1);
    assert!(io.actions.contains(&"Owner:/api/run-model".into()));
    assert_eq!(e.admissions.len(), 2);
    assert!(!serde_json::to_string(&e).unwrap().contains("PRIVATE"));
}
#[test]
fn preflight_battery_and_network_refuse_before_mutations_and_collect_both_snapshots() {
    for network in [false, true] {
        let mut io = Fake {
            low_battery: !network,
            network_mismatch: network,
            ..Fake::default()
        };
        let mut store = Store::default();
        let e = run(
            &mut io,
            &mut store,
            &mut Tick::default(),
            candidate(),
            limits(),
        );
        assert_eq!(e.outcome, "incomplete");
        assert_eq!(e.product_failed, Some(false));
        assert!(
            !io.actions
                .iter()
                .any(|s| s.starts_with("install:") || s.starts_with("launch:"))
        );
        assert!(io.actions.contains(&"snapshot:Owner".into()));
        assert!(io.actions.contains(&"snapshot:Member".into()));
        assert!(store.0.is_some());
    }
}
#[test]
fn missing_model_and_stale_launch_refuse_and_do_not_pair_or_start() {
    for stale in [false, true] {
        let mut io = Fake {
            missing_cache: !stale,
            stale_launch: stale,
            ..Fake::default()
        };
        let e = run(
            &mut io,
            &mut Store::default(),
            &mut Tick::default(),
            candidate(),
            limits(),
        );
        assert_ne!(e.outcome, "passed");
        assert!(!io.actions.contains(&"Owner:/api/pair/window/open".into()));
        assert!(!io.actions.contains(&"Owner:/api/demo/start".into()));
    }
}
#[test]
fn chat_product_failure_and_ssh_observation_loss_are_distinct_and_stop_after_snapshot() {
    for loss in [false, true] {
        let mut io = Fake {
            chat_failure: !loss,
            fail_at: loss.then_some("/api/demo/start"),
            ..Fake::default()
        };
        let mut store = Store::default();
        let e = run(
            &mut io,
            &mut store,
            &mut Tick::default(),
            candidate(),
            limits(),
        );
        assert_eq!(e.outcome, if loss { "incomplete" } else { "failed" });
        assert_eq!(e.product_failed, Some(!loss));
        let snapshot = io
            .actions
            .iter()
            .position(|s| s == "snapshot:Owner")
            .unwrap();
        let stop = io
            .actions
            .iter()
            .position(|s| s == "Owner:/api/demo/stop")
            .unwrap();
        assert!(snapshot < stop);
        assert!(store.0.is_some());
    }
}
#[test]
fn correct_default_model_does_not_call_picker_and_same_version_set_aside_readback_is_required() {
    let mut io = Fake::default();
    let e = run(
        &mut io,
        &mut Store::default(),
        &mut Tick::default(),
        candidate(),
        limits(),
    );
    assert_eq!(e.outcome, "passed");
    assert!(!io.actions.contains(&"Owner:/api/run-model".into()));
    for h in [Host::Owner, Host::Member] {
        let aside = io
            .actions
            .iter()
            .position(|s| s == &format!("set_aside:{h:?}"))
            .unwrap();
        let install = io
            .actions
            .iter()
            .position(|s| s == &format!("install:{h:?}"))
            .unwrap();
        assert!(aside < install);
        assert!(io.inspections[index(h)] >= 2);
    }
}
