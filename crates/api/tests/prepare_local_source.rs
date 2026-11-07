// SPDX-License-Identifier: AGPL-3.0-only
//! `api::serving_source::prepare_local_source` equals the executed oracle (`serving.prepare_local_source` with
//! the compiler injected, f4e366e9): 42 cases from `L10-evidence/r18-probe-prepare-local-source.py`
//! (sha256 753aaade12bc63f877846dd9873fc8ecbdd87f1173b282a1766fb1b2fe74280d), fixture
//! `fixtures/oracle_prepare.json` (sha256 30f3866164babc0a63eaf1b165d2fbe762a6ae21ee9d7dfbd1fdbabc9ce2023f).
use api::{
    model_catalog,
    serving_source::{Failure, LocalSource, prepare_local_source},
};
use serde_json::{Value, json};
use std::{
    fs,
    os::unix::fs::symlink,
    path::{Path, PathBuf},
};

const FIXTURE: &str = include_str!("fixtures/oracle_prepare.json");

fn model_dir(model: &model_catalog::Model) -> String {
    format!("models--{}", model.model_id.replace('/', "--"))
}
fn build(
    base: &Path,
    model: &model_catalog::Model,
    snapshot: Option<&str>,
    parent: &str,
    mdir: Option<&str>,
    config: Option<&str>,
    weights: Option<(&str, &[u8])>,
) -> PathBuf {
    let root = base
        .join(mdir.map_or_else(|| model_dir(model), str::to_owned))
        .join(parent)
        .join(snapshot.unwrap_or(model.resolved_commit));
    fs::create_dir_all(&root).unwrap();
    if let Some(config) = config {
        fs::write(root.join("config.json"), config).unwrap();
    }
    if let Some((name, bytes)) = weights {
        fs::write(root.join(name), bytes).unwrap();
    }
    root
}
const GOOD_CONFIG: &str = r#"{"model_type":"qwen2","torch_dtype":"bfloat16"}"#;

