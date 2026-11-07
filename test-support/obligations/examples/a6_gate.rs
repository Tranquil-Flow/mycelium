// SPDX-License-Identifier: AGPL-3.0-only
//! Read-only Git-object source authentication. No Python/app/native execution.
#[path = "support/source_bytes.rs"]
mod source_bytes;
use obligations::{FilePin, Registry, a6, contracts};
use std::{collections::BTreeMap, path::PathBuf, process::ExitCode};
fn read_json<T: serde::de::DeserializeOwned>(path: PathBuf) -> Result<T, String> {
    serde_json::from_slice(&std::fs::read(path).map_err(|e| e.to_string())?)
        .map_err(|e| e.to_string())
}
fn verify_bodies(bytes: &[u8], tests: &serde_json::Value, path: &str) -> Result<usize, String> {
    let lines: Vec<_> = bytes.split_inclusive(|b| *b == b'\n').collect();
    let tests = tests.as_array().ok_or("body_list")?;
    for test in tests {
        let start = usize::try_from(test["line"].as_u64().ok_or("body_start")?)
            .map_err(|e| e.to_string())?;
        let end = usize::try_from(test["end_line"].as_u64().ok_or("body_end")?)
            .map_err(|e| e.to_string())?;
        if start == 0 || end < start || end > lines.len() {
            return Err("body_span".into());
        }
        let body = lines[start - 1..end].concat();
        let pin = FilePin {
            path: format!("{path}:{}", test["name"].as_str().ok_or("body_name")?),
            sha256: test["sha256"].as_str().ok_or("body_hash")?.into(),
            size_bytes: test["size_bytes"].as_u64().ok_or("body_size")?,
        };
        source_bytes::verify_bytes(&pin, &body)?;
    }
    Ok(tests.len())
}
fn run() -> Result<(), String> {
    let mut args = std::env::args_os().skip(1);
    let repo = PathBuf::from(
        args.next()
            .ok_or("usage: a6_gate <oracle-git-repo> [binding.json] [history.json]")?,
    );
    let binding: a6::Binding = if let Some(p) = args.next() {
        read_json(p.into())?
    } else {
        a6::checked_in_binding().map_err(|e| e.to_string())?
    };
    let history: Registry = if let Some(p) = args.next() {
        read_json(p.into())?
    } else {
        obligations::checked_in().map_err(|e| e.to_string())?.0
    };
    if args.next().is_some() {
        return Err("unexpected arguments".into());
    }
    let manifest = a6::accepted_manifest().map_err(|e| e.to_string())?;
    a6::validate_binding(&binding, &manifest, &history)
        .map_err(|e| format!("a6_binding: {e:?}"))?;
    let data = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("data");
    for pin in &binding.history_files {
        source_bytes::verify_bytes(
            pin,
            &std::fs::read(data.join(&pin.path)).map_err(|e| e.to_string())?,
        )?;
    }
    let raw = source_bytes::read_object(&repo, &binding.source_sha, &binding.manifest.path)?;
    source_bytes::verify_bytes(&binding.manifest, &raw)?;
    let actual: a6::Manifest = serde_json::from_slice(&raw).map_err(|e| e.to_string())?;
    a6::validate_binding(&binding, &actual, &history).map_err(|e| format!("a6_actual: {e:?}"))?;
    let contract = contracts::checked_in_binding().map_err(|e| e.to_string())?;
    let raw = source_bytes::read_object(&repo, &binding.source_sha, &contract.manifest.path)?;
    source_bytes::verify_bytes(&contract.manifest, &raw)?;
    let cm: contracts::Manifest = serde_json::from_slice(&raw).map_err(|e| e.to_string())?;
    contracts::validate_binding(&contract, &cm, &history)
        .map_err(|e| format!("contract: {e:?}"))?;
    let contract_objects =
        contracts::source_objects(&cm).map_err(|e| format!("contracts: {e:?}"))?;
    let mut objects: BTreeMap<String, FilePin> = BTreeMap::new();
    for p in &actual.files {
        let p = a6::file_pin(p)?;
        objects.insert(p.path.clone(), p);
    }
    let mut overlap = 0;
    for (path, pin) in &contract_objects {
        if let Some(old) = objects.insert(path.clone(), pin.clone()) {
            if old.sha256 != pin.sha256 || old.size_bytes != pin.size_bytes {
                return Err(format!("a6_contract_conflict: {path}"));
            }
            overlap += 1;
        }
    }
    // Retain only the small reviewed body cohort, not all source bytes.
    let mut body_sources = BTreeMap::new();
    for (path, pin) in &objects {
        let bytes = source_bytes::read_object(&repo, &binding.source_sha, path)?;
        source_bytes::verify_bytes(pin, &bytes)?;
        if binding
            .body_rows
            .iter()
            .any(|r| r["path"].as_str() == Some(path))
        {
            body_sources.insert(path.clone(), bytes);
        }
    }
    let mut bodies = 0;
    let mut historical_bodies = 0;
    let mut historical_objects = 0;
    for row in &binding.body_rows {
        let path = row["path"].as_str().ok_or("body_path")?;
        bodies += verify_bodies(
            body_sources.get(path).ok_or("body_source_missing")?,
            &row["source_tests"],
            path,
        )?;
        if !row["old_pin"].is_null() {
            let pin: FilePin =
                serde_json::from_value(row["old_pin"].clone()).map_err(|e| e.to_string())?;
            let bytes = source_bytes::read_object(&repo, contracts::HISTORICAL_SOURCE, path)?;
            source_bytes::verify_bytes(&pin, &bytes)?;
            historical_bodies += verify_bodies(&bytes, &row["old_source_tests"], path)?;
            historical_objects += 1;
        }
    }
    println!("{}",serde_json::to_string_pretty(&serde_json::json!({
        "scope":"additive-A6-source-identity-only", "source_sha":binding.source_sha,
        "final_a6_entries":actual.files.len(),"new_path_obligations":binding.paths.len(),
        "new_groups":binding.test_groups.len(),"new_test_bodies":binding.test_groups.iter().map(|g|g.evidence["source_tests"].as_array().map_or(0,Vec::len)).sum::<usize>(),
        "reviewed_current_bodies_authenticated":bodies,"historical_bodies_authenticated":historical_bodies,
        "historical_source_objects":historical_objects,"contract_objects":contract_objects.len(),
        "a6_contract_overlap":overlap,"distinct_final_source_objects_rehashed":objects.len(),
        "history_files_unchanged":binding.history_files.len(),"unmapped_final_a6_paths":0,
        "retirements":0,"runtime_qualification":false,"native_qualification":false
    })).map_err(|e|e.to_string())?);
    Ok(())
}
fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("a6-inventory: {e}");
            ExitCode::FAILURE
        }
    }
}

