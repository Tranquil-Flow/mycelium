// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::{Fault, FaultKind, Host, Result, observe::*};
use serde_json::{Value, json};
#[test]
fn full_nested_diagnostics_redact_fields_values_urls_without_step_truncation() {
    let steps:Vec<_>=(0..20).map(|i|json!({"name":i.to_string(),"details":{"error":"timeout","authorization_token":"PRIVATE-CREDENTIAL"}})).collect();
    let input = json!({"owner_run":{"rollover":{"reason":"member_timeout","attempts":[1,2,3]},"cleanup_unproven":true,"switching_runs":true,"steps":steps,"error_message":"failed PRIVATE-CREDENTIAL https://fixture.invalid/join#PRIVATE"},"private_key":"SECRETKEY","join_code":"JOINCODE","elsewhere":"SECRETKEY JOINCODE","unknown_diagnostic":{"full":true}});
    let safe = redact(&input, &[]);
    let text = safe.to_string();
    assert_eq!(safe["owner_run"]["steps"].as_array().unwrap().len(), 20);
    assert_eq!(
        safe["owner_run"]["rollover"],
        input["owner_run"]["rollover"]
    );
    assert_eq!(safe["unknown_diagnostic"]["full"], true);
    assert_eq!(safe["owner_run"]["steps"][0]["details"]["error"], "timeout");
    for s in [
        "PRIVATE-CREDENTIAL",
        "SECRETKEY",
        "JOINCODE",
        "https://fixture.invalid",
    ] {
        assert!(!text.contains(s), "{s}");
    }
}
#[test]
fn top_five_cpu_uses_comm_basename_sorted_not_argv_and_validates_numbers() {
    let rows = "10 12.5 /bin/alpha\n20 50.0 /Applications/Some App/worker\n30 1 /bin/c\n40 2 /bin/d\n50 3 /bin/e\n60 4 /bin/f\n";
    let m = metrics([1., 2., 3.], rows).unwrap();
    assert_eq!(m.load, [1., 2., 3.]);
    assert_eq!(m.top_cpu.len(), 5);
    assert_eq!(
        m.top_cpu[0],
        Cpu {
            pid: 20,
            cpu_percent: 50.,
            command: "worker".into()
        }
    );
    assert!(metrics([f64::NAN, 2., 3.], rows).is_err());
    assert!(metrics([1., 2., 3.], "1 NaN worker").is_err());
    assert!(metrics([1., 2., 3.], "1 1 worker").is_err());
}
#[test]
fn mailbox_counts_both_databases_and_wal_shm_target_zero_owner_unknown() {
    assert_eq!(
        mailbox(false, false, &[None, None], &[None; 4])
            .unwrap()
            .database_bytes,
        None
    );
    let m = mailbox(true, true, &[None, None], &[None; 4]).unwrap();
    assert_eq!(m.database_bytes, Some(0));
    assert_eq!(m.state, "target_remote_mailbox_no_local_db");
    let m = mailbox(
        true,
        true,
        &[Some(3), Some(7)],
        &[Some(5), None, Some(11), Some(13)],
    )
    .unwrap();
    assert_eq!(m.database_bytes, Some(10));
    assert_eq!(m.storage_bytes, 39);
    assert_eq!(m.local_files, 2);
    assert!(mailbox(false, false, &[Some(u64::MAX), Some(1)], &[None; 4]).is_err());
}
struct Fake {
    reads: Vec<(Host, &'static str)>,
    owner_down: bool,
}
impl Observer for Fake {
    fn status(&mut self, h: Host) -> Result<(u16, Value)> {
        self.reads.push((h, "status"));
        if h == Host::Owner && self.owner_down {
            Err(Fault::new("status_timeout", FaultKind::Observation))
        } else {
            Ok((
                200,
                json!({"details":{"nested":"retained"},"cookie":"PRIVATE"}),
            ))
        }
    }
    fn metrics(&mut self, h: Host) -> Result<Metrics> {
        self.reads.push((h, "metrics"));
        Ok(Metrics {
            load: [1., 2., 3.],
            top_cpu: (1..=5)
                .map(|pid| Cpu {
                    pid,
                    cpu_percent: 10.,
                    command: "worker".into(),
                })
                .collect(),
        })
    }
    fn mailbox(&mut self, h: Host) -> Result<Mailbox> {
        self.reads.push((h, "mailbox"));
        mailbox(h == Host::Member, true, &[None, None], &[None; 4])
    }
}
#[derive(Default)]
struct Store {
    first: Option<Value>,
    fail: bool,
}
impl SnapshotStore for Store {
    fn exists(&mut self) -> Result<bool> {
        Ok(self.first.is_some())
    }
    fn create_first(&mut self, v: &Value) -> Result<()> {
        if self.fail {
            return Err(Fault::new("snapshot_write_failed", FaultKind::Observation));
        }
        assert!(self.first.is_none());
        self.first = Some(v.clone());
        Ok(())
    }
}
#[test]
fn failure_snapshot_attempts_both_even_owner_down_preserves_first_and_write_error() {
    let mut io = Fake {
        reads: vec![],
        owner_down: true,
    };
    let mut store = Store::default();
    let cause = Fault::new("battery_floor", FaultKind::Admission);
    failure_snapshot(&mut io, &mut store, cause, &[]).unwrap();
    let first = store.first.clone().unwrap();
    assert_eq!(first["owner"]["unavailable"], true);
    assert_eq!(first["member"]["status"]["details"]["nested"], "retained");
    assert_eq!(first["first_failure"]["code"], "battery_floor");
    assert!(!first.to_string().contains("PRIVATE"));
    assert!(io.reads.contains(&(Host::Member, "status")));
    io.reads.clear();
    failure_snapshot(&mut io, &mut store, cause, &[]).unwrap();
    assert!(io.reads.is_empty());
    assert_eq!(store.first, Some(first));
    store.first = None;
    store.fail = true;
    assert_eq!(
        failure_snapshot(&mut io, &mut store, cause, &[])
            .unwrap_err()
            .code,
        "snapshot_write_failed"
    );
}
#[test]
fn each_sample_reports_both_mailboxes_and_host_load_even_status_fails() {
    let mut io = Fake {
        reads: vec![],
        owner_down: true,
    };
    let v = sample(&mut io, &[]);
    for h in [Host::Owner, Host::Member] {
        for op in ["status", "metrics", "mailbox"] {
            assert!(io.reads.contains(&(h, op)));
        }
    }
    assert_eq!(
        v["member"]["metrics"]["top_cpu"].as_array().unwrap().len(),
        5
    );
    assert_eq!(v["owner"]["mailbox"]["database_bytes"], Value::Null);
    assert_eq!(v["member"]["mailbox"]["database_bytes"], 0);
}
