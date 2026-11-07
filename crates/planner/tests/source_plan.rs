// SPDX-License-Identifier: AGPL-3.0-only
use planner::{chain_compile::BuildRequest, source_plan::*};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn sha(bytes: &[u8]) -> String {
    format!("sha256:{:x}", Sha256::digest(bytes))
}
fn decode(text: &str) -> Vec<u8> {
    (0..text.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
        .collect()
}
// Independent test decoder; product encoder is compared against actual oracle bytes.
fn unbase64(s: &str) -> Vec<u8> {
    let mut value = 0u32;
    let mut bits = 0;
    let mut out = Vec::new();
    for c in s.bytes().take_while(|c| *c != b'=') {
        let n = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
            .iter()
            .position(|x| *x == c)
            .unwrap() as u32;
        value = (value << 6) | n;
        bits += 6;
        if bits >= 8 {
            bits -= 8;
            out.push((value >> bits) as u8);
        }
    }
    out
}
use std::{
    collections::BTreeMap,
    fs::{self, File},
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
struct Fixture {
    root: PathBuf,
    row: Value,
    source: BTreeMap<String, File>,
    shards: BTreeMap<String, File>,
    node_files: BTreeMap<String, Vec<Value>>,
    request: BuildRequest,
    pin: String,
}
impl Fixture {
    fn new(row: Value) -> Self {
        let root = std::env::temp_dir().join(format!(
            "l7-source-slices-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        fs::create_dir(&root).unwrap();
        let mut sets = Vec::new();
        for field in ["source_files", "shard_files"] {
            let dir = root.join(field);
            fs::create_dir(&dir).unwrap();
            let mut files = BTreeMap::new();
            for (name, hex) in row[field].as_object().unwrap() {
                let text = hex.as_str().unwrap();
                let bytes: Vec<u8> = (0..text.len())
                    .step_by(2)
                    .map(|i| u8::from_str_radix(&text[i..i + 2], 16).unwrap())
                    .collect();
                fs::write(dir.join(name), bytes).unwrap();
                files.insert(name.clone(), File::open(dir.join(name)).unwrap());
            }
            sets.push(files);
        }
        let shards = sets.pop().unwrap();
        let source = sets.pop().unwrap();
        let node_files: BTreeMap<String, Vec<Value>> =
            serde_json::from_value(row["node_files"].clone()).unwrap();
        let pin = planner::digest(&json!(node_files), true).unwrap();
        let request = BuildRequest {
            model_id: row["manifest"]["model_id"].as_str().unwrap().into(),
            resolved_commit: row["manifest"]["resolved_commit"].as_str().unwrap().into(),
            deployment_id: "12345678-1234-5678-9234-abcdefabcdef".into(),
            route_label: "a13",
            stage_sharded: true,
            compilation_cache_key: "fixture-only-current-binding".into(),
            node_ids: node_files.keys().cloned().collect(),
            managed_layer_ranges: serde_json::from_value(row["ranges"].clone()).unwrap(),
            layer_split: json!({"stages":node_files.keys().zip(row["ranges"].as_array().unwrap()).map(|(n,r)|json!({"node_id":n,"start":r[0],"end":r[1]})).collect::<Vec<_>>()}),
        };
        Self {
            root,
            row,
            source,
            shards,
            node_files,
            request,
            pin,
        }
    }
    fn repin_manifest(&mut self) {
        let m = self.row["manifest"].as_object_mut().unwrap();
        m.remove("manifest_digest");
        let digest = planner::digest(&json!(m), false).unwrap();
        m.insert(
            "manifest_digest".into(),
            json!({"algorithm":"sha256","value":digest.strip_prefix("sha256:").unwrap()}),
        );
        self.row["manifest_digest"] = json!(digest);
    }
    fn repin_transfers(&mut self) {
        self.pin = planner::digest(&json!(self.node_files), true).unwrap();
    }
    fn rewrite_shard(&mut self, bytes: Vec<u8>) {
        let path = self
            .root
            .join("shard_files/model-stage-002-of-003.safetensors");
        fs::write(path, &bytes).unwrap();
        let row = &mut self.node_files.get_mut("node-1").unwrap()[0];
        row["size_bytes"] = json!(bytes.len());
        row["content_digest"] = json!(sha(&bytes));
        self.repin_transfers();
    }
    fn build(&self, current: &BuildRequest) -> planner::Result<SliceDocuments> {
        compile_slices(
            &self.request,
            current,
            PreparedSource {
                manifest: &self.row["manifest"],
                manifest_digest: self.row["manifest_digest"].as_str().unwrap(),
                files: &self.source,
            },
            EmittedShards {
                files: &self.shards,
                node_files: &self.node_files,
                node_files_digest: &self.pin,
                max_segment_bytes: self.row["segment_bytes"].as_u64().unwrap(),
            },
        )
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.root).unwrap();
    }
}
fn rows() -> Vec<Value> {
    serde_json::from_str::<Value>(include_str!("fixtures/source-slices.json")).unwrap()["cases"]
        .as_array()
        .unwrap()
        .clone()
}
#[test]
fn full_documents_equal_real_oracle_and_ordered_bytes() {
    let mut emitted = Vec::new();
    for row in rows() {
        let f = Fixture::new(row);
        let out = f
            .build(&f.request)
            .expect("real source bytes must produce complete slice documents");
        assert_eq!(out.plan, f.row["plan"], "{}", f.row["name"]);
        assert_eq!(json!(out.node_plans), f.row["node_plans"]);
        assert_eq!(
            planner::canonical(&out.plan, true).unwrap(),
            planner::canonical(&f.row["plan"], true).unwrap()
        );
        assert_eq!(f.row["oracle_consumer_accepted"], true);
        emitted.push(json!({"name":f.row["name"],"plan":out.plan,"node_plans":out.node_plans}));
    }
    // Optional external qualification capture, never a product Python dependency.
    if let Some(path) = std::env::var_os("L7_SOURCE_PLAN_CAPTURE") {
        use std::io::Write;
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .open(path)
            .unwrap();
        file.write_all(serde_json::to_string_pretty(&emitted).unwrap().as_bytes())
            .unwrap();
    }
}

#[test]
fn current_binding_and_all_request_fields_are_checked() {
    let f = Fixture::new(rows().remove(0));
    for i in 0..9 {
        let mut current = f.request.clone();
        match i {
            0 => current.model_id.push('x'),
            1 => current.resolved_commit = "f".repeat(40),
            2 => current.deployment_id.push('x'),
            3 => current.route_label = "foreign",
            4 => current.stage_sharded = false,
            5 => current.compilation_cache_key.push('x'),
            6 => current.node_ids.reverse(),
            7 => current.managed_layer_ranges[0][1] += 1,
            _ => current.layer_split["stages"][0]["end"] = json!(99),
        }
        assert_eq!(
            f.build(&current).unwrap_err().code,
            "source_plan_binding_changed",
            "binding field {i}"
        );
    }
}

#[test]
fn independent_manifest_and_transfer_pins_are_checked() {
    let mut f = Fixture::new(rows().remove(0));
    f.row["manifest"]["requested_revision"] = json!("changed-without-repin");
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_source_manifest_invalid"
    );
    let mut f = Fixture::new(rows().remove(0));
    f.row["manifest_digest"] = json!(format!("sha256:{}", "0".repeat(64)));
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_source_manifest_invalid"
    );
    let mut f = Fixture::new(rows().remove(0));
    f.pin = format!("sha256:{}", "0".repeat(64));
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_plan_file_digest_mismatch"
    );
}

