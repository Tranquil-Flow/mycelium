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
#[path = "support/staged_bundle.rs"]
mod staged;
use std::{
    path::Path,
    process::{Command, Stdio},
};

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

/// Lane BND (REPORT-laneTEN D1, review F3): the route's codec starts `<binary> tokenize
/// <tokenizer.json>` (`api::joined_owner::codec::NativeCodec::start`), and in the shipped app
/// `<binary>` is this executable. Outside a bundle the role stops at the same admission as every
/// other worker role, instead of being rejected as an unknown argument.
#[test]
fn the_tokenize_role_is_routed_to_admission_not_rejected_as_an_argument() {
    let (success, stderr) = run(&["tokenize", "/nonexistent/tokenizer.json"]);
    assert!(!success, "an unadmitted tokenizer must not run: {stderr}");
    assert!(
        !stderr.contains("no_arguments_accepted"),
        "tokenize was rejected as an unknown argument: {stderr}"
    );
    assert!(
        stderr.starts_with("payload:ExecutableLayout"),
        "tokenize did not stop at bundle admission: {stderr}"
    );
}

/// `mycelium-node` accepts `tokenize` only with exactly one tokenizer path (`args.len() == 3`).
#[test]
fn the_tokenize_role_takes_exactly_one_tokenizer() {
    for args in [&["tokenize"][..], &["tokenize", "a.json", "extra"][..]] {
        let (success, stderr) = run(args);
        assert!(!success, "{args:?}");
        assert!(
            stderr.contains("no_arguments_accepted"),
            "{args:?}: {stderr}"
        );
    }
}

/// Lane BND: inside a bundle-shaped directory holding exactly the pinned payload
/// (`support/staged_bundle.rs`), the packaged executable's `tokenize` role passes worker admission
/// and answers the codec protocol on the real Qwen3-0.6B `tokenizer.json` exactly as
/// `mycelium-node tokenize` does: the reference prompt's ids and the startup challenge's four ids
/// `[151667, 198, 32313, 11]` decoded. Unknown arguments are still rejected there. The staged run
/// has no `DYLD_*` variable: it loads the staged `Contents/Frameworks` through its own rpath
/// (`@executable_path/../Frameworks`).
#[test]
#[ignore = "MYCELIUM_PINNED_PAYLOAD_APP (an app holding the pinned payload) + this Mac's cached Qwen3-0.6B"]
fn inside_an_admitted_bundle_the_tokenize_role_answers_as_mycelium_node_does() {
    let dir = std::env::temp_dir()
        .canonicalize()
        .unwrap()
        .join(format!("bnd-tokenize-{}", std::process::id()));
    std::fs::create_dir(&dir).unwrap();
    /// The staged bundle goes whatever the outcome (its payload copies are APFS clones).
    struct Scratch(std::path::PathBuf);
    impl Drop for Scratch {
        fn drop(&mut self) {
            if let Err(e) = std::fs::remove_dir_all(&self.0) {
                eprintln!("staged bundle cleanup failed: {e}");
            }
        }
    }
    let _scratch = Scratch(dir.clone());
    let exe = staged::stage(Path::new(EXE), &staged::payload_app(), &dir);
    let tokenizer = Path::new(&std::env::var_os("HOME").unwrap())
        .join(".cache/huggingface/hub/models--Qwen--Qwen3-0.6B/snapshots")
        .join("c1899de289a04d12100db370d81485cdf75e47ca/tokenizer.json");
    let oracle: serde_json::Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let prompt = "<|im_start|>system\nYou are a helpful assistant.<|im_end|>\n<|im_start|>user\nExplain how rain forms and why plants need it. Give a detailed answer.<|im_end|>\n<|im_start|>assistant\n";
    const CHALLENGE: [u32; 4] = [151667, 198, 32313, 11];
    // The helper protocol `NativeCodec` speaks (`node_service::tokenize::serve`): one JSON line
    // per request on stdin, one per answer on stdout.
    let mut requests = vec![serde_json::json!({"encode": prompt})];
    requests.extend(CHALLENGE.iter().map(|id| serde_json::json!({"decode": id})));
    let answers = |binary: &Path, dyld: bool| -> (String, Vec<serde_json::Value>) {
        use std::io::Write as _;
        let mut command = Command::new(binary);
        command
            .arg("tokenize")
            .arg(&tokenizer)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if !dyld {
            // The staged executable then loads only its own staged `Contents/Frameworks`.
            command
                .env_remove("DYLD_LIBRARY_PATH")
                .env_remove("DYLD_FALLBACK_LIBRARY_PATH");
        }
        let mut child = command.spawn().unwrap();
        let mut input = child.stdin.take().unwrap();
        for request in &requests {
            writeln!(input, "{request}").unwrap();
        }
        drop(input);
        let output = child.wait_with_output().unwrap();
        let stdout = String::from_utf8(output.stdout).unwrap();
        assert!(
            output.status.success(),
            "{}: {}",
            binary.display(),
            String::from_utf8_lossy(&output.stderr)
        );
        let parsed = stdout
            .lines()
            .map(|l| serde_json::from_str(l).unwrap())
            .collect();
        (stdout, parsed)
    };
    let (packaged, replies) = answers(&exe, false);
    let (node, _) = answers(Path::new(env!("CARGO_BIN_EXE_mycelium-node")), true);
    println!("BND tokenize packaged={packaged:?}");
    assert_eq!(
        packaged, node,
        "the packaged role answers byte for byte as mycelium-node does"
    );
    assert_eq!(replies.len(), 5, "{packaged}");
    assert_eq!(
        replies[0]["ids"],
        oracle["recorded"]["workload"]["prompt_ids"]
    );
    let pieces: Vec<&str> = replies[1..]
        .iter()
        .map(|r| r["text"].as_str().unwrap())
        .collect();
    assert_eq!(pieces, ["<think>", "\n", "Okay", ","]);
    for args in [&["extra"][..], &["tokenize"][..]] {
        let output = Command::new(&exe)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .output()
            .unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(!output.status.success(), "{args:?}");
        assert!(
            stderr.contains("no_arguments_accepted"),
            "{args:?}: {stderr}"
        );
    }
}
