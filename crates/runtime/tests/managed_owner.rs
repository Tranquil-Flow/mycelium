// SPDX-License-Identifier: AGPL-3.0-only
//! Real private journal; fixture IO is not a mailbox or native worker.
use protocol::identity::Identity;
use runtime::managed_owner::{Limits, ManagedControlOwner, RequestMeta};
use serde_json::{Value, json};
use std::fs;
use std::os::unix::fs::{PermissionsExt, symlink};
use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, Ordering};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Root(PathBuf);
impl Root {
    fn new() -> Self {
        let path = std::env::temp_dir().join(format!(
            "managed-owner-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&path).expect("fresh root");
        fs::set_permissions(&path, fs::Permissions::from_mode(0o700)).expect("private root");
        Self(path)
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("owned fixture cleanup");
    }
}
fn signer() -> Identity {
    Identity::from_secret("owner-endpoint", &[7; 32]).expect("fixture identity")
}
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/managed-owner-oracle.json"))
        .expect("executed oracle")
}
fn case(name: &str) -> Value {
    fixture()["cases"]
        .as_array()
        .expect("cases")
        .iter()
        .find(|x| x["name"] == name)
        .expect("case")
        .clone()
}
fn open(r: &Root) -> ManagedControlOwner {
    ManagedControlOwner::open(
        &r.0,
        &signer(),
        "swarm",
        "owner",
        Limits::registered().expect("registered source bounds"),
    )
    .expect("real owner must open")
}
fn disk(r: &Root) -> Value {
    serde_json::from_slice(&fs::read(r.0.join("owner.json")).expect("persisted journal"))
        .expect("journal JSON")
}
fn binding() -> Value {
    case("uncertain_enqueue")["journal"]["commands"]["hello-1"]["binding"].clone()
}
fn command() -> Value {
    case("uncertain_enqueue")["journal"]["commands"]["hello-1"]["enqueue_request"]["envelope"]["statement"]["native_command"].clone()
}
#[path = "support/managed_owner_exchange.rs"]
mod exchange;
#[path = "support/managed_owner_process.rs"]
mod process;
fn meta() -> RequestMeta {
    RequestMeta {
        request_id: "00000000000000000000000000000002".into(),
        nonce: "00000000000000000000000000000001".into(),
        issued_at: 1000.0,
        ttl: budgets::Span::from_millis(30_000),
    }
}
#[test]
fn creates_real_private_journal_and_exclusive_owner_lock() {
    let r = Root::new();
    let o = open(&r);
    assert_eq!(disk(&r), case("created")["journal"]);
    for f in ["owner.json", "owner.lock"] {
        assert_eq!(
            fs::metadata(r.0.join(f))
                .expect("private file")
                .permissions()
                .mode()
                & 0o777,
            0o600
        );
    }
    let other = ManagedControlOwner::open(
        &r.0,
        &signer(),
        "swarm",
        "owner",
        Limits::registered().unwrap(),
    );
    assert_eq!(
        other.err().expect("exclusive lock").0,
        case("double_open")["error"].as_str().unwrap()
    );
    drop(o);
    drop(open(&r));
}
#[test]
fn signed_intent_is_durable_and_exact_on_retry_and_restart() {
    let r = Root::new();
    let mut o = open(&r);
    let prepared = o
        .prepare_enqueue(&signer(), &binding(), &command(), meta())
        .expect("prepare");
    assert_eq!(disk(&r), case("uncertain_enqueue")["journal"]);
    assert_eq!(
        prepared,
        case("uncertain_enqueue")["calls"][0]["journal"]["commands"]["hello-1"]["enqueue_request"]
    );
    let mut changed = meta();
    changed.issued_at = 1005.0;
    changed.request_id = "must-not-replace".into();
    assert_eq!(
        o.prepare_enqueue(&signer(), &binding(), &command(), changed.clone())
            .expect("retry"),
        prepared
    );
    drop(o);
    let mut o = open(&r);
    assert_eq!(
        o.prepare_enqueue(&signer(), &binding(), &command(), changed)
            .expect("restart exact"),
        prepared
    );
    let result = o
        .prepare_result(&signer(), &binding(), &command(), meta())
        .expect("read-only result");
    assert_eq!(result["envelope"]["statement"]["sequence"], 128);
    assert_eq!(disk(&r)["write_intents"], 1);
}
#[test]
fn migrated_counters_and_refused_boundary_match_executed_oracle() {
    for (v, sequence) in [("v1", 9), ("v2", 4096)] {
        let r = Root::new();
        drop(open(&r));
        let mut old = disk(&r);
        old["protocol"] = json!(format!("mycelium.a13.owner_control_journal.{v}"));
        old["sequence"] = json!(sequence);
        old.as_object_mut().unwrap().remove("sequence_ceiling");
        old.as_object_mut().unwrap().remove("write_intents");
        fs::write(
            r.0.join("owner.json"),
            protocol::canonical::to_bytes(&old).unwrap(),
        )
        .unwrap();
        let mut o = open(&r);
        assert_eq!(disk(&r), case(&format!("migration_{v}"))["journal"]);
        if v == "v2" {
            assert_eq!(
                o.prepare_enqueue(&signer(), &binding(), &command(), meta())
                    .unwrap_err()
                    .0,
                "managed_owner_request_limit_exceeded"
            );
        }
    }
}
#[test]
fn refuses_foreign_authority_and_unsafe_journal_without_rewriting() {
    let r = Root::new();
    drop(open(&r));
    let before = fs::read(r.0.join("owner.json")).unwrap();
    assert_eq!(
        ManagedControlOwner::open(
            &r.0,
            &signer(),
            "other",
            "owner",
            Limits::registered().unwrap()
        )
        .err()
        .unwrap()
        .0,
        "managed_owner_state_invalid"
    );
    assert_eq!(fs::read(r.0.join("owner.json")).unwrap(), before);
    fs::rename(r.0.join("owner.json"), r.0.join("retained")).unwrap();
    symlink("retained", r.0.join("owner.json")).unwrap();
    assert!(
        ManagedControlOwner::open(
            &r.0,
            &signer(),
            "swarm",
            "owner",
            Limits::registered().unwrap()
        )
        .is_err()
    );
    fs::remove_file(r.0.join("owner.json")).unwrap();
    fs::hard_link(r.0.join("retained"), r.0.join("owner.json")).unwrap();
    assert!(
        ManagedControlOwner::open(
            &r.0,
            &signer(),
            "swarm",
            "owner",
            Limits::registered().unwrap()
        )
        .is_err()
    );
}
#[test]
fn persistence_failure_prevents_intent_and_breaks_owner() {
    let r = Root::new();
    let mut o = open(&r);
    // A real rename boundary failure, not a callback claiming persistence worked.
    fs::rename(r.0.join("owner.json"), r.0.join("retained")).unwrap();
    fs::create_dir(r.0.join("owner.json")).unwrap();
    assert_eq!(
        o.prepare_enqueue(&signer(), &binding(), &command(), meta())
            .unwrap_err()
            .0,
        "managed_owner_state_unavailable"
    );
    assert_eq!(
        o.prepare_enqueue(&signer(), &binding(), &command(), meta())
            .unwrap_err()
            .0,
        "managed_owner_closed"
    );
}

#[test]
fn result_preparation_matches_actual_python_client_signed_bytes() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/managed-owner-prepare-oracle.json")).unwrap();
    let r = Root::new();
    let mut o = open(&r);
    let expected = &fixture["request"]["envelope"];
    let s = &expected["statement"];
    let meta = RequestMeta {
        request_id: s["request_id"].as_str().unwrap().into(),
        nonce: s["nonce"].as_str().unwrap().into(),
        issued_at: 1000.0,
        ttl: budgets::BudgetId::PyFinalMyceliumA13MacosManagedControlTtlSecondsL2386C73
            .span(budgets::Entries::ZERO)
            .unwrap(),
    };
    let first = o
        .prepare_result(&signer(), &binding(), &command(), meta.clone())
        .unwrap();
    assert_eq!(
        first["envelope"]["statement"]["sequence"],
        fixture["reserved_sequence"]
    );
    let next = o
        .prepare_result(&signer(), &binding(), &command(), meta)
        .unwrap();
    assert_eq!(next["envelope"], *expected);
    assert_eq!(disk(&r), fixture["journal"]);
}
#[test]
/// Partial B-AUTH-04: signer-global counter survives distinct logical runs.
fn new_run_never_resets_the_signer_global_sequence() {
    let r = Root::new();
    let mut o = open(&r);
    let first = o
        .prepare_result(&signer(), &binding(), &command(), meta())
        .unwrap();
    let mut other_binding = binding();
    other_binding["run_id"] = json!("next-run");
    let mut other_command = command();
    other_command["run_id"] = json!("next-run");
    other_command["command_id"] = json!("new-run-command");
    let next = o
        .prepare_enqueue(&signer(), &other_binding, &other_command, meta())
        .unwrap();
    assert!(
        next["envelope"]["statement"]["sequence"].as_u64().unwrap()
            > first["envelope"]["statement"]["sequence"].as_u64().unwrap()
    );
}
