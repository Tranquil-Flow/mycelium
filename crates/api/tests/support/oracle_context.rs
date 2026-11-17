// SPDX-License-Identifier: AGPL-3.0-only
//! The oracle's serving context and the product's (owner ruling 16 Nov 2026,
//! `contracts/divergences.json`), and below them the oracle's run-model starter and picker texts
//! and the product's (B13). Every v0.1.4 fixture that carries a consent document was recorded
//! at the oracle's `DEMO_CONTEXT_TOKENS` (model_resources.py:76 @ f4e366e9, 4,096); the product
//! computes the whole-model member figures at its model contract's serving context
//! (`model_catalog::Model::serving_context_tokens`, 2,356). Parity is checked at 4,096, passed
//! explicitly; the product's own document is what the product is compared with. The contract's
//! value never reaches a fixture comparison.
#![allow(dead_code)]
use api::model_catalog;
use serde_json::Value;

/// `model_resources.DEMO_CONTEXT_TOKENS` at f4e366e9: the context the fixtures were recorded at.
pub const ORACLE_CONTEXT_TOKENS: u64 = 4096;

/// Every consent document inside an oracle result (`resources`): checked equal to the product's
/// document computed at [`ORACLE_CONTEXT_TOKENS`], then replaced by the product's document at the
/// contract's serving context, which is what the product returns. Everything else in the result
/// is compared as recorded.
pub fn at_product_context(value: &mut Value) {
    match value {
        Value::Object(map) => {
            for (key, inner) in map.iter_mut() {
                if key == "resources" && inner.get("storage_consent_ceiling_bytes").is_some() {
                    let selected = inner["selected"]["model_id"]
                        .as_str()
                        .zip(inner["selected"]["resolved_commit"].as_str())
                        .map(|(m, c)| (m.to_owned(), c.to_owned()));
                    let selected = selected.as_ref().map(|(m, c)| (m.as_str(), c.as_str()));
                    let memory = inner["device_memory_bytes"].as_u64();
                    assert_eq!(
                        *inner,
                        model_catalog::consent_document_at(selected, memory, ORACLE_CONTEXT_TOKENS),
                        "the consent document at the oracle's context equals the oracle's"
                    );
                    *inner = model_catalog::consent_document(selected, memory);
                } else {
                    at_product_context(inner);
                }
            }
        }
        Value::Array(items) => items.iter_mut().for_each(at_product_context),
        _ => {}
    }
}

/// The released app's response bytes (compact `json.dumps`) with the one consent-document figure
/// that depends on the serving context, each model's `whole_model_member_rss_bytes`, moved from
/// its value at [`ORACLE_CONTEXT_TOKENS`] to the product's at the contract's context. Every such
/// field in the bytes must carry a registered model's figure at 4,096 (this pins the oracle side);
/// every other byte stays as recorded.
pub fn at_product_context_bytes(oracle_bytes: &str) -> String {
    const FIELD: &str = "\"whole_model_member_rss_bytes\":";
    let rss = |member: Option<Value>| member.unwrap()["max_rss_bytes"].as_u64().unwrap();
    let pairs: Vec<(u64, u64)> = model_catalog::registered()
        .iter()
        .map(|m| {
            (
                rss(model_catalog::whole_model_member_at(
                    m.model_id,
                    ORACLE_CONTEXT_TOKENS,
                )),
                rss(model_catalog::whole_model_member(m.model_id)),
            )
        })
        .collect();
    let mut out = String::with_capacity(oracle_bytes.len());
    let mut rest = oracle_bytes;
    while let Some(at) = rest.find(FIELD) {
        let (head, tail) = rest.split_at(at + FIELD.len());
        out.push_str(head);
        let digits = tail.bytes().take_while(u8::is_ascii_digit).count();
        let recorded: u64 = tail[..digits].parse().expect("an integer figure");
        let (_, product) = pairs
            .iter()
            .find(|(oracle, _)| *oracle == recorded)
            .unwrap_or_else(|| panic!("{recorded} is no registered model's figure at 4,096"));
        out.push_str(&product.to_string());
        rest = &tail[digits..];
    }
    out.push_str(rest);
    out
}

