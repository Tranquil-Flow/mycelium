// SPDX-License-Identifier: AGPL-3.0-only
//! Content-addressed view of the compiler's named stage shards; no second byte allocation.
use super::*;
pub(super) fn view(root: &Path, _: &Value) -> Result<PathBuf, StepError> {
    Ok(root.join("stage-shards"))
}
#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::MetadataExt};
    #[test]
    fn named_compiler_shards_resolve_by_digest_without_copying_or_hardlinks() {
        let root = std::env::temp_dir().join(format!(
            "s4cb2-objects-{}",
            crate::service::random_id().unwrap()
        ));
        fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let shard = root.join("stage-shards");
        fs::create_dir(&shard).unwrap();
        let name = "stage-0.safetensors";
        let content = b"inert model-free compiler-layout fixture";
        fs::write(shard.join(name), content).unwrap();
        let digest = protocol::canonical::sha256_prefixed(content);
        let document = json!({"node_files":{"node-0":[{"path":format!("deployment/{name}"),"content_digest":digest}]}});
        let objects = view(&root, &document).unwrap();
        let alias = objects.join(digest.strip_prefix("sha256:").unwrap());
        assert_eq!(
            fs::read(&alias).ok().as_deref(),
            Some(content.as_slice()),
            "compiler filenames are not content-addressed source objects"
        );
        assert_eq!(
            fs::metadata(shard.join(name)).unwrap().nlink(),
            1,
            "do not invalidate compiler pins with a hardlink"
        );
        assert!(
            fs::symlink_metadata(alias)
                .unwrap()
                .file_type()
                .is_symlink(),
            "no second reserved byte allocation"
        );
        fs::remove_dir_all(root).unwrap();
    }
}
