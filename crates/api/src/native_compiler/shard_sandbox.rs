// SPDX-License-Identifier: AGPL-3.0-only
//! The shard worker's source-read fence is applied before exec, not after load.
//! This is local isolation, not remote admission or a sandbox against arbitrary
//! executable code. Other stage safetensors and both source/cache roots are denied.
use std::path::Path;

pub fn profile(shard: &Path, source: &Path, cache: &Path) -> Result<String, &'static str> {
    let quote = |path: &Path| -> Result<String, &'static str> {
        let s = path.to_str().ok_or("native_shard_path_invalid")?;
        if !path.is_absolute() || s.chars().any(|c| c.is_control()) {
            return Err("native_shard_path_invalid");
        }
        Ok(format!(
            "\"{}\"",
            s.replace('\\', "\\\\").replace('"', "\\\"")
        ))
    };
    Ok(format!(
        "(version 1)(allow default)(deny file-read* (subpath {})(subpath {})(regex #\".*\\.safetensors$\"))(allow file-read* (literal {}))",
        quote(source)?,
        quote(cache)?,
        quote(shard)?
    ))
}