// ---- The run-model picker (B13) -----------------------------------------------------------------
//
// ORCHESTRATOR DECISION 17 Nov 2026 (reversible, on ruling 28; `contracts/divergences.json`
// `diverged.run_model_default`): v0.2's starter is `qwen3-0.6b` where v0.1.4's is `a9-route`, and
// the two picker texts that say which model was tested on two Macs are swapped. Every v0.1.4
// fixture that carries a run-model document was recorded with the oracle's starter and texts; the
// oracle side is checked against the record, then moved to the product's. The product's values
// come from the record and from the recorded document's own rows, never from the code under test.

/// `contracts/divergences.json` `diverged.run_model_default`.
pub fn picker_record() -> Value {
    let record: Value =
        serde_json::from_str(include_str!("../../contracts/divergences.json")).unwrap();
    record["diverged"]["run_model_default"].clone()
}

fn picker_labels(record: &Value) -> Vec<(String, Value, Value)> {
    let oracle = record["oracle_labels"].as_object().expect("oracle_labels");
    let product = record["product_labels"]
        .as_object()
        .expect("product_labels");
    assert_eq!(
        oracle.keys().collect::<Vec<_>>(),
        product.keys().collect::<Vec<_>>(),
        "the record relabels the same choices on both sides"
    );
    oracle
        .iter()
        .map(|(name, text)| (name.clone(), text.clone(), product[name].clone()))
        .collect()
}

/// The choice row named `name` of a recorded document's `choices`.
fn choice_row<'a>(choices: &'a [Value], name: &Value) -> &'a Value {
    choices
        .iter()
        .find(|c| c["name"] == *name)
        .unwrap_or_else(|| panic!("no choice row {name}"))
}

/// Every run-model picker document inside an oracle result (an object with `demo_default` and
/// `choices`) moved from the oracle's starter and picker texts to the product's. A document that
/// shows the starter (`source` `demo_default`) also moves its selection: name from the record,
/// model id and commit from the document's own choice row for the product's starter, and
/// `resources.selected` from the document's own `resources.models` row for that model (the oracle's
/// `resources.selected` must equal its row for the oracle's starter). Returns how many documents
/// were moved.
pub fn at_product_picker(value: &mut Value) -> usize {
    match value {
        Value::Object(map) => {
            let mut moved = 0;
            if map.contains_key("demo_default") && map.contains_key("choices") {
                picker_document(map);
                moved += 1;
            }
            for (key, inner) in map.iter_mut() {
                if key != "choices" {
                    moved += at_product_picker(inner);
                }
            }
            moved
        }
        Value::Array(items) => items.iter_mut().map(at_product_picker).sum(),
        _ => 0,
    }
}

fn picker_document(doc: &mut serde_json::Map<String, Value>) {
    let record = picker_record();
    let (oracle, product) = (&record["oracle_value"], &record["product_value"]);
    assert_eq!(doc["demo_default"], *oracle, "the oracle's starter");
    doc.insert("demo_default".into(), product.clone());
    let choices = doc
        .get_mut("choices")
        .and_then(Value::as_array_mut)
        .expect("choices");
    for (name, was, now) in picker_labels(&record) {
        let row = choices
            .iter_mut()
            .find(|c| c["name"] == name.as_str())
            .unwrap_or_else(|| panic!("no choice row {name}"));
        assert_eq!(row["label"], was, "the oracle's text for {name}");
        row["label"] = now;
    }
    if doc["source"] != "demo_default" {
        return;
    }
    assert_eq!(
        doc["selected"], *oracle,
        "a starter document shows the oracle's starter"
    );
    let choices = doc["choices"].as_array().unwrap().clone();
    let (was, now) = (choice_row(&choices, oracle), choice_row(&choices, product));
    assert_eq!(
        (&doc["model_id"], &doc["resolved_commit"]),
        (&was["model_id"], &was["resolved_commit"])
    );
    doc.insert("selected".into(), product.clone());
    doc.insert("model_id".into(), now["model_id"].clone());
    doc.insert("resolved_commit".into(), now["resolved_commit"].clone());
    if let Some(resources) = doc.get_mut("resources").and_then(Value::as_object_mut) {
        let models = resources["models"]
            .as_array()
            .expect("resources.models")
            .clone();
        let model_row = |id: &Value| {
            models
                .iter()
                .find(|m| m["model_id"] == *id)
                .unwrap_or_else(|| panic!("no resources.models row {id}"))
                .clone()
        };
        assert_eq!(resources["selected"], model_row(&was["model_id"]));
        resources.insert("selected".into(), model_row(&now["model_id"]));
    }
}

