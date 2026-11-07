// SPDX-License-Identifier: AGPL-3.0-only
mod support;
use budgets::Span;
use device_harness::{
    Host, Result,
    admission::{Power, PowerSource},
    effects::Clock,
    http::Chat,
    lifecycle::PowerIo,
    observe::{Cpu, Mailbox, Metrics, Observer, SnapshotStore},
    soak::*,
    soak_driver::*,
};
use serde_json::{Value, json};
#[derive(Default)]
struct Fake {
    now: u64,
    restarted: bool,
    stops: usize,
    low: bool,
    initial_transport_failure: bool,
    statuses: Vec<Host>,
    metrics: Vec<Host>,
}
impl Clock for Fake {
    fn now(&self) -> u64 {
        self.now
    }
    fn sleep(&mut self, s: Span) -> Result<()> {
        self.now += s.millis();
        Ok(())
    }
}
impl PowerIo for Fake {
    fn power(&mut self, h: Host) -> Result<Power> {
        if self.initial_transport_failure && h == Host::Member {
            return Err(device_harness::Fault::new(
                "command_failed:ssh:rc255",
                device_harness::FaultKind::Observation,
            ));
        }
        Ok(Power {
            source: PowerSource::Battery,
            percent: Some(if self.low && self.now >= 60000 && h == Host::Member {
                24
            } else {
                61
            }),
        })
    }
    fn stop_demo(&mut self) -> Result<()> {
        self.stops += 1;
        Ok(())
    }
}
fn run(f: &Fake) -> Run {
    Run {
        run_id: Some(
            if f.now >= 1680000 {
                "r3"
            } else if f.restarted {
                "r2"
            } else {
                "r1"
            }
            .into(),
        ),
        state: if (1680000..1740000).contains(&f.now) {
            "running"
        } else {
            "serving"
        }
        .into(),
        route_ready: !(1680000..1740000).contains(&f.now),
        participants: 2,
    }
}
impl Observer for Fake {
    fn status(&mut self, h: Host) -> Result<(u16, Value)> {
        self.statuses.push(h);
        let r = run(self);
        Ok((
            200,
            json!({"authority":{"membership_state":"current","lease_state":"fresh","capability_state":"self_report_accepted"},"owner_run":{"run_id":r.run_id,"state":r.state,"route_ready":r.route_ready,"participants":[{},{}],"rollover":{"state":"idle"}},"cookie":"PRIVATE"}),
        ))
    }
    fn metrics(&mut self, h: Host) -> Result<Metrics> {
        self.metrics.push(h);
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
            database_bytes: Some(if h == Host::Owner { 2048 } else { 0 }),
            storage_bytes: 2048,
            local_files: 0,
            state: "observed".into(),
        })
    }
}
impl SoakIo for Fake {
    fn chat(&mut self) -> Result<Chat> {
        Ok(Chat {
            done: true,
            terminal: Some("completed".into()),
            n_tokens: 3,
            ..Chat::default()
        })
    }
    fn restart_owner(&mut self) -> Result<(Restart, Run)> {
        self.restarted = true;
        self.status(Host::Member)?;
        Ok((
            Restart {
                expectation: RestartExpectation::AutomaticRecovery,
                attempted: true,
                recovered: true,
                idle_observed: false,
                explicit_start: false,
                member_mutations: 0,
                endpoint_changed: Some(true),
                approval: None,
            },
            run(self),
        ))
    }
    fn visitor(&mut self) -> Result<bool> {
        panic!("unpublished")
    }
    fn resources(&mut self, _: Host) -> Result<()> {
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
fn policy() -> Policy {
    Policy {
        requested_ms: 2100000,
        minimum_ms: 2100000,
        chat_every_ms: 300000,
        sample_every_ms: 60000,
        mailbox_every_ms: 600000,
        visitor_every_ms: 600000,
        restart_at_ms: Some(720000),
        minimum_rollovers: 1,
        mailbox_max_bytes: Some(4096),
        restart_expectation: RestartExpectation::AutomaticRecovery,
        required_steps: Default::default(),
    }
}
fn limits() -> Limits {
    Limits {
        poll: Span::from_millis(1000),
        power_sample: Span::from_millis(60000),
    }
}
#[test]
fn owned_soak_finalizes_success_short_failure_and_battery_incomplete() {
    use device_harness::{
        attempt::{Attempt, Parallel},
        preflight::ProcessIdentity,
        private_fs::FileSnapshot,
    };
    use std::fs;
    for scenario in 0..3 {
        let root = support::PrivateRoot::new(&format!("soak-{scenario}"));
        let out = root.join("out");
        let markers = root.join("markers");
        let a = Attempt::open(
            &out,
            &markers,
            "soak",
            ProcessIdentity {
                pid: std::process::id(),
                birth: "fixture".into(),
                executable: "fixture-harness".into(),
            },
            Parallel::Quiet,
            root.uid(),
        )
        .unwrap();
        let mut f = Fake {
            low: scenario == 2,
            ..Fake::default()
        };
        let mut p = policy();
        if scenario == 1 {
            p.requested_ms = 120000;
            p.restart_at_ms = None;
            p.mailbox_max_bytes = None;
        }
        let r = drive_owned(
            &mut f,
            &mut FileSnapshot {
                path: out.join("failure-snapshot.json"),
            },
            p,
            limits(),
            a,
        )
        .unwrap();
        assert_eq!(r.verdict.scenario_passed, scenario == 0);
        assert!(!markers.join("DEVICE.busy").exists());
        let written: Value =
            serde_json::from_slice(&fs::read(out.join("SOAK-RESULT.json")).unwrap()).unwrap();
        assert_eq!(written["verdict"]["scenario_passed"], scenario == 0);
        assert_eq!(out.join("failure-snapshot.json").exists(), scenario != 0);
        if scenario == 2 {
            assert_eq!(written["battery_stop_percentages"][1], 24);
            assert_eq!(written["verdict"]["outcome"], "incomplete");
        }
    }
}
#[test]
fn initial_power_failure_class_and_cause_survive_soak_receipt() {
    let mut f = Fake {
        initial_transport_failure: true,
        ..Fake::default()
    };
    let r = drive(&mut f, &mut Store::default(), policy(), limits());
    assert_eq!(r.verdict.outcome, "incomplete");
    assert_eq!(r.verdict.reasons, ["preflight_ssh_unreachable"]);
    let value = serde_json::to_value(r).unwrap();
    assert_eq!(
        value["power_causes"],
        json!([{"code":"command_failed:ssh:rc255","class":"Failure","kind":"observation"}])
    );
    assert!(!f.restarted);
    assert_eq!(f.stops, 0);
}
#[test]
fn complete_soak_dispatches_thirty_seven_read_only_member_statuses() {
    let mut f = Fake::default();
    let mut store = Store::default();
    let r = drive(&mut f, &mut store, policy(), limits());
    assert!(r.verdict.scenario_passed, "{:?}", r.verdict.reasons);
    assert!(r.verdict.f3_automatic_recovery);
    assert_eq!(r.evidence.chats.len(), 7);
    assert_eq!(r.evidence.samples, 35);
    assert_eq!(r.evidence.rollovers.len(), 1);
    assert_eq!(r.evidence.rollovers[0].gap_ms, 60000);
    assert_eq!(r.evidence.mailbox_samples.len(), 5);
    assert_eq!(
        f.statuses.iter().filter(|h| **h == Host::Member).count(),
        37
    );
    assert_eq!(f.stops, 0);
    assert!(store.0.is_none());
    assert!(!r.verdict.release_qualified);
    assert_eq!(&f.metrics[..2], [Host::Owner, Host::Member]);
    assert_eq!(r.evidence.host_metrics.len(), 35);
    assert_eq!(
        r.samples[0]["owner_run"]["rollover"],
        json!({"state":"idle"})
    );
    assert_eq!(
        r.samples[0]["hosts"]["laptop"],
        serde_json::to_value(f.metrics(Host::Member).unwrap()).unwrap()
    );
}
#[test]
fn short_no_rollover_soak_snapshots_both_hosts_and_stops() {
    let mut f = Fake::default();
    let mut store = Store::default();
    let mut p = policy();
    p.requested_ms = 120000;
    p.restart_at_ms = None;
    p.mailbox_max_bytes = None;
    let r = drive(&mut f, &mut store, p, limits());
    assert!(!r.verdict.scenario_passed);
    assert!(
        r.verdict
            .reasons
            .contains(&"mailbox_bound_not_specified".into())
    );
    assert_eq!(f.stops, 1);
    assert!(store.0.is_some());
    assert!(!serde_json::to_string(&store.0).unwrap().contains("PRIVATE"));
}
#[test]
fn battery_floor_is_incomplete_retains_percentages_and_cleans() {
    let mut f = Fake {
        low: true,
        ..Fake::default()
    };
    let mut store = Store::default();
    let r = drive(&mut f, &mut store, policy(), limits());
    assert_eq!(r.verdict.outcome, "incomplete");
    assert_eq!(r.verdict.reasons, ["battery_floor"]);
    assert_eq!(r.verdict.product_failed, Some(false));
    assert!(!r.verdict.scenario_passed);
    assert_eq!(r.battery_stop_percentages[1], Some(24));
    assert!(f.stops >= 1);
    assert!(store.0.is_some());
    assert_eq!(r.evidence.product_failure, None);
}
#[test]
fn registered_driver_enforces_owner_floor_and_blocks_missing_registry_before_effects() {
    struct Registry(bool);
    impl device_harness::harness_budgets::Registry for Registry {
        fn span(&self, name: &str) -> Result<Span> {
            assert_eq!(name, "HarnessMinimumSoak");
            if self.0 {
                Ok(Span::from_millis(1_200_000))
            } else {
                Err(device_harness::Fault::new(
                    "fixture_registry_missing",
                    device_harness::FaultKind::Admission,
                ))
            }
        }
        fn count(&self, _: &str) -> Result<u64> {
            panic!("unexpected count request")
        }
        fn bytes(&self, _: &str) -> Result<budgets::Bytes> {
            panic!("unexpected byte-cap request")
        }
    }
    let mut p = policy();
    p.minimum_ms = 1;
    p.requested_ms = 1_199_000;
    let mut f = Fake::default();
    let report = drive_registered(
        &Registry(true),
        &mut f,
        &mut Store::default(),
        p.clone(),
        limits(),
    )
    .unwrap();
    assert_eq!(report.evidence.duration_ms, 1_199_000);
    assert!(
        report
            .verdict
            .reasons
            .contains(&"duration_incomplete".into())
    );
    p.requested_ms = 1_200_000;
    let report = drive_registered(
        &Registry(true),
        &mut Fake::default(),
        &mut Store::default(),
        p.clone(),
        limits(),
    )
    .unwrap();
    assert!(
        !report
            .verdict
            .reasons
            .contains(&"duration_incomplete".into())
    );
    let mut f = Fake::default();
    assert_eq!(
        drive_registered(&Registry(false), &mut f, &mut Store::default(), p, limits())
            .err()
            .unwrap()
            .code,
        "fixture_registry_missing"
    );
    assert!(f.statuses.is_empty());
    assert_eq!(f.now, 0);
}
