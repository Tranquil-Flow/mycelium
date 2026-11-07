// SPDX-License-Identifier: AGPL-3.0-only
use control_runtime::Store;
use std::{
    os::unix::fs::{PermissionsExt, symlink},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
#[test]
fn private_database_and_symlink_refusal() {
    let root = std::env::temp_dir().join(format!(
        "v02-l6-private-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::SeqCst)
    ));
    std::fs::create_dir(&root).unwrap();
    let path = root.join("state.db");
    let s = Store::open(&path).unwrap();
    drop(s);
    let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
    let link = root.join("link.db");
    symlink(&path, &link).unwrap();
    let refused = Store::open(&link).is_err();
    let budget = budgets::BudgetId::ControlTtl
        .span(budgets::Entries::ZERO)
        .unwrap();
    let configured = Store::open_with_policy(&path, None, budget).unwrap();
    let configured_wait = configured
        .connection()
        .query_row("PRAGMA busy_timeout", [], |r| r.get::<_, u64>(0))
        .unwrap();
    drop(configured);
    std::fs::remove_dir_all(root).unwrap();
    assert_eq!(configured_wait, budget.millis());
    assert_eq!(mode, 0o600);
    assert!(refused);
}
