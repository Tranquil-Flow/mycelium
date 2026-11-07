// SPDX-License-Identifier: AGPL-3.0-only
//! Offline accepted static/source-resource gate. Never executes oracle code.
#[path = "support/source_bytes.rs"]
mod source_bytes;
#[path = "support/static_consumer.rs"]
mod static_consumer;
use obligations::{package, static_source as s};
use std::{path::PathBuf, process::ExitCode};
fn optional_json<T: serde::de::DeserializeOwned>(
    arg: Option<std::ffi::OsString>,
    default: impl FnOnce() -> Result<T, serde_json::Error>,
) -> Result<T, String> {
    if let Some(path) = arg {
        serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
            .map_err(|e| e.to_string())
    } else {
        default().map_err(|e| e.to_string())
    }
}
fn run() -> Result<(), String> {
    let mut args = std::env::args_os().skip(1);
    let repo = PathBuf::from(args.next().ok_or(
        "usage: static_gate <oracle-git> [binding.json] [manifest.json] [catalogue.json]",
    )?);
    let binding = optional_json(args.next(), s::checked_in_binding)?;
    let manifest = optional_json(args.next(), s::accepted_manifest)?;
    let census = optional_json(args.next(), || Ok(package::checked_in_package()?.1))?;
    if args.next().is_some() {
        return Err("unexpected arguments".into());
    }
    let history = obligations::checked_in().map_err(|e| e.to_string())?.0;
    let data = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data");
    let receipt = static_consumer::authenticate(
        &binding,
        &manifest,
        &history,
        &census,
        |pin| std::fs::read(data.join(&pin.path)).map_err(|e| format!("history_read: {e}")),
        |source, path| source_bytes::read_object(&repo, source, path),
    )?;
    println!(
        "{}",
        serde_json::to_string_pretty(&receipt).map_err(|e| e.to_string())?
    );
    Ok(())
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("static-source: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;
    fn inputs() -> (
        s::Binding,
        s::Manifest,
        obligations::Registry,
        package::PackageCensus,
    ) {
        (
            s::checked_in_binding().unwrap(),
            s::accepted_manifest().unwrap(),
            obligations::checked_in().unwrap().0,
            package::checked_in_package().unwrap().1,
        )
    }
    #[test]
    fn corrupt_metadata_refuses_before_any_history_or_bulk_reads() {
        for mode in 0..8 {
            let (mut b, mut m, h, mut c) = inputs();
            match mode {
                0 => b.source_sha = h.source_sha.clone(),
                1 => m.files[0].sha256 = "0".repeat(64),
                2 => m.files[0].size_bytes += 1,
                3 => m.files[0].path = "../escape".into(),
                4 => {
                    m.packaged_source_paths.pop();
                }
                5 => {
                    b.crosswalk.pop();
                }
                6 => c.source_objects[0].sha256 = "0".repeat(64),
                _ => b.history_files[0].sha256 = "0".repeat(64),
            }
            let calls = Cell::new(0);
            let result = static_consumer::authenticate(
                &b,
                &m,
                &h,
                &c,
                |_| {
                    calls.set(calls.get() + 1);
                    Err("unexpected history read".into())
                },
                |_, _| {
                    calls.set(calls.get() + 1);
                    Err("unexpected source read".into())
                },
            );
            assert!(result.unwrap_err().starts_with("static_"), "mode {mode}");
            assert_eq!(calls.get(), 0, "mode {mode}");
        }
    }
    #[test]
    fn catalogue_raw_bytes_are_authenticated_before_bulk_source_reads() {
        let (b, m, h, c) = inputs();
        let data = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data");
        let calls = Cell::new(0);
        let error = static_consumer::authenticate(
            &b,
            &m,
            &h,
            &c,
            |pin| {
                let mut bytes = std::fs::read(data.join(&pin.path)).map_err(|e| e.to_string())?;
                if pin.path == "package-census.json" {
                    bytes[0] ^= 1;
                }
                Ok(bytes)
            },
            |_, _| {
                calls.set(calls.get() + 1);
                Err("unexpected source read".into())
            },
        )
        .unwrap_err();
        assert_eq!(error, "source_sha256: package-census.json");
        assert_eq!(calls.get(), 0);
    }
    #[test]
    fn actual_source_size_and_digest_refuse_at_first_read() {
        let (b, m, h, c) = inputs();
        let data = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data");
        let first = s::source_objects(&m).unwrap().into_values().next().unwrap();
        for same_size in [false, true] {
            let calls = Cell::new(0);
            let error = static_consumer::authenticate(
                &b,
                &m,
                &h,
                &c,
                |pin| std::fs::read(data.join(&pin.path)).map_err(|e| e.to_string()),
                |_, path| {
                    calls.set(calls.get() + 1);
                    assert_eq!(path, first.path);
                    Ok(if same_size {
                        vec![0; usize::try_from(first.size_bytes).unwrap()]
                    } else {
                        Vec::new()
                    })
                },
            )
            .unwrap_err();
            assert!(error.starts_with(if same_size {
                "source_sha256:"
            } else {
                "source_size:"
            }));
            assert_eq!(calls.get(), 1);
        }
    }
    #[test]
    fn missing_source_object_error_is_not_swallowed() {
        let (b, m, h, c) = inputs();
        let data = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data");
        let calls = Cell::new(0);
        let error = static_consumer::authenticate(
            &b,
            &m,
            &h,
            &c,
            |pin| std::fs::read(data.join(&pin.path)).map_err(|e| e.to_string()),
            |_, _| {
                calls.set(calls.get() + 1);
                Err("source_missing: actual-path".into())
            },
        )
        .unwrap_err();
        assert_eq!(error, "source_missing: actual-path");
        assert_eq!(calls.get(), 1);
    }
}
