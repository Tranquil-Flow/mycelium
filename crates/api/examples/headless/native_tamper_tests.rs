// SPDX-License-Identifier: AGPL-3.0-only
//! Mutate only genuine model-run output, never synthesize a passing proof.
use super::*;
use api::compiled_handoff::{CompiledHandoff, Pin, Pins};
use std::io::{Read, Seek, SeekFrom, Write};

pub(super) fn native_handoff_tamper_refusals(
    root: &Path,
    pins: &Pins,
    signer: &protocol::identity::Identity,
    handoff: &CompiledHandoff,
) {
    let original: BTreeMap<String, Vec<u8>> = pins
        .keys()
        .map(|name| (name.clone(), fs::read(root.join(name)).unwrap()))
        .collect();
    let read = |name: &str| serde_json::from_slice::<Value>(&original[name]).unwrap();
    let put = |name: &str, doc: &Value| {
        fs::write(root.join(name), protocol::canonical::to_bytes(doc).unwrap()).unwrap()
    };
    for mutation in [
        "swapped-stage-order",
        "wrong-challenge-token",
        "stale-member-generation",
        "wrong-resident-digest",
    ] {
        let mut plan = read("compiled/operator-plan.json");
        let mut checkpoint = read("compiled/local-challenge-checkpoint.json");
        let mut report = read("compiled/build-report.json");
        let mut template = read("template.json");
        match mutation {
            "swapped-stage-order" => plan["stages"].as_array_mut().unwrap().swap(0, 1),
            "wrong-challenge-token" => {
                let wrong = checkpoint["output_token_ids"][0].as_u64().unwrap() + 1;
                checkpoint["output_token_ids"][0] = json!(wrong);
                checkpoint["steps"][0]["output_token_id"] = json!(wrong);
                report["challenge_output_token_ids"] = checkpoint["output_token_ids"].clone();
            }
            "stale-member-generation" => {
                let generation = plan["members"]["members"][0]["generation"]
                    .as_u64()
                    .unwrap()
                    + 1;
                plan["members"]["members"][0]["generation"] = json!(generation);
                template["members"] = plan["members"].clone();
            }
            "wrong-resident-digest" => {
                let weights = &mut checkpoint["proofs"][0]["weights"];
                let weight = weights
                    .as_object_mut()
                    .unwrap()
                    .values_mut()
                    .next()
                    .unwrap();
                let part = weight.as_object_mut().unwrap().values_mut().next().unwrap();
                let mut wrong = part["payload_sha256"].as_str().unwrap().to_owned();
                wrong.replace_range(..1, if wrong.starts_with('a') { "b" } else { "a" });
                part["payload_sha256"] = json!(wrong);
                checkpoint["proofs"][0]["weights_digest"] =
                    json!(protocol::canonical::digest(weights).unwrap());
            }
            _ => unreachable!(),
        }
        put("compiled/local-challenge-checkpoint.json", &checkpoint);
        put("compiled/build-report.json", &report);
        put("template.json", &template);
        // Even re-pinning/re-signing the corrupt candidate cannot turn it into
        // genuine native/oracle evidence. These signatures are negative fixtures.
        let names: Vec<_> = plan["documents"]
            .as_object()
            .unwrap()
            .keys()
            .cloned()
            .collect();
        for name in names {
            let doc: Value = serde_json::from_slice(&fs::read(root.join(&name)).unwrap()).unwrap();
            plan["documents"][&name] = json!(protocol::canonical::digest(&doc).unwrap());
        }
        plan.as_object_mut().unwrap().remove("signature");
        plan["signature"] = signer.sign(&plan).unwrap();
        put("compiled/operator-plan.json", &plan);
        let mut complete = read("compile-complete.private.json");
        complete["operator_plan_digest"] = json!(protocol::canonical::digest(&plan).unwrap());
        put("compile-complete.private.json", &complete);
        let tampered: Pins = pins
            .keys()
            .map(|name| {
                use sha2::{Digest, Sha256};
                let bytes = fs::read(root.join(name)).unwrap();
                (
                    name.clone(),
                    Pin {
                        bytes: bytes.len() as u64,
                        sha256: format!("{:x}", Sha256::digest(&bytes)),
                    },
                )
            })
            .collect();
        assert!(
            CompiledHandoff::open(root, &tampered).is_err(),
            "accepted {mutation}"
        );
        eprintln!("native tamper refused: {mutation}");
        for (name, bytes) in &original {
            fs::write(root.join(name), bytes).unwrap();
        }
    }
    let source = Path::new(handoff.plan()["source"]["path"].as_str().unwrap());
    // The test copied this shard into its owned fixture; never write the shared
    // HuggingFace snapshot (nor any source outside the fixture's root).
    assert!(
        source.starts_with(
            root.parent()
                .unwrap()
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .canonicalize()
                .unwrap()
        )
    );
    let mut file = fs::OpenOptions::new()
        .read(true)
        .write(true)
        .open(source)
        .unwrap();
    file.seek(SeekFrom::End(-1)).unwrap();
    let mut original_byte = [0];
    file.read_exact(&mut original_byte).unwrap();
    file.seek(SeekFrom::End(-1)).unwrap();
    file.write_all(&[original_byte[0] ^ 1]).unwrap();
    file.sync_all().unwrap();
    let retained_refused = handoff.revalidate().is_err();
    let reopen_refused = CompiledHandoff::open(root, pins).is_err();
    file.seek(SeekFrom::End(-1)).unwrap();
    file.write_all(&original_byte).unwrap();
    file.sync_all().unwrap();
    assert!(
        retained_refused && reopen_refused,
        "changed shard byte accepted"
    );
    eprintln!("native tamper refused: changed-shard-byte");
    handoff.revalidate().unwrap();
    CompiledHandoff::open(root, pins).unwrap();
    for node in handoff.nodes().values() {
        eprintln!(
            "native resident proof pid={} rows={} digest={}",
            node.load_proof["worker_pid"],
            node.load_proof["weights"].as_object().unwrap().len(),
            node.load_proof["weights_digest"]
        );
    }
}
