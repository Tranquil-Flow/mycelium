// SPDX-License-Identifier: AGPL-3.0-only
//! SE-L5-L7: `route_assignment.validate_preparation_assignment` against the executed oracle
//! (`fixtures/preparation.json`, generator and provenance beside it, FX-L7-preparation-r10).
//! Every case is a patch on an oracle-compiled assignment with a signed-manifest-shaped pack;
//! the oracle's verdict (ok / refusal code / escaped exception class) must be reproduced exactly.
use planner::bound::assignment_id_for;
use planner::preparation::{
    PREPARATION_ASSIGNMENT_INVALID, PreparationError, validate_preparation_assignment,
};
use serde_json::{Value, json};

fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/preparation.json")).unwrap()
}

fn apply(doc: &mut Value, ops: &Value) {
    for op in ops.as_array().unwrap() {
        let path = op["path"].as_array().unwrap();
        let mut cursor = &mut *doc;
        for (i, key) in path.iter().enumerate() {
            let last = i + 1 == path.len();
            let slot = match key {
                Value::String(k) if last => {
                    let map = cursor.as_object_mut().unwrap();
                    if op.get("delete").is_some() {
                        map.remove(k);
                    } else {
                        map.insert(k.clone(), op["value"].clone());
                    }
                    continue;
                }
                Value::String(k) => &mut cursor[k.as_str()],
                Value::Number(n) => {
                    let index = n.as_u64().unwrap() as usize;
                    if last {
                        cursor[index] = op["value"].clone();
                        continue;
                    }
                    &mut cursor[index]
                }
                _ => unreachable!(),
            };
            cursor = slot;
        }
    }
}

fn run(doc: &Value) -> Value {
    match validate_preparation_assignment(&doc["assignment"], &doc["pack"], &doc["node_id"]) {
        Ok(()) => json!({"ok": true}),
        Err(PreparationError::Invalid) => json!({"refusal": PREPARATION_ASSIGNMENT_INVALID}),
        Err(PreparationError::Escaped(f)) => {
            json!({"raised": f.message.split(':').next().unwrap_or("")})
        }
    }
}

#[test]
fn oracle_corpus_preparation_verdicts_are_reproduced() {
    let f = fixture();
    let bases = f["bases"].as_array().unwrap();
    let cases = f["cases"].as_array().unwrap();
    assert!(cases.len() >= 2000, "corpus shrank: {}", cases.len());
    let (mut ok, mut refused, mut mismatches) = (0, 0, Vec::new());
    for c in cases {
        let base = &bases[c["base"].as_u64().unwrap() as usize];
        let mut doc = json!({
            "assignment": base["assignment"], "pack": base["pack"], "node_id": base["node_id"],
        });
        apply(&mut doc, &c["ops"]);
        if c["reid"] == true
            && doc["assignment"].is_object()
            && let Ok(id) = assignment_id_for(&doc["assignment"])
        {
            doc["assignment"]["assignment_id"] = json!(id);
        }
        let got = run(&doc);
        if got != c["oracle"] {
            mismatches.push(format!(
                "{}: oracle {} rust {}",
                c["label"], c["oracle"], got
            ));
        }
        if c["oracle"].get("ok").is_some() {
            ok += 1;
        } else {
            refused += 1;
        }
    }
    assert!(ok >= 300 && refused >= 1500, "ok {ok} refused {refused}");
    assert!(
        mismatches.is_empty(),
        "{} of {} cases differ from the oracle; first 25:\n{}",
        mismatches.len(),
        cases.len(),
        mismatches
            .iter()
            .take(25)
            .cloned()
            .collect::<Vec<_>>()
            .join("\n")
    );
}