#[cfg(test)]
mod tests {
    use super::verify_bodies;
    use serde_json::{Value, json};
    fn body() -> Value {
        json!([{"name":"test_example","line":2,"end_line":2,"sha256":"edeaaff3f1774ad2888673770c6d64097e391bc362d7d6fb34982ddf0efd18cb","size_bytes":4}])
    }
    #[test]
    fn exact_body_span_accepts_without_executing_source() {
        assert_eq!(
            verify_bodies(b"header\nabc\nfooter\n", &body(), "test.py"),
            Ok(1)
        );
    }
    #[test]
    fn same_size_body_corruption_refuses() {
        assert!(
            verify_bodies(b"header\nabd\nfooter\n", &body(), "test.py")
                .unwrap_err()
                .starts_with("source_sha256:")
        );
    }
    #[test]
    fn body_size_and_span_controls_refuse() {
        for (start, end, size) in [(0, 2, 4), (3, 2, 4), (2, 9, 4), (2, 2, 5), (1, 1, 4)] {
            let mut b = body();
            b[0]["line"] = json!(start);
            b[0]["end_line"] = json!(end);
            b[0]["size_bytes"] = json!(size);
            assert!(verify_bodies(b"header\nabc\nfooter\n", &b, "test.py").is_err());
        }
    }
}
