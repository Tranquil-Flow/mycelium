// SPDX-License-Identifier: AGPL-3.0-only
//! Content-addressed view of the compiler's named stage shards; no second byte allocation.
use super::*;
pub(super) struct View {
    pub path: PathBuf,
    pub pinned: Arc<runtime::artifact_journal::PinnedObjects>,
}
pub(super) fn view(root: &Path, document: &Value) -> Result<View, StepError> {
    use std::{
        fs,
        os::unix::fs::{DirBuilderExt, MetadataExt, symlink},
    };
    let invalid = || refused("compiler_objects_invalid");
    let shards = root.join("stage-shards");
    let metadata = fs::symlink_metadata(&shards).map_err(|_| invalid())?;
    if !metadata.is_dir() || metadata.file_type().is_symlink() {
        return Err(invalid());
    }
    let nodes = document["node_files"].as_object().ok_or_else(invalid)?;
    if nodes.is_empty() || nodes.len() > planner::chain_compile::MAX_HOSTS {
        return Err(invalid());
    }
    let mut paths = BTreeMap::new();
    let mut pinned = runtime::artifact_journal::PinnedObjects::new();
    for rows in nodes.values() {
        let rows = rows
            .as_array()
            .filter(|r| r.len() == 1)
            .ok_or_else(invalid)?;
        let row = &rows[0];
        let name = row["path"]
            .as_str()
            .and_then(|s| s.strip_prefix("deployment/"))
            .filter(|s| {
                !s.is_empty()
                    && *s != "."
                    && *s != ".."
                    && s.bytes()
                        .all(|b| b.is_ascii_alphanumeric() || b"._-".contains(&b))
            })
            .ok_or_else(invalid)?;
        let hash = row["content_digest"]
            .as_str()
            .and_then(|s| s.strip_prefix("sha256:"))
            .filter(|s| {
                s.len() == 64
                    && s.bytes()
                        .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
            })
            .ok_or_else(invalid)?;
        let source = shards.join(name);
        let metadata = fs::symlink_metadata(&source).map_err(|_| invalid())?;
        if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.nlink() != 1 {
            return Err(invalid());
        }
        pinned
            .admit(hash, &source, row["size_bytes"].as_u64())
            .map_err(|_| invalid())?;
        paths.entry(hash.to_owned()).or_insert(source);
    }
    let objects = root.join("work-objects");
    let metadata = match fs::DirBuilder::new().mode(0o700).create(&objects) {
        Ok(()) => fs::symlink_metadata(&objects),
        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => fs::symlink_metadata(&objects),
        Err(_) => return Err(invalid()),
    }
    .map_err(|_| invalid())?;
    if !metadata.is_dir()
        || metadata.file_type().is_symlink()
        || metadata.mode() & 0o7777 != 0o700
        || metadata.uid() != rustix::process::getuid().as_raw()
    {
        return Err(invalid());
    }
    for (hash, source) in paths {
        let alias = objects.join(hash);
        match symlink(&source, &alias) {
            Ok(()) => {}
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                if fs::read_link(&alias).map_err(|_| invalid())? != source {
                    return Err(invalid());
                }
            }
            Err(_) => return Err(invalid()),
        }
    }
    // The view retains original descriptors through OwnerWork and the repeated reader.
    Ok(View {
        path: objects,
        pinned: Arc::new(pinned),
    })
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
        let alias = objects.path.join(digest.strip_prefix("sha256:").unwrap());
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
