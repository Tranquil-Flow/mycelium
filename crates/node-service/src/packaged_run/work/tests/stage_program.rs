// SPDX-License-Identifier: AGPL-3.0-only
//! Lane BND (REPORT-laneTEN D2, REVIEW-laneTEN-astra F2): in the packaged app every stage worker,
//! the owner's own contributor's and a joined Mac's, is the program the composition ADMITTED
//! (`RunConfig::native_program`), never the development environment's
//! `MYCELIUM_COMPILER_NATIVE_BINARY` / `_SHA256`, which the shipped app never sets. The real publish
//! step runs through the real admission (`Proven`, `link::prepare`): both members receive
//! `prepare_native` over their work services. The "programs" are `/bin/sh` scripts that append
//! their name to a marker file and exit, so nothing native runs; the child process has the
//! development variables set to a third, validly pinned script.
use super::*;
use std::os::unix::fs::PermissionsExt;

const DIR: &str = "MYCELIUM_BND_PACKAGED_STAGE_DIR";

/// A stage "worker" that records `name`, then waits (at most 20 s) until BOTH stages have launched
/// before it exits: the owner's admission fails the publish at the first stage's refusal and
/// cancels the other's pending prepare, so a worker that exits at once can end the run before
/// the second stage launched (seen once: marker `["owner"]`).
fn script(dir: &Path, name: &str) -> PathBuf {
    let path = dir.join(name);
    let marker = dir.join("launched.txt");
    fs::write(
        &path,
        format!(
            "#!/bin/sh\necho {name} >> '{m}'\ni=0\nwhile [ \"$(wc -l < '{m}')\" -lt 2 ] && [ $i -lt 200 ]; do sleep 0.1; i=$((i+1)); done\nexit 3\n",
            m = marker.display()
        ),
    )
    .unwrap();
    fs::set_permissions(&path, fs::Permissions::from_mode(0o755)).unwrap();
    path
}
fn sha256(path: &Path) -> String {
    protocol::canonical::sha256_prefixed(&fs::read(path).unwrap())[7..].to_owned()
}
fn admitted(path: &Path) -> api::native_compiler::NativeProgram {
    api::native_compiler::NativeProgram {
        binary: path.to_owned(),
        expected: api::compiled_handoff::Pin {
            bytes: fs::metadata(path).unwrap().len(),
            sha256: sha256(path),
        },
    }
}

#[test]
fn both_packaged_stages_launch_the_admitted_program_whatever_the_environment_names() {
    let dir = std::env::temp_dir().join(format!(
        "bnd-packaged-stage-{}",
        crate::service::random_id().unwrap()
    ));
    fs::create_dir(&dir).unwrap();
    let dir = dir.canonicalize().unwrap();
    for name in ["owner", "member"] {
        script(&dir, name);
    }
    let environment = script(&dir, "environment");
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "packaged_run::work::tests::stage_program::packaged_stage_program_child",
            "--ignored",
            "--nocapture",
            "--test-threads",
            "1",
        ])
        .env(DIR, &dir)
        .env("MYCELIUM_COMPILER_NATIVE_BINARY", &environment)
        .env("MYCELIUM_COMPILER_NATIVE_SHA256", sha256(&environment))
        .output()
        .unwrap();
    let text = String::from_utf8_lossy(&output.stdout).into_owned()
        + &String::from_utf8_lossy(&output.stderr);
    assert!(output.status.success(), "{text}");
    assert!(text.contains("1 passed"), "{text}");
    let mut launched: Vec<String> = fs::read_to_string(dir.join("launched.txt"))
        .unwrap_or_default()
        .lines()
        .map(str::to_owned)
        .collect();
    launched.sort();
    assert_eq!(
        launched,
        ["member", "owner"],
        "each host launched its own admitted program once, the environment's never: {text}"
    );
    fs::remove_dir_all(&dir).unwrap();
}

#[test]
#[ignore = "child of both_packaged_stages_launch_the_admitted_program_* (development variables set)"]
fn packaged_stage_program_child() {
    let dir = PathBuf::from(std::env::var_os(DIR).unwrap());
    let mut f = Fixture::with_programs(
        WorkNetwork::loopback(),
        WorkNetwork::loopback(),
        Some(admitted(&dir.join("owner"))),
        Some(admitted(&dir.join("member"))),
    );
    let mut ports = PackagedChainPorts::new(&f.host, None, None);
    let mut state = run_state(&mut f, &ports);
    let mut env = Env { at: Instant::now() };
    ports
        .work
        .compose(&state, f.plan(&state), Arc::new(NoCodec), &mut env)
        .unwrap();
    let wall = BudgetId::MemberChosenWall
        .span(Entries::ZERO)
        .unwrap()
        .millis()
        / 1000;
    let proposal = json!({"max_combined_rss_bytes":2_147_483_648u64,"max_additional_disk_bytes":4_000_000_000u64,"max_wall_seconds":wall});
    state.proposal = Some(proposal);
    use owner_run::steps::model::{ModelStepOutput, PreparedModel, PreparedSource};
    state.model = Some(ModelStepOutput {
        forced_slice: false,
        model_id: "Qwen/Qwen3-0.6B".into(),
        resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
        manifest_digest: "sha256:00".into(),
        slice_mode: true,
        choice: None,
        prepared: PreparedSource {
            model: PreparedModel {
                model_id: "Qwen/Qwen3-0.6B".into(),
                resolved_commit: "c1899de289a04d12100db370d81485cdf75e47ca".into(),
                architecture: "qwen3".into(),
                label: "constructor-only inert source fixture".into(),
                choice: None,
                shape_registered: true,
                owner_storage_need: 0,
            },
            manifest_digest: "sha256:00".into(),
            source_bytes: json!(0),
        },
    });
    let slices = ports.slices();
    owner_run::steps::slices::run_slices_step(
        &mut env,
        slices.reports,
        slices.slices,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    )
    .unwrap();
    f.agent.tick(&f.member);
    ports.work.shares_ready().unwrap();
    // The real admission: `prepare_native` to both stages over their work services.
    let publication = ports.publication();
    let outcome = owner_run::steps::publish::run_publish_step(
        &mut env,
        publication.reports,
        publication.workflow,
        &mut state,
        span(BudgetId::OwnerPoll).unwrap().as_secs_f64(),
    );
    eprintln!("publish over script workers: {outcome:?}");
    let until = Instant::now() + span(BudgetId::RouterClose).unwrap() * 4;
    loop {
        let flow = ports.work.workflow_state().unwrap();
        if flow["state"] == "failed" {
            eprintln!("workflow: {flow}");
            break;
        }
        assert!(Instant::now() < until, "{flow}");
        std::thread::sleep(span(BudgetId::OwnerPoll).unwrap());
    }
    ports.close_work().unwrap();
}