/// `text` as the released app serialises a string (`json.dumps`, `ensure_ascii`).
fn python_json_string(text: &str) -> String {
    let mut out = String::new();
    for unit in serde_json::to_string(text).unwrap().encode_utf16() {
        match unit {
            0x20..=0x7e => out.push(char::from(unit as u8)),
            _ => out.push_str(&format!("\\u{unit:04x}")),
        }
    }
    out
}

/// Replaces the one occurrence of `from` in `text` by `to` (exactly one must exist).
fn replace_once(text: &str, from: &str, to: &str) -> String {
    assert_eq!(
        text.matches(from).count(),
        1,
        "exactly one {from} in {text}"
    );
    text.replacen(from, to, 1)
}

/// The released app's bytes (compact `json.dumps`) of ONE run-model picker document moved to the
/// product's starter and texts, as [`at_product_picker`] moves the parsed document; every other
/// byte stays as recorded.
pub fn at_product_picker_bytes(oracle_bytes: &str) -> String {
    let record = picker_record();
    let (oracle, product) = (&record["oracle_value"], &record["product_value"]);
    let quoted = |v: &Value| python_json_string(v.as_str().unwrap());
    let mut text = replace_once(
        oracle_bytes,
        &format!("\"demo_default\":{}", quoted(oracle)),
        &format!("\"demo_default\":{}", quoted(product)),
    );
    for (_, was, now) in picker_labels(&record) {
        text = replace_once(
            &text,
            &format!("\"label\":{}", quoted(&was)),
            &format!("\"label\":{}", quoted(&now)),
        );
    }
    let parsed: Value = serde_json::from_str(oracle_bytes).unwrap();
    if parsed["source"] != "demo_default" {
        return text;
    }
    let choices = parsed["choices"].as_array().unwrap();
    let (was, now) = (choice_row(choices, oracle), choice_row(choices, product));
    let head = |name: &Value, row: &Value| {
        format!(
            "\"selected\":{},\"source\":\"demo_default\",\"locked\":false,\"model_id\":{},\"resolved_commit\":{}",
            quoted(name),
            quoted(&row["model_id"]),
            quoted(&row["resolved_commit"])
        )
    };
    text = replace_once(&text, &head(oracle, was), &head(product, now));
    // `resources.selected` and each `resources.models` row are the same flat object.
    let flat_row = |text: &str, id: &Value| -> String {
        let start = format!("{{\"model_id\":{},", quoted(id));
        let at = text.find(&start).unwrap_or_else(|| panic!("no row {id}"));
        let end = at + text[at..].find('}').unwrap() + 1;
        text[at..end].to_owned()
    };
    let (was_row, now_row) = (
        flat_row(&text, &was["model_id"]),
        flat_row(&text, &now["model_id"]),
    );
    assert_eq!(
        text.matches(&was_row).count(),
        2,
        "the oracle's resources.selected is its resources.models row"
    );
    replace_once(
        &text,
        &format!("\"selected\":{was_row}"),
        &format!("\"selected\":{now_row}"),
    )
}