#[test]
fn actual_source_and_shard_bytes_not_claimed_hashes() {
    for (field, code) in [
        ("source_files", "slice_source_manifest_invalid"),
        ("shard_files", "slice_plan_file_digest_mismatch"),
    ] {
        let f = Fixture::new(rows().remove(0));
        let name = if field == "source_files" {
            "model.safetensors"
        } else {
            "model-stage-002-of-003.safetensors"
        };
        let path = f.root.join(field).join(name);
        let mut bytes = fs::read(&path).unwrap();
        *bytes.last_mut().unwrap() ^= 1;
        fs::write(path, bytes).unwrap();
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            code,
            "{field} actual content"
        );
    }
}

#[test]
fn node_order_ranges_and_transfers_cannot_be_relabelled() {
    for i in 0..4 {
        let mut f = Fixture::new(rows().remove(0));
        match i {
            0 => f.request.node_ids.reverse(),
            1 => f.request.managed_layer_ranges[1] = [1, 4],
            2 => {
                f.node_files
                    .insert("foreign".into(), f.node_files["node-1"].clone());
            }
            _ => {
                let a = f.node_files["node-0"].clone();
                let b = f.node_files["node-1"].clone();
                f.node_files.insert("node-0".into(), b);
                f.node_files.insert("node-1".into(), a);
            }
        }
        f.pin = planner::digest(&json!(f.node_files), true).unwrap();
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            "source_plan_assignment_mismatch",
            "binding {i}"
        );
    }
}

