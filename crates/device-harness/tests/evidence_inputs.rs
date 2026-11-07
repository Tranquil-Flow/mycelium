// SPDX-License-Identifier: AGPL-3.0-only
use device_harness::evidence_inputs::{Content, Inputs, Pins};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn identity(bytes: &[u8]) -> Content {
    Content {
        size_bytes: bytes.len() as u64,
        sha256: format!("{:x}", Sha256::digest(bytes)),
    }
}
fn documents() -> (Value, Value) {
    let reference = |name: &str| json!({"path":format!("/fixture/{name}"),"path_base":"absolute","size_bytes":1,"sha256":"a".repeat(64)});
    (
        json!({"schema":"mycelium.foundation.native-input-evidence.v1", "rust_candidate":{"source_commit":"b".repeat(40),"packaged_executable":null},
        "native_inputs":[reference("libmlxc.dylib"),reference("libmlx.dylib"),reference("libjaccl.dylib"),reference("mlx.metallib")]}),
        json!({"schema":"mycelium.foundation.oracle-evidence.v1", "rust_candidate_source":"b".repeat(40), "accepted_source":{"commit":"c".repeat(40)},
        "certified_executable":reference("oracle"),"published_zip":reference("oracle.zip")}),
    )
}
fn parse(n: Value, o: Value) -> device_harness::Result<Inputs> {
    let n = serde_json::to_vec(&n).unwrap();
    let o = serde_json::to_vec(&o).unwrap();
    let pins = Pins {
        native: identity(&n),
        oracle: identity(&o),
    };
    Inputs::parse(&n, &o, pins)
}
#[test]
fn pinned_discovery_binds_sources_and_full_four_resource_closure_without_package_claim() {
    let (n, o) = documents();
    let input = parse(n, o).unwrap();
    assert_eq!(input.resources().len(), 4);
    assert_eq!(
        input.resources()[2].destination,
        "Contents/Frameworks/libjaccl.dylib"
    );
    assert_eq!(input.rust_source(), "b".repeat(40));
    assert_eq!(input.oracle_source(), "c".repeat(40));
    assert_eq!(input.oracle_executable().content.size_bytes, 1);
}
#[test]
fn tamper_bad_schema_missing_transitive_input_duplicate_and_source_drift_refuse() {
    let (n, o) = documents();
    let nb = serde_json::to_vec(&n).unwrap();
    let ob = serde_json::to_vec(&o).unwrap();
    let pins = Pins {
        native: identity(&nb),
        oracle: identity(&ob),
    };
    let mut changed = nb.clone();
    changed.push(b' ');
    assert!(Inputs::parse(&changed, &ob, pins).is_err());
    for mutation in ["schema", "missing", "duplicate", "source", "path", "hash"] {
        let (mut n, mut o) = documents();
        match mutation {
            "schema" => n["schema"] = json!("unknown"),
            "missing" => {
                n["native_inputs"].as_array_mut().unwrap().remove(2);
            }
            "duplicate" => n["native_inputs"][2] = n["native_inputs"][1].clone(),
            "source" => o["rust_candidate_source"] = json!("d".repeat(40)),
            "path" => n["native_inputs"][0]["path"] = json!("/fixture/../libmlxc.dylib"),
            _ => n["native_inputs"][0]["sha256"] = json!("bogus"),
        }
        assert!(parse(n, o).is_err(), "{mutation}");
    }
}
#[test]
fn metallib_destination_is_beside_libmlx_where_mlx_loads_it_never_under_resources() {
    // MLX resolves its default library only from the directory of libmlx.dylib; the old
    // `Contents/Resources/metal/` layout made every real worker trap in
    // `mlx::core::metal::Device::Device()` (L11 round 7).
    let (n, o) = documents();
    let input = parse(n, o).unwrap();
    let destination = |leaf: &str| {
        input
            .resources()
            .iter()
            .find(|r| r.source.path.ends_with(leaf))
            .unwrap()
            .destination
    };
    assert_eq!(
        destination("mlx.metallib"),
        "Contents/Frameworks/mlx.metallib"
    );
    let dir = |p: &str| p.rsplit_once('/').unwrap().0.to_owned();
    assert_eq!(
        dir(destination("mlx.metallib")),
        dir(destination("libmlx.dylib"))
    );
    assert!(
        input
            .resources()
            .iter()
            .all(|r| !r.destination.starts_with("Contents/Resources/"))
    );
}
