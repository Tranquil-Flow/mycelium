// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged `mycelium` executable is also the node's worker: the owner re-executes
//! `current_exe()` as `member|owner-leaf <launch> <state>`, and since the RU-020 split each
//! member agent re-executes it again as `native-worker` (stdout = the private link). Preview 9's
//! `bundle_chat` failed because the packaged entry knew only the first two: the native worker
//! answered `no_arguments_accepted` in 9 ms, the run settled Failed and no `result.json` was
//! written (`chat-live.log`: `native_worker_spawned pid=49158` ... `no_arguments_accepted` ...
//! `worker:worker_native_exited`).
//!
//! These tests launch the real built executable OUTSIDE a bundle, where every worker role must
//! stop at admission (`payload:ExecutableLayout`) before anything native loads. That separates
//! "the role is routed to the worker path" from "the role is rejected as an unknown argument".
#![cfg(all(feature = "packaged", target_os = "macos"))]
use std::process::{Command, Stdio};

const EXE: &str = env!("CARGO_BIN_EXE_mycelium");

/// Run the executable with `args`, stdout to the null device (a worker's private link is
/// stdout), a scratch-less environment, and return `(success, stderr)`.
fn run(args: &[&str]) -> (bool, String) {
    let mut command = Command::new(EXE);
    command
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::null());
    if let Some(lib) = std::env::var_os("MLXC_LIB_DIR") {
        command.env("DYLD_LIBRARY_PATH", lib);
    }
    let output = command.output().expect("spawn the packaged executable");
    (
        output.status.success(),
        String::from_utf8_lossy(&output.stderr).into_owned(),
    )
}

#[test]
fn the_native_worker_role_is_routed_to_admission_not_rejected_as_an_argument() {
    let (success, stderr) = run(&["native-worker"]);
    assert!(!success, "an unadmitted worker must not run: {stderr}");
    assert!(
        !stderr.contains("no_arguments_accepted"),
        "native-worker was rejected as an unknown argument: {stderr}"
    );
    assert!(
        stderr.starts_with("payload:ExecutableLayout"),
        "native-worker did not stop at bundle admission: {stderr}"
    );
}

#[test]
fn member_and_owner_leaf_roles_stop_at_the_same_admission() {
    for role in ["member", "owner-leaf"] {
        let (success, stderr) = run(&[role, "launch", "/nonexistent-state"]);
        assert!(!success, "{role}: {stderr}");
        assert!(
            stderr.starts_with("payload:ExecutableLayout"),
            "{role}: {stderr}"
        );
    }
}

#[test]
fn the_native_worker_role_takes_no_arguments() {
    // `mycelium-node` accepts `native-worker` only as the whole argv (`args.len() == 2`).
    let (success, stderr) = run(&["native-worker", "extra"]);
    assert!(!success);
    assert!(stderr.contains("no_arguments_accepted"), "{stderr}");
}

/// The cause of preview 9's failure was a worker role added to `mycelium-node` (the qualification
/// binary) that the packaged entry did not learn. Every role `mycelium-node` dispatches is either a
/// qualification / development entry (listed here, never shipped) or a worker role the packaged
/// executable must route too.
#[test]
fn every_worker_role_of_mycelium_node_is_routed_by_the_packaged_entry() {
    const NODE: &str = include_str!("../src/bin/mycelium-node.rs");
    const PACKAGED: &str = include_str!("../src/bin/mycelium.rs");
    const NOT_SHIPPED: [&str; 5] = [
        "qualify",
        "qualify-restart",
        "qualification-member",
        "desktop",
        "desktop-probe",
    ];
    let roles: Vec<&str> = NODE
        .lines()
        .filter_map(|line| line.trim_start().strip_prefix("Some(\""))
        .filter_map(|rest| rest.split('"').next())
        .collect();
    assert!(
        roles.contains(&"native-worker") && roles.contains(&"member"),
        "the scan found no roles: {roles:?}"
    );
    for role in roles.iter().filter(|r| !NOT_SHIPPED.contains(r)) {
        assert!(
            PACKAGED.contains(&format!("\"{role}\"")),
            "worker role `{role}` of mycelium-node is not routed by the packaged executable"
        );
    }
}