#[test]
fn missing_and_escaped_input_names_refuse_without_path_open() {
    let mut f = Fixture::new(rows().remove(0));
    f.source.remove("model.safetensors");
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_source_manifest_invalid"
    );
    let mut f = Fixture::new(rows().remove(0));
    f.shards.remove("model-stage-002-of-003.safetensors");
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_plan_file_digest_mismatch"
    );
    for path in [
        "deployment/../outside.safetensors",
        "/deployment/x.safetensors",
        "deployment/.hidden.safetensors",
        "deployment//x.safetensors",
    ] {
        let mut f = Fixture::new(rows().remove(0));
        f.node_files.get_mut("node-1").unwrap()[0]["path"] = json!(path);
        f.pin = planner::digest(&json!(f.node_files), true).unwrap();
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            "slice_path_invalid",
            "{path}"
        );
    }
}

#[test]
fn repinned_wrong_manifest_content_size_hash_model_commit_and_paths_refuse() {
    for mutation in ["size", "hash", "model", "commit", "escape"] {
        let mut f = Fixture::new(rows().remove(0));
        let m = &mut f.row["manifest"];
        match mutation {
            "size" => m["files"][0]["size_bytes"] = json!(1),
            "hash" => m["files"][0]["content_digest"]["value"] = json!("0".repeat(64)),
            "model" => m["model_id"] = json!("foreign/model"),
            "commit" => m["resolved_commit"] = json!("f".repeat(40)),
            _ => m["files"][0]["path"] = json!("../model.safetensors"),
        }
        f.repin_manifest();
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            if mutation == "escape" {
                "slice_path_invalid"
            } else {
                "slice_source_manifest_invalid"
            },
            "{mutation}"
        );
    }
}

#[test]
fn independently_repinned_shard_still_must_match_source_tensor_bytes() {
    let mut f = Fixture::new(rows().remove(0));
    let mut bytes = fs::read(
        f.root
            .join("shard_files/model-stage-002-of-003.safetensors"),
    )
    .unwrap();
    *bytes.last_mut().unwrap() ^= 1;
    f.rewrite_shard(bytes);
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_source_tensor_mismatch"
    );
}

#[test]
fn malformed_headers_and_tensor_schema_mismatch_refuse_after_outer_repin() {
    for mutation in [
        "prefix",
        "truncated",
        "shape",
        "dtype",
        "missing",
        "offsets",
    ] {
        let mut f = Fixture::new(rows().remove(0));
        let mut bytes = fs::read(
            f.root
                .join("shard_files/model-stage-002-of-003.safetensors"),
        )
        .unwrap();
        let mut expected = "slice_safetensors_invalid";
        if mutation == "prefix" {
            bytes[..8].copy_from_slice(&u64::MAX.to_le_bytes());
        } else if mutation == "truncated" {
            bytes.truncate(7);
        } else {
            let len = u64::from_le_bytes(bytes[..8].try_into().unwrap()) as usize;
            let mut h: Value = serde_json::from_slice(&bytes[8..8 + len]).unwrap();
            let key = h
                .as_object()
                .unwrap()
                .keys()
                .find(|k| k.as_str() != "__metadata__")
                .unwrap()
                .clone();
            match mutation {
                "shape" => {
                    h[&key]["shape"] = json!([99]);
                    expected = "slice_source_tensor_mismatch";
                }
                "dtype" => {
                    h[&key]["dtype"] = json!("F64");
                    expected = "slice_source_tensor_mismatch";
                }
                "missing" => {
                    let v = h.as_object_mut().unwrap().remove(&key).unwrap();
                    h["foreign.tensor"] = v;
                    expected = "slice_source_tensor_missing";
                }
                _ => h[&key]["data_offsets"][0] = json!(1),
            }
            let h = serde_json::to_vec(&h).unwrap();
            let mut rewritten = (h.len() as u64).to_le_bytes().to_vec();
            rewritten.extend(h);
            rewritten.extend_from_slice(&bytes[8 + len..]);
            bytes = rewritten;
        }
        f.rewrite_shard(bytes);
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            expected,
            "{mutation}"
        );
    }
}

#[test]
fn source_origin_bounds_and_regular_file_contract() {
    let mut f = Fixture::new(rows().remove(0));
    f.source.insert(
        "model.safetensors".into(),
        File::open(f.root.join("source_files")).unwrap(),
    );
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_source_manifest_invalid"
    );
    for size in [0, 16777217] {
        let mut f = Fixture::new(rows().remove(0));
        f.row["segment_bytes"] = json!(size);
        assert_eq!(
            f.build(&f.request).unwrap_err().code,
            "slice_plan_request_invalid"
        );
    }
    let mut f = Fixture::new(rows().remove(0));
    let limit = budgets::BudgetId::PyMyceliumA13MacosServingSliceFetchMaxSourceBytesL57
        .bytes()
        .unwrap()
        .get();
    f.row["manifest"]["files"][0]["size_bytes"] = json!(limit + 1);
    f.repin_manifest();
    assert_eq!(
        f.build(&f.request).unwrap_err().code,
        "slice_fetch_model_too_large"
    );
}

