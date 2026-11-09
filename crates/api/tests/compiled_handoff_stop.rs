// SPDX-License-Identifier: AGPL-3.0-only
//! Cooperative Stop inside `CompiledHandoff` hashing (REVIEW-laneB6 F1). The
//! pinned file is real and multi-chunk; no handoff is fabricated. The stop is
//! observed inside that file's hash, before any document is parsed; without a
//! Stop the same root is refused as an invalid (incomplete) handoff.
use api::compiled_handoff::{CompiledHandoff, Pin, Pins};
use sha2::{Digest, Sha256};
use std::{cell::Cell, fs, path::PathBuf};

struct Root(PathBuf);
impl Root {
    fn new(tag: &str) -> Self {
        let path = std::env::temp_dir().join(format!("b10-handoff-{tag}-{}", std::process::id()));
        fs::create_dir(&path).expect("fresh fixture root");
        Self(path.canonicalize().unwrap())
    }
}
impl Drop for Root {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.0).expect("remove owned fixture root");
    }
}

fn pinned(root: &Root, name: &str, length: usize) -> Pins {
    let bytes: Vec<u8> = (0..length).map(|i| (i % 251) as u8).collect();
    fs::write(root.0.join(name), &bytes).unwrap();
    let mut pins = Pins::new();
    pins.insert(
        name.into(),
        Pin {
            bytes: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(&bytes)),
        },
    );
    pins
}

#[test]
fn a_stop_inside_a_pinned_hash_is_a_stop_not_an_invalid_handoff() {
    let root = Root::new("stop");
    // Three full 1 MiB chunks and a short tail: the first file `open` checks.
    let pins = pinned(&root, "compile-start.private.json", 3 * (1 << 20) + 17);
    let calls = Cell::new(0_u32);
    let counted = |stop_after: Option<u32>| {
        let calls = &calls;
        move || {
            calls.set(calls.get() + 1);
            stop_after.is_some_and(|n| calls.get() > n)
        }
    };
    let never = counted(None);
    let refusal = CompiledHandoff::open_until(&root.0, &pins, &never)
        .err()
        .unwrap();
    assert_eq!(refusal.code, "compiled_handoff_invalid", "incomplete root");
    assert!(
        calls.get() >= 4,
        "every chunk consults Stop: {}",
        calls.get()
    );
    calls.set(0);
    let after_one_chunk = counted(Some(1));
    let refusal = CompiledHandoff::open_until(&root.0, &pins, &after_one_chunk)
        .err()
        .unwrap();
    assert_eq!(refusal.code, "compiled_handoff_stopped");
    assert_eq!(calls.get(), 2, "stopped inside the file after one chunk");
    let refusal = CompiledHandoff::open_until(&root.0, &pins, &|| true)
        .err()
        .unwrap();
    assert_eq!(refusal.code, "compiled_handoff_stopped");
}
