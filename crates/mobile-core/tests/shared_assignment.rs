// SPDX-License-Identifier: AGPL-3.0-only
use mycelium_mobile_core as mobile;
#[test]
fn assignment_and_seed_projections_use_the_shared_pure_implementation() {
    let raw = include_bytes!("../../protocol/fixtures/contracts/layer-assignment-v2.json");
    let assignment = mobile::strict::object(raw, raw.len()).unwrap();
    mobile::assignment::validate_identity(&assignment).unwrap();
    assert_eq!(
        mobile::assignment::id_for(&assignment).unwrap(),
        assignment["assignment_id"].as_str().unwrap()
    );
    let raw = concat!(
        "{\"rows\":",
        include_str!("../../protocol/fixtures/round3-seed-vectors.json"),
        "}"
    )
    .as_bytes();
    let records = mobile::strict::object(raw, raw.len()).unwrap();
    let row = &records["rows"][0];
    let bytes = row["value"]["raw"].as_str().unwrap().as_bytes();
    let manifest = mobile::operator_projection::backup_manifest(bytes).unwrap();
    assert_eq!(
        mobile::canonical::to_bytes(&manifest).unwrap(),
        row["canonical_utf8"].as_str().unwrap().as_bytes()
    );
    let row = &records["rows"][3];
    let v = &row["value"];
    mobile::operator_projection::validate_seed_revocation_reason(&v["reason"]).unwrap();
    let result = mobile::operator_projection::seed_revocation_projection(
        &v["binding"],
        &v["member"],
        &v["expected_generation"],
        &v["reason"],
        &v["now"],
    )
    .unwrap();
    assert_eq!(
        mobile::canonical::to_bytes(&result).unwrap(),
        row["canonical_utf8"].as_str().unwrap().as_bytes()
    );
}
