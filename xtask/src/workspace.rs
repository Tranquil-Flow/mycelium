// SPDX-License-Identifier: AGPL-3.0-only
use crate::lint::Source;
use std::{fs, path::Path};

/// Read every source below the workspace, including cfg-disabled code, tests,
/// build scripts and untracked additions. Generated build output and this
/// tool's intentionally invalid fixtures are the only source exclusions.
pub fn sources(root: &Path) -> Result<Vec<Source>, String> {
    if !root.join("Cargo.toml").is_file() {
        return Err("WORKSPACE: missing root Cargo.toml".into());
    }
    let mut result = Vec::new();
    collect(root, root, &mut result)?;
    result.sort_by(|a, b| a.path.cmp(&b.path));
    Ok(result)
}
fn collect(root: &Path, directory: &Path, result: &mut Vec<Source>) -> Result<(), String> {
    let entries = fs::read_dir(directory)
        .map_err(|e| format!("WORKSPACE: read directory failed ({:?})", e.kind()))?;
    for entry in entries {
        let entry = entry.map_err(|e| format!("WORKSPACE: read entry failed ({:?})", e.kind()))?;
        let path = entry.path();
        let relative = path
            .strip_prefix(root)
            .map_err(|e| format!("WORKSPACE: {e}"))?;
        let name = relative
            .to_str()
            .ok_or("WORKSPACE: non-UTF-8 source path")?
            .replace('\\', "/");
        if entry.file_name() == ".git" || entry.file_name() == "target" || name == "xtask/fixtures"
        {
            continue;
        }
        let kind = entry
            .file_type()
            .map_err(|e| format!("{name}:1: IO: {:?}", e.kind()))?;
        if kind.is_symlink() {
            return Err(format!(
                "{name}:1: WORKSPACE: symlinks need explicit source inventory; refusing silent omission"
            ));
        }
        if kind.is_dir() {
            collect(root, &path, result)?;
        } else if path.extension().is_some_and(|extension| extension == "rs") {
            let text =
                fs::read_to_string(&path).map_err(|e| format!("{name}:1: IO: {:?}", e.kind()))?;
            result.push(Source { path: name, text });
        }
    }
    Ok(())
}
