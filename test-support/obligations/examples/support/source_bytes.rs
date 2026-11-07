// SPDX-License-Identifier: AGPL-3.0-only
//! Offline byte authentication using the host SHA-256 utility, not a new hash implementation.
use obligations::FilePin;
use std::io::Write;
use std::process::{Command, Stdio};

pub fn verify_bytes(pin: &FilePin, bytes: &[u8]) -> Result<(), String> {
    if bytes.len() as u64 != pin.size_bytes {
        return Err(format!("source_size: {}", pin.path));
    }
    let mut child = Command::new("/usr/bin/shasum")
        .args(["-a", "256"])
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LC_ALL", "C")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("hash_spawn: {e}"))?;
    let write = child
        .stdin
        .take()
        .ok_or("hash_stdin_missing".to_owned())
        .and_then(|mut stdin| {
            stdin
                .write_all(bytes)
                .map_err(|e| format!("hash_write: {e}"))
        });
    // Reap even after a broken input pipe; no detached helper on error.
    let output = child
        .wait_with_output()
        .map_err(|e| format!("hash_wait: {e}"))?;
    write?;
    if !output.status.success() {
        return Err(format!("hash_exit: {}", output.status));
    }
    let text = std::str::from_utf8(&output.stdout).map_err(|e| format!("hash_output: {e}"))?;
    if text.split_whitespace().next() != Some(pin.sha256.as_str()) {
        return Err(format!("source_sha256: {}", pin.path));
    }
    Ok(())
}

pub fn read_object(repo: &std::path::Path, source: &str, path: &str) -> Result<Vec<u8>, String> {
    if path.is_empty()
        || path.starts_with('/')
        || path.contains(['\\', '\0', ':'])
        || path
            .split('/')
            .any(|p| p.is_empty() || p == "." || p == "..")
        || source.len() != 40
        || !source.bytes().all(|b| b.is_ascii_hexdigit())
    {
        return Err(format!("source_path: {path}"));
    }
    let output = Command::new("/usr/bin/git")
        .arg("-C")
        .arg(repo)
        .args(["cat-file", "blob", &format!("{source}:{path}")])
        .env_clear()
        .env("PATH", "/usr/bin:/bin")
        .env("LC_ALL", "C")
        .output()
        .map_err(|e| format!("source_read: {e}"))?;
    if !output.status.success() {
        return Err(format!("source_missing: {path}"));
    }
    Ok(output.stdout)
}
