// SPDX-License-Identifier: AGPL-3.0-only
//! Exercise the exact pre-exec fence used by native shard workers, without a GPU.
#[cfg(target_os = "macos")]
#[test]
fn shard_worker_can_read_only_its_stage_not_source_cache_or_other_stage() {
    use std::{fs, process::Command};
    let root = std::env::temp_dir().join(format!("b6-sandbox-{}", std::process::id()));
    fs::create_dir(&root).unwrap();
    let root = root.canonicalize().unwrap();
    let source = root.join("source");
    let cache = root.join("cache");
    fs::create_dir(&source).unwrap();
    fs::create_dir(&cache).unwrap();
    let stage = root.join("stage.safetensors");
    let other = root.join("other.safetensors");
    let full = source.join("model.safetensors");
    let blob = cache.join("unlabelled-blob");
    for path in [&stage, &other, &full, &blob] {
        fs::write(path, b"stage-data").unwrap();
    }
    let profile = api::native_compiler::shard_sandbox::profile(&stage, &source, &cache).unwrap();
    for (path, allowed) in [
        (&stage, true),
        (&other, false),
        (&full, false),
        (&blob, false),
    ] {
        let out = Command::new("/usr/bin/sandbox-exec")
            .args(["-p", &profile, "/bin/cat"])
            .arg(path)
            .output()
            .unwrap();
        assert_eq!(
            out.status.success(),
            allowed,
            "{}: {}",
            path.display(),
            String::from_utf8_lossy(&out.stderr)
        );
        if allowed {
            assert_eq!(out.stdout, b"stage-data");
        } else {
            assert!(out.stdout.is_empty());
        }
    }
    fs::remove_dir_all(root).unwrap();
}
