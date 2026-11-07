// SPDX-License-Identifier: AGPL-3.0-only
// The fork/socket ordering lock shared by test binaries (S23; see fork_boundary.rs).
static FIXTURE_SPAWN_BOUNDARY: std::sync::Mutex<()> = std::sync::Mutex::new(());
fn fixture_spawn_boundary() -> std::sync::MutexGuard<'static, ()> {
    // The guarded value is `()`; a failed test must not cascade as poisoning.
    FIXTURE_SPAWN_BOUNDARY
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
