// SPDX-License-Identifier: AGPL-3.0-only
//! Native-socket refusals against genuine compiler-emitted shards, after success.
use super::*;
use std::{
    io::{Read, Seek, SeekFrom, Write},
    os::{fd::OwnedFd, unix::net::UnixStream},
    process::{Command, Stdio},
    sync::atomic::AtomicBool,
};

struct ChildGuard(std::process::Child);
impl Drop for ChildGuard {
    fn drop(&mut self) {
        match self.0.try_wait() {
            Ok(Some(_)) => (),
            Ok(None) => {
                self.0.kill().expect("tamper worker kill");
            }
            Err(error) => panic!("tamper worker status: {error}"),
        }
        self.0.wait().expect("tamper worker reap");
    }
}
fn refused(prepare: &Value, source: &Path, expected: &str) {
    let binary = std::env::var("MYCELIUM_COMPILER_NATIVE_BINARY").unwrap();
    let checkpoint = Path::new(prepare["checkpoint"].as_str().unwrap());
    let cache = PathBuf::from(std::env::var("HOME").unwrap()).join(".cache/huggingface");
    let profile = api::native_compiler::shard_sandbox::profile(checkpoint, source, &cache).unwrap();
    let (client, server) = UnixStream::pair().unwrap();
    let child = ChildGuard(
        Command::new("/usr/bin/sandbox-exec")
            .args(["-p", &profile, &binary, "native-worker"])
            .stdin(Stdio::null())
            .stdout(Stdio::from(OwnedFd::from(server)))
            .spawn()
            .unwrap(),
    );
    let wall = std::time::Duration::from_millis(
        BudgetId::MemberChosenWall
            .span(Entries::ZERO)
            .unwrap()
            .millis(),
    );
    let poll = std::time::Duration::from_millis(
        BudgetId::RegistryPoll.span(Entries::ZERO).unwrap().millis(),
    );
    let mut link = api::native_worker_link::Link::new(client, wall).unwrap();
    let (reply, body) = link
        .request_until(
            prepare,
            &[],
            std::time::Instant::now() + wall,
            &AtomicBool::new(false),
            poll,
        )
        .unwrap();
    assert!(body.is_empty());
    assert_eq!(reply["code"], expected, "{reply}");
    let pid = child.0.id();
    drop(child);
    assert_eq!(
        rustix::process::test_kill_process(rustix::process::Pid::from_raw(pid as i32).unwrap()),
        Err(rustix::io::Errno::SRCH),
        "tamper child left running"
    );
    eprintln!("B6 shard tamper refused before load: {expected}; pid={pid} reaped");
}
pub(super) fn native_shard_tamper_refusals(root: &Path, plan: &Value, source: &Path) {
    use stage_contract::native_shard as contract;
    let reference: Value =
        serde_json::from_str(stage_contract::native_proof::QWEN3_PACKED_ORACLE).unwrap();
    let stage = &plan["stages"][0];
    let shard = &stage["shard"];
    let name = shard["transfer"]["path"]
        .as_str()
        .unwrap()
        .strip_prefix("deployment/")
        .unwrap();
    let path = root.join("stage-shards").join(name);
    let memory = BudgetId::PyFinalScriptsA13OperatorSingleMacUiRehearsalMaxMemoryBytesL171C55
        .bytes()
        .unwrap()
        .get();
    let prepare = json!({"command":contract::COMMAND,"checkpoint":path,"start":0,"end":14,"total":28,"memory":memory,"tokens":39,"id":stage["request_id"],"generation":stage["member_generation"],"shard":shard,
        "stage_image":{"model_id":"Qwen/Qwen3-0.6B","revision":reference["recorded"]["identity"]["revision"],"image_digest":reference["recorded"]["identity"]["checkpoint_sha256"],"tensor_manifest":reference["recorded"]["identity"]["manifest_sha256"],"profile":"packed_int8","layers":{"start":0,"end":14},"total_layers":28,"source_metadata":{"header_sha256":reference["recorded"]["identity"]["header_sha256"]}}});
    let mut file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(&path)
        .unwrap();
    let size = file.metadata().unwrap().len();
    let mut last = [0];
    file.seek(SeekFrom::Start(size - 1)).unwrap();
    file.read_exact(&mut last).unwrap();
    file.seek(SeekFrom::Start(size - 1)).unwrap();
    file.write_all(&[last[0] ^ 1]).unwrap();
    file.sync_all().unwrap();
    refused(&prepare, source, contract::HASH);
    file.seek(SeekFrom::Start(size - 1)).unwrap();
    file.write_all(&last).unwrap();
    file.sync_all().unwrap();
    file.set_len(size - 1).unwrap();
    refused(&prepare, source, contract::LENGTH);
    file.seek(SeekFrom::Start(size - 1)).unwrap();
    file.write_all(&last).unwrap();
    file.sync_all().unwrap();
    let other_name = plan["stages"][1]["shard"]["transfer"]["path"]
        .as_str()
        .unwrap()
        .strip_prefix("deployment/")
        .unwrap();
    let other = root.join("stage-shards").join(other_name);
    let temp = root.join("stage-shards/swap-owned");
    fs::rename(&path, &temp).unwrap();
    fs::rename(&other, &path).unwrap();
    fs::rename(&temp, &other).unwrap();
    refused(&prepare, source, contract::LENGTH);
    fs::rename(&path, &temp).unwrap();
    fs::rename(&other, &path).unwrap();
    fs::rename(&temp, &other).unwrap();
    let mut wrong = prepare.clone();
    wrong["shard"] = plan["stages"][1]["shard"].clone();
    refused(&wrong, source, contract::BINDING);
    let mut wrong = prepare.clone();
    wrong["shard"]["recipe"]["files"][0]["segments"][1]["sha256"] =
        json!(format!("sha256:{}", "0".repeat(64)));
    let recipe = &mut wrong["shard"]["recipe"];
    recipe.as_object_mut().unwrap().remove("node_plan_digest");
    let digest = protocol::canonical::digest(recipe).unwrap();
    recipe["node_plan_digest"] = json!(digest);
    wrong["shard"]["recipe_digest"] = json!(digest);
    assert_eq!(
        wrong["shard"]["transfer"], prepare["shard"]["transfer"],
        "transfer hash remains authentic"
    );
    refused(&wrong, source, contract::RECIPE);
}
