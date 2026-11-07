// SPDX-License-Identifier: AGPL-3.0-only
use base64::{Engine, engine::general_purpose::STANDARD};
use protocol::{
    artifact_grant::{self as ag, GrantLimits},
    identity::Identity,
    remote::*,
};
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!("../fixtures/artifact_grant.json")).unwrap()
}
fn limits() -> GrantLimits {
    GrantLimits {
        max_grant_ttl_ms: 21_600_000,
        max_request_ttl_ms: 120_000,
        request_ttl_ms: 60_000,
        clock_skew_ms: 30_000,
    }
}

#[test]
fn grant_request_receipt_match_python_bytes() {
    let f = fixture();
    let now = f["now_unix_ms"].as_u64().unwrap();
    let grant = ag::verify_grant(&f["grant"], &f["seed_key"], now, limits()).unwrap();
    let member = Identity::from_secret(&"a".repeat(64), &[0x11; 32]).unwrap();
    let source = Identity::from_secret(&"c".repeat(64), &[0x33; 32]).unwrap();
    grant
        .check_binding(
            "node-phone-1",
            member.endpoint_id(),
            &member.verification_key_digest(),
            &grant.manifest_pack_digest,
        )
        .unwrap();
    assert_eq!(
        grant
            .check_binding(
                "other",
                member.endpoint_id(),
                &member.verification_key_digest(),
                &grant.manifest_pack_digest
            )
            .unwrap_err()
            .0,
        "artifact_grant_recipient_mismatch"
    );
    let (request, checked) = ag::verify_chunk_request(
        &f["request"],
        &f["seed_key"],
        source.endpoint_id(),
        &source.verification_key_digest(),
        &|_| Some(256),
        now + 1000,
        limits(),
    )
    .unwrap();
    assert_eq!(checked, grant);
    assert_eq!(
        ag::sign_chunk_request(
            &member,
            &grant,
            "chunk-request-1",
            "nonce-1",
            source.endpoint_id(),
            &request.object_digest,
            16,
            32,
            now + 1000,
            limits()
        )
        .unwrap(),
        f["request"]
    );
    let data = STANDARD.decode(f["data_b64"].as_str().unwrap()).unwrap();
    ag::verify_chunk_receipt(&f["receipt"], &request, &grant, &data).unwrap();
    assert_eq!(
        ag::sign_chunk_receipt(&source, &request, &grant, &data, now + 2000).unwrap(),
        f["receipt"]
    );
    let mut wrong = data;
    wrong[0] ^= 1;
    assert_eq!(
        ag::verify_chunk_receipt(&f["receipt"], &request, &grant, &wrong)
            .unwrap_err()
            .0,
        "artifact_chunk_receipt_mismatch"
    );
}

#[test]
fn grants_refuse_expiry_key_signature_scope_and_shape() {
    let f = fixture();
    let now = f["now_unix_ms"].as_u64().unwrap();
    let seed = &f["seed_key"];
    assert_eq!(
        ag::verify_grant(&f["grant"], &json!({}), now, limits())
            .unwrap_err()
            .0,
        "artifact_grant_signer_mismatch"
    );
    assert_eq!(
        ag::verify_grant(&f["grant"], seed, now + 930001, limits())
            .unwrap_err()
            .0,
        "artifact_grant_ineligible"
    );
    let mut bad = f["grant"].clone();
    bad["message"]["maximum_chunk_bytes"] = 1.into();
    assert_eq!(
        ag::verify_grant(&bad, seed, now, limits()).unwrap_err().0,
        "artifact_grant_signature_invalid"
    );
    bad = f["grant"].clone();
    bad["message"]["allowed_object_digests"]
        .as_array_mut()
        .unwrap()
        .swap(0, 1);
    assert_eq!(
        ag::verify_grant(&bad, seed, now, limits()).unwrap_err().0,
        "artifact_grant_invalid"
    );
    assert_eq!(
        ag::verify_chunk_request(
            &f["request"],
            seed,
            &"d".repeat(64),
            "wrong",
            &|_| Some(256),
            now + 1000,
            limits()
        )
        .unwrap_err()
        .0,
        "artifact_chunk_request_unauthorized"
    );
}

#[test]
fn extreme_caller_clock_is_refused_not_overflowed() {
    let f = fixture();
    assert_eq!(
        ag::verify_grant(&f["grant"], &f["seed_key"], u64::MAX, limits())
            .unwrap_err()
            .0,
        "artifact_grant_ineligible"
    );
}

#[test]
fn remote_framing_has_exact_version_kind_id_and_big_endian_length() {
    let mut expected = vec![1, 1];
    expected.extend_from_slice(&[4; 16]);
    expected.extend_from_slice(&[0, 0, 0, 5]);
    expected.extend_from_slice(b"frame");
    assert_eq!(
        encode_remote_frame(RemoteKind::Transfer, [4; 16], b"frame").unwrap(),
        expected
    );
    assert_eq!(decode_remote_frame(&expected).unwrap().payload, b"frame");
    let mut bad = expected.clone();
    bad[0] = 2;
    assert_eq!(
        decode_remote_frame(&bad),
        Err(RemoteProtocolError::UnknownVersion)
    );
    bad = expected.clone();
    bad[1] = 4;
    assert_eq!(
        decode_remote_frame(&bad),
        Err(RemoteProtocolError::UnknownKind)
    );
    bad = expected.clone();
    bad.push(0);
    assert_eq!(
        decode_remote_frame(&bad),
        Err(RemoteProtocolError::LengthMismatch)
    );
    bad = expected;
    bad[18..22].copy_from_slice(&u32::MAX.to_be_bytes());
    assert_eq!(
        decode_remote_frame(&bad),
        Err(RemoteProtocolError::PayloadTooLarge)
    );
}