#[test]
fn retained_handles_do_not_reopen_replaced_names_or_depend_on_seek_position() {
    use std::io::{Seek, SeekFrom};
    let f = Fixture::new(rows().remove(0));
    for (field, map) in [("source_files", &f.source), ("shard_files", &f.shards)] {
        for (name, file) in map {
            file.try_clone().unwrap().seek(SeekFrom::End(0)).unwrap();
            fs::rename(
                f.root.join(field).join(name),
                f.root.join(field).join(format!("held-{name}")),
            )
            .unwrap();
            fs::write(
                f.root.join(field).join(name),
                b"untrusted replacement pathname",
            )
            .unwrap();
        }
    }
    assert_eq!(f.build(&f.request).unwrap().plan, f.row["plan"]);
}

#[test]
fn document_projection_rejects_plan_digest_and_unknown_node() {
    let f = Fixture::new(rows().remove(0));
    let mut plan = f.build(&f.request).unwrap().plan;
    assert_eq!(
        node_slice_plan(&plan, "absent").unwrap_err().code,
        "slice_plan_node_unknown"
    );
    plan["model_id"] = json!("foreign/model");
    assert_eq!(
        node_slice_plan(&plan, "node-0").unwrap_err().code,
        "slice_plan_digest_mismatch"
    );
}

#[test]
fn every_emitted_recipe_reconstitutes_exact_shard_and_exchange_preserves_full_documents() {
    use api::run_exchange::{Announce, RunExchange};
    for row in rows() {
        let f = Fixture::new(row);
        let docs = f.build(&f.request).unwrap();
        for (node, p) in &docs.node_plans {
            assert_eq!(
                planner::canonical(p, true).unwrap(),
                planner::canonical(&f.row["node_plans"][node], true).unwrap()
            );
            let mut downloaded = 0;
            let mut inlined = 0;
            for file in p["files"].as_array().unwrap() {
                let mut reconstructed = Vec::new();
                for s in file["segments"].as_array().unwrap() {
                    assert_eq!(s["offset"].as_u64().unwrap(), reconstructed.len() as u64);
                    let n = s["length"].as_u64().unwrap() as usize;
                    let bytes = if let Some(text) = s["inline_base64"].as_str() {
                        inlined += n;
                        unbase64(text)
                    } else {
                        downloaded += n;
                        let source = decode(
                            f.row["source_files"][s["source_path"].as_str().unwrap()]
                                .as_str()
                                .unwrap(),
                        );
                        let start = s["source_offset"].as_u64().unwrap() as usize;
                        source[start..start + n].to_vec()
                    };
                    assert_eq!(bytes.len(), n);
                    assert_eq!(s["sha256"], sha(&bytes));
                    reconstructed.extend(bytes);
                }
                let name = file["path"]
                    .as_str()
                    .unwrap()
                    .strip_prefix("deployment/")
                    .unwrap();
                assert_eq!(
                    reconstructed,
                    decode(f.row["shard_files"][name].as_str().unwrap())
                );
                assert_eq!(file["size_bytes"], json!(reconstructed.len()));
                assert_eq!(file["content_digest"], sha(&reconstructed));
            }
            assert_eq!(p["download_bytes"], json!(downloaded));
            assert_eq!(p["inline_bytes"], json!(inlined));
        }
        // The existing protocol publication consumer is shape/serialization only,
        // NOT a runtime slice fetch or authority/readiness qualification.
        let exchange = RunExchange::new(Box::new(|| 1.0));
        let per_node = docs
            .node_plans
            .iter()
            .map(|(n, p)| (n.clone(), json!({"node_slice_plan":p})))
            .collect();
        let prepared=exchange.prepare_announcement(Announce{phase:"compile".into(),run_id:Some("fixture-run".into()),deployment_id:Some(f.request.deployment_id.clone()),model:Some(json!({"model_id":f.request.model_id,"resolved_commit":f.request.resolved_commit})),slice_mode:true,manifest_digest:Some(f.row["manifest_digest"].as_str().unwrap().into()),work_limits:None,per_node}).unwrap();
        assert!(exchange.commit_announcement(&prepared, false));
        for (node, p) in docs.node_plans {
            assert_eq!(
                exchange.announcement(Some(&node)).to_value()["node_slice_plan"],
                p
            );
        }
    }
}