/// The scenario builders, keyed by the oracle case label.
fn make(label: &str, base: &Path, model: &model_catalog::Model) -> PathBuf {
    let std_build = |config: Option<&str>, weights: Option<(&str, &[u8])>| {
        build(base, model, None, "snapshots", None, config, weights)
    };
    let x100 = vec![b'x'; 100];
    match label {
        l if l.starts_with("manifest_") || l.starts_with("compiler_") || l == "ok" => {
            std_build(Some(GOOD_CONFIG), Some(("model.safetensors", &x100)))
        }
        "ok_bf16_alias" => std_build(
            Some(r#"{"model_type":"qwen2","torch_dtype":"bf16"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "ok_qwen3" => build(
            base,
            model,
            None,
            "snapshots",
            Some("models--Qwen--Qwen3-0.6B"),
            Some(r#"{"model_type":"qwen3","torch_dtype":"bfloat16"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "root_symlink" => {
            let real = std_build(Some(GOOD_CONFIG), Some(("model.safetensors", &x100)));
            let link = base.join("link");
            symlink(real, &link).unwrap();
            link
        }
        "root_missing" => base.join("nope"),
        "root_is_file" => {
            fs::write(base.join("f"), "x").unwrap();
            base.join("f")
        }
        "snapshot_name_wrong" => build(
            base,
            model,
            Some(&"f".repeat(40)),
            "snapshots",
            None,
            Some(GOOD_CONFIG),
            Some(("model.safetensors", &x100)),
        ),
        "parent_name_wrong" => build(
            base,
            model,
            None,
            "snaps",
            None,
            Some(GOOD_CONFIG),
            Some(("model.safetensors", &x100)),
        ),
        "model_dir_wrong" => build(
            base,
            model,
            None,
            "snapshots",
            Some("models--Qwen--Other"),
            Some(GOOD_CONFIG),
            Some(("model.safetensors", &x100)),
        ),
        "config_missing" => std_build(None, Some(("model.safetensors", &x100))),
        "config_not_json" => std_build(Some("not json"), Some(("model.safetensors", &x100))),
        "config_list" => std_build(Some("[]"), Some(("model.safetensors", &x100))),
        "config_utf8_bad" => {
            let r = std_build(None, Some(("model.safetensors", &x100)));
            fs::write(r.join("config.json"), [0xffu8, 0xfe]).unwrap();
            r
        }
        "config_symlink" => {
            let r = std_build(None, Some(("model.safetensors", &x100)));
            fs::write(base.join("real.json"), GOOD_CONFIG).unwrap();
            symlink(base.join("real.json"), r.join("config.json")).unwrap();
            r
        }
        "config_too_big" => {
            let r = std_build(None, Some(("model.safetensors", &x100)));
            fs::File::create(r.join("config.json"))
                .unwrap()
                .set_len(16 * 1024 * 1024 + 1)
                .unwrap();
            r
        }
        "config_exact_max" => {
            let r = std_build(None, Some(("model.safetensors", &x100)));
            let head = br#"{"model_type":"qwen2","torch_dtype":"bfloat16","p":""#;
            let pad = 16 * 1024 * 1024 - head.len() - 2;
            let mut body = head.to_vec();
            body.extend(vec![b'x'; pad]);
            body.extend_from_slice(br#""}"#);
            fs::write(r.join("config.json"), body).unwrap();
            r
        }
        "arch_mismatch" => std_build(
            Some(r#"{"model_type":"qwen3","torch_dtype":"bfloat16"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "dtype_float16" => std_build(
            Some(r#"{"model_type":"qwen2","torch_dtype":"float16"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "dtype_missing" => std_build(
            Some(r#"{"model_type":"qwen2"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "model_type_missing" => std_build(
            Some(r#"{"torch_dtype":"bfloat16"}"#),
            Some(("model.safetensors", &x100)),
        ),
        "no_weights" => std_build(Some(GOOD_CONFIG), None),
        "weights_other_ext" => std_build(Some(GOOD_CONFIG), Some(("model.bin", b"x"))),
        "weights_two_sum" => {
            let r = std_build(Some(GOOD_CONFIG), Some(("model.safetensors", &x100)));
            fs::write(r.join("b.safetensors"), vec![b'y'; 7]).unwrap();
            r
        }
        "weights_hardlink_once" => {
            let r = std_build(Some(GOOD_CONFIG), Some(("model.safetensors", &x100)));
            fs::hard_link(r.join("model.safetensors"), r.join("alias.safetensors")).unwrap();
            r
        }
        "weights_broken_symlink" => {
            let r = std_build(Some(GOOD_CONFIG), Some(("model.safetensors", &x100)));
            symlink(base.join("missing"), r.join("dead.safetensors")).unwrap();
            r
        }
        "weights_symlink_counts_target" => {
            let r = std_build(Some(GOOD_CONFIG), None);
            fs::write(base.join("w.bin"), vec![b'z'; 33]).unwrap();
            symlink(base.join("w.bin"), r.join("model.safetensors")).unwrap();
            r
        }
        "weights_is_dir" => {
            let r = std_build(Some(GOOD_CONFIG), None);
            fs::create_dir(r.join("d.safetensors")).unwrap();
            r
        }
        other => panic!("no builder for {other}"),
    }
}
fn good_manifest(model: &model_catalog::Model) -> Value {
    json!({"model_id": model.model_id, "resolved_commit": model.resolved_commit,
        "runtime_model": {"architecture": api::serving_source::architecture(model)},
        "manifest_digest": {"algorithm": "sha256", "value": "ab".repeat(32)}})
}
fn compile_for(label: &str, model: &model_catalog::Model) -> Result<Value, Failure> {
    let mut m = good_manifest(model);
    let set = |m: &mut Value, k: &str, v: Value| m[k] = v;
    match label {
        "compiler_serving_error" => return Err(Failure::Serving("boom_code".into())),
        "compiler_other_error" | "compiler_value_error" => return Err(Failure::Other),
        "compiler_returns_list" => return Ok(json!([])),
        "compiler_returns_pairs" => return Ok(json!([["model_id", model.model_id]])),
        "manifest_id_wrong" => set(&mut m, "model_id", json!("x/y")),
        "manifest_commit_wrong" => set(&mut m, "resolved_commit", json!("0".repeat(40))),
        "manifest_arch_wrong" => set(&mut m, "runtime_model", json!({"architecture": "qwen3"})),
        "manifest_runtime_model_missing" => set(&mut m, "runtime_model", Value::Null),
        "manifest_digest_alg" => set(
            &mut m,
            "manifest_digest",
            json!({"algorithm": "sha1", "value": "ab".repeat(32)}),
        ),
        "manifest_digest_not_dict" => set(&mut m, "manifest_digest", json!("x")),
        "manifest_digest_short" => set(
            &mut m,
            "manifest_digest",
            json!({"algorithm": "sha256", "value": "ab".repeat(31)}),
        ),
        "manifest_digest_long" => set(
            &mut m,
            "manifest_digest",
            json!({"algorithm": "sha256", "value": "ab".repeat(33)}),
        ),
        "manifest_digest_missing" => set(&mut m, "manifest_digest", Value::Null),
        "manifest_digest_value_int" => set(
            &mut m,
            "manifest_digest",
            json!({"algorithm": "sha256", "value": 12}),
        ),
        _ => {}
    }
    Ok(m)
}

#[test]
fn prepare_local_source_equals_the_executed_oracle() {
    let doc: Value = serde_json::from_str(FIXTURE).unwrap();
    let cases = doc["cases"].as_array().unwrap();
    assert_eq!(cases.len(), 42);
    for case in cases {
        let label = case["label"].as_str().unwrap();
        let model = &model_catalog::registered()[case["model_index"].as_u64().unwrap() as usize];
        let base = fs::canonicalize(std::env::temp_dir())
            .unwrap()
            .join(format!("l10-prep-{}-{label}", std::process::id()));
        fs::create_dir_all(&base).unwrap();
        let root = make(label, &base, model);
        let calls = std::cell::RefCell::new(Vec::<Value>::new());
        let compiler = |source: &LocalSource| {
            calls.borrow_mut().push(json!({
                "root": source.root.strip_prefix(&base).unwrap().to_string_lossy(),
                "model_id": source.model_id, "requested_revision": source.requested_revision,
                "resolved_commit": source.resolved_commit,
            }));
            compile_for(label, model)
        };
        let got = prepare_local_source(&root, model, &compiler);
        let expected = &case["result"];
        match (&got, expected["ok"].as_bool().unwrap()) {
            (Ok(p), true) => {
                assert_eq!(
                    p.source_bytes,
                    expected["source_bytes"].as_u64().unwrap(),
                    "{label}"
                );
                assert_eq!(
                    p.manifest_digest,
                    expected["manifest_digest"].as_str().unwrap(),
                    "{label}"
                );
                assert_eq!(
                    p.source_root.strip_prefix(&base).unwrap().to_string_lossy(),
                    expected["source_root"].as_str().unwrap(),
                    "{label}"
                );
            }
            (Err(Failure::Serving(code)), false) => {
                assert_eq!(expected["kind"], "serving", "{label}");
                assert_eq!(code, expected["code"].as_str().unwrap(), "{label}");
            }
            (Err(Failure::Other), false) => assert_eq!(expected["kind"], "other", "{label}"),
            (other, ok) => panic!("{label}: ours {other:?}, oracle ok={ok} {expected}"),
        }
        assert_eq!(
            Value::Array(calls.into_inner()),
            case["calls"],
            "{label}: compiler calls"
        );
        fs::remove_dir_all(&base).unwrap();
    }
}
