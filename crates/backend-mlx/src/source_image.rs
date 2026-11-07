// SPDX-License-Identifier: AGPL-3.0-only
//! CPU-only immutable dense source verification, also used by the native loader.
use crate::admission::{artifact_layout, digest_hex};
use sha2::{Digest, Sha256};
use stage_contract::*;
use std::{fs::File, io::Read, path::Path};

pub fn verify_artifact(path: &Path, d: &StageDescriptor) -> Result<()> {
    let io = || Error::new(ErrorCode::Artifact, "checkpoint-read");
    let mut file = File::open(path).map_err(|_| io())?;
    let (expected_length, header_pin) = artifact_layout(d)?;
    if file.metadata().map_err(|_| io())?.len() != expected_length {
        return Err(Error::new(ErrorCode::Artifact, "checkpoint-length"));
    }
    let mut length = [0; 8];
    file.read_exact(&mut length).map_err(|_| io())?;
    let n = u64::from_le_bytes(length);
    if n > 1_048_576 {
        return Err(Error::new(ErrorCode::Artifact, "header-cap"));
    }
    let mut header = vec![0; n as usize];
    file.read_exact(&mut header).map_err(|_| io())?;
    let header_hash: [u8; 32] = Sha256::digest(&header).into();
    if header_hash != digest_hex(header_pin)? {
        return Err(Error::new(ErrorCode::Artifact, "manifest-digest"));
    }
    let mut hash = Sha256::new();
    hash.update(length);
    hash.update(&header);
    let mut chunk = vec![0; 1024 * 1024];
    loop {
        let n = file.read(&mut chunk).map_err(|_| io())?;
        if n == 0 {
            break;
        }
        hash.update(&chunk[..n]);
    }
    let actual: [u8; 32] = hash.finalize().into();
    if actual != d.checkpoint {
        return Err(Error::new(ErrorCode::Artifact, "checkpoint-digest"));
    }
    Ok(())
}
