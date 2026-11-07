// SPDX-License-Identifier: AGPL-3.0-only
use backend_mlx::{
    process::{OwnedWorker, ProcessLimits},
    resources::ResourcePool,
};
use budgets::{BudgetId, Bytes, Entries, Reservation, Resources};
use node::{Supervisor, Ticket};
use node_service::{status::refresh, worker::LeafAdapter};
use std::{
    process::Command,
    time::{Duration, Instant},
};
fn duration(id: BudgetId) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
#[test]
fn exact_owned_leaf_cancel_reap_becomes_l6_cleanup_observation() {
    let amount = Reservation {
        resident: Bytes::new(1),
        staged: Bytes::ZERO,
        staging_overhead: Bytes::ZERO,
        scratch: Bytes::ZERO,
        kv: Bytes::ZERO,
    };
    let pool = ResourcePool::new(amount.total().unwrap());
    let mut command = Command::new("/bin/sleep");
    command.arg(duration(BudgetId::WorkerDefaultWall).as_secs().to_string());
    let limits = ProcessLimits {
        deadline: Instant::now() + duration(BudgetId::WorkerStartup),
        close: BudgetId::RouterClose.span(Entries::ZERO).unwrap(),
        reap: BudgetId::WorkerCleanupReserve.span(Entries::ZERO).unwrap(),
        poll: BudgetId::RegistryPoll.span(Entries::ZERO).unwrap(),
    };
    let leaf = LeafAdapter::new(OwnedWorker::spawn(command, pool.clone(), amount, limits).unwrap());
    let pid = leaf.pid();
    let ticket = Ticket {
        epoch: 1,
        run: 1,
        node: "member".into(),
        incarnation: "boot".into(),
    };
    let mut supervisor = Supervisor::new(ticket.clone(), leaf);
    assert!(!supervisor.observe_cleanup().unwrap().proven());
    supervisor.request_stop().unwrap();
    let observation = supervisor.observe_cleanup().unwrap();
    assert!(observation.proven());
    assert_eq!(observation.ticket(), &ticket);
    assert_eq!(pool.used().unwrap(), Resources::default());
    assert_eq!(
        Command::new("/bin/ps")
            .args(["-p", &pid.to_string(), "-o", "pid="])
            .status()
            .unwrap()
            .code(),
        Some(1)
    );
}
#[test]
fn runtime_publication_projects_to_api_without_effect_lock() {
    let mut doc: serde_json::Value =
        serde_json::from_str(include_str!("../../api/contracts/status-example.json")).unwrap();
    doc["running"] = true.into();
    let source = runtime::published::Published::new(1, api::schema::Snapshot::parse(doc).unwrap());
    let target = api::status::PublishedStatus::new();
    refresh(&source, &target, 1).unwrap();
    assert_eq!(target.read().unwrap()["running"], true);
    assert!(refresh(&source, &target, 1).is_err());
}
