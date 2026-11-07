// SPDX-License-Identifier: AGPL-3.0-only
//! The v2 artifact grant the seed delivers beside a seed-signed v2 stage pack: v0.1.4
//! `mycelium_seed/route_assignment.py:770-774, 805-808` (`_artifact_grant` :811-846) ->
//! `mycelium_mobile/artifact_grant.py:196-245` (`issue_grant`) -> `qwen_stage_manifest.py:233-242`
//! (`_verify_architecture`). Every expected value is hand-derived from those lines (no oracle run, never
//! computed by the code under test); the fixtures are hand-written single-layer manifests.
use protocol::artifact_grant::{self as ag, GrantLimits};
use protocol::identity::Identity;
use protocol::qwen_stage_manifest::manifest_digest;
use protocol::route_assignment::{
    GrantAuthority, RouteRow, RouteService, RouteTable, answer_with_grant,
};
use protocol::seed_members::SeedTable;
use serde_json::{Value, json};

const SIGNED: &str = "mycelium.membership.signed_message.v1";
const SWARM: &str = "swarm-1";
const SEED_NODE: &str = "seed-node-1";
const NODE: &str = "node-phone-1";
const ASSIGNMENT: &str = "assignment-1";
/// `now` (seconds) and `int(now * 1000)` (route_assignment.py:826).
const NOW: f64 = 1_800_000_000.0;
const NOW_MS: u64 = 1_800_000_000_000;
/// The dedupe fixture's two object digests (`a` sorts before `b`).
const A: &str = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
const B: &str = "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
/// The architecture fixture's digests: every matrix is `MATRIX`, every vector `VECTOR`.
const MATRIX: &str = "sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const VECTOR: &str = "sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd";
const SOURCE_B: &str = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb";
const SOURCE_C: &str = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc";
const SOURCE_B_KEY: &str =
    "sha256:1111111111111111111111111111111111111111111111111111111111111111";
const SOURCE_C_KEY: &str =
    "sha256:2222222222222222222222222222222222222222222222222222222222222222";
/// `MAX_GRANT_TTL_MS` (artifact_grant.py:38; BudgetId `PyMyceliumMobileArtifactGrantMaxGrantTtlMsL38`).
const MAX_GRANT_TTL_MS: u64 = 21_600_000;

fn seed() -> Identity {
    Identity::from_secret(&"5".repeat(64), &[0x51; 32]).unwrap()
}

fn member() -> Identity {
    Identity::from_secret(&"6".repeat(64), &[0x61; 32]).unwrap()
}

fn limits() -> GrantLimits {
    GrantLimits {
        max_grant_ttl_ms: MAX_GRANT_TTL_MS,
        max_request_ttl_ms: 120_000,
        request_ttl_ms: 60_000,
        clock_skew_ms: 30_000,
    }
}

/// One tensor of layer 0: `(suffix, dtype, shape, size_bytes, sha256)`.
type Tensor = (&'static str, &'static str, Vec<u64>, u64, &'static str);

/// A single-layer v2 manifest (start 0, end 1) over `[hidden, intermediate, heads, kv_heads, head_dim]`, every
/// other field valid, `pack_digest` over the rest (qwen_stage_manifest.py:148-150).
fn manifest(arch: [u64; 5], tensors: Vec<Tensor>) -> Value {
    let [hidden, inner, heads, kv_heads, head_dim] = arch;
    let tensors: Vec<Value> = tensors
        .into_iter()
        .map(|(suffix, dtype, shape, size, digest)| {
            json!({
                "name": format!("model.layers.0.{suffix}"), "dtype": dtype, "shape": shape,
                "size_bytes": size, "sha256": digest,
            })
        })
        .collect();
    let mut document = json!({
        "protocol": "mycelium.mobile_stage_pack.v2",
        "stage_id": "stage-a",
        "model_id": "Qwen/Qwen2.5-0.5B",
        "resolved_commit": "a".repeat(40),
        "source": {
            "file": "model.safetensors",
            "sha256": "sha256:0000000000000000000000000000000000000000000000000000000000000005",
            "size_bytes": 987_654_321,
        },
        "architecture": {
            "model_type": "qwen2", "hidden_act": "silu", "hidden_size": hidden,
            "intermediate_size": inner, "num_attention_heads": heads,
            "num_key_value_heads": kv_heads, "head_dim": head_dim, "num_hidden_layers": 1,
            "rms_norm_eps": 1e-6, "rope_theta": 1_000_000.0,
        },
        "start_layer": 0,
        "end_layer_exclusive": 1,
        "backend": "mobile-rust-qwen2",
        "quantizer": "numpy_rowwise_v1",
        "activation_dtype": "float32",
        "tensors": tensors,
        "golden": null,
        "route_ready": false,
    });
    document["pack_digest"] = Value::String(manifest_digest(&document).unwrap());
    document
}

/// The twelve layer-0 tensors in manifest order (`tensor_names`, sorted) with the shapes `expected_shapes`
/// gives (qwen_stage_manifest.py:152-180; kv = kv_heads * head_dim), all BF16 (2 bytes per element);
/// matrices carry `MATRIX`, vectors `VECTOR`.
fn uniform_layer(arch: [u64; 5]) -> Vec<Tensor> {
    let [h, i, _, kv_heads, head_dim] = arch;
    let kv = kv_heads * head_dim;
    let shapes: [(&'static str, Vec<u64>); 12] = [
        ("input_layernorm.weight", vec![h]),
        ("mlp.down_proj.weight", vec![h, i]),
        ("mlp.gate_proj.weight", vec![i, h]),
        ("mlp.up_proj.weight", vec![i, h]),
        ("post_attention_layernorm.weight", vec![h]),
        ("self_attn.k_proj.bias", vec![kv]),
        ("self_attn.k_proj.weight", vec![kv, h]),
        ("self_attn.o_proj.weight", vec![h, h]),
        ("self_attn.q_proj.bias", vec![h]),
        ("self_attn.q_proj.weight", vec![h, h]),
        ("self_attn.v_proj.bias", vec![kv]),
        ("self_attn.v_proj.weight", vec![kv, h]),
    ];
    shapes
        .into_iter()
        .map(|(suffix, shape)| {
            let digest = if shape.len() == 2 { MATRIX } else { VECTOR };
            let size = shape.iter().product::<u64>() * 2;
            (suffix, "BF16", shape, size, digest)
        })
        .collect()
}

/// Architecture `[hidden, intermediate, heads, kv_heads, head_dim]` with the uniform layer.
fn arch_manifest(arch: [u64; 5]) -> Value {
    manifest(arch, uniform_layer(arch))
}

/// hidden 64, intermediate 16, heads 8, kv_heads 2, head_dim 8 (kv rows 16). Digest `A` is pinned eight
/// times (256, 4096, 4096, 64, 4096, 16384, 32 and finally 4096 bytes), `B` four times (2048, 128, 128 and
/// finally 8192 bytes): `{sha256: size_bytes}` keeps the last size per digest (artifact_grant.py:235), so the
/// grant budget is a:4096 + b:8192 = 12288 (first sizes would give 2304, the largest 24576, all twelve 43616).
fn dedupe_manifest() -> Value {
    manifest(
        [64, 16, 8, 2, 8],
        vec![
            ("input_layernorm.weight", "F32", vec![64], 256, A),
            ("mlp.down_proj.weight", "F32", vec![64, 16], 4096, A),
            ("mlp.gate_proj.weight", "BF16", vec![16, 64], 2048, B),
            ("mlp.up_proj.weight", "F32", vec![16, 64], 4096, A),
            ("post_attention_layernorm.weight", "BF16", vec![64], 128, B),
            ("self_attn.k_proj.bias", "F32", vec![16], 64, A),
            ("self_attn.k_proj.weight", "F32", vec![16, 64], 4096, A),
            ("self_attn.o_proj.weight", "F32", vec![64, 64], 16384, A),
            ("self_attn.q_proj.bias", "BF16", vec![64], 128, B),
            ("self_attn.q_proj.weight", "BF16", vec![64, 64], 8192, B),
            ("self_attn.v_proj.bias", "BF16", vec![16], 32, A),
            ("self_attn.v_proj.weight", "F32", vec![16, 64], 4096, A),
        ],
    )
}

/// The manifest seed-signed as a member-channel message (signed directly, so a manifest the verifier refuses can
/// still be stored, as a v0.1.4 database can hold it).
fn signed_pack(manifest: &Value) -> Value {
    let seed = seed();
    json!({
        "protocol": SIGNED,
        "message": manifest,
        "signature": seed.sign(manifest).unwrap(),
        "verification_key": seed.public_key_record(),
    })
}

/// One source with its direct-address hint, one without (artifact_grant.py:229-231 adds `direct_addrs: []`),
/// stored out of `endpoint_id` order.
fn two_sources() -> Value {
    json!([
        {"endpoint_id": SOURCE_C, "verification_key_digest": SOURCE_C_KEY, "direct_addrs": ["192.0.2.7:4433"]},
        {"endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY},
    ])
}

fn one_source() -> Value {
    json!([{"endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY, "direct_addrs": []}])
}

/// A RUNNING, activation-eligible member at generation 1 whose lease outlives the request.
fn seed_table(member: &Identity) -> SeedTable {
    let snapshot = json!({
        "members": [{
            "node_id": NODE, "endpoint_id": member.endpoint_id(), "endpoint_addrs": [],
            "peer_class": "android_native_qwen2_iroh",
            "runtime_capability": {
                "runtime_backend": "mobile-rust-qwen2", "transport": "iroh",
                "activation_protocol": "mycelium.router_wire.v1",
            },
            "verification_key_digest": member.verification_key_digest(),
            "incarnation": "boot-1", "generation": 1, "lease_expires_at": NOW + 300.0,
            "lifecycle_state": "RUNNING", "last_heartbeat_sequence": 0, "last_liveness_at": NOW,
            "next_heartbeat_due_at": NOW + 30.0, "last_activity_receipt_at": null,
            "active_requests": 0,
        }],
        "replay": [], "renewals": [], "join_rows": [], "resume_rows": [], "assignments": [],
        "rotations": [], "acks": [], "authority_generation": 0, "emitted": [],
    });
    SeedTable::restore(&snapshot, Box::new(|| String::from("unused"))).unwrap()
}

/// The member's signed route-assignment request.
fn request(member: &Identity) -> Value {
    let message = json!({
        "protocol": "mycelium.seed.route_assignment_request.v1",
        "message_id": "route-request-1", "swarm_id": SWARM, "sender_node_id": NODE,
        "sender_endpoint_id": member.endpoint_id(), "recipient_node_id": SEED_NODE,
        "incarnation": "boot-1", "generation": 1, "issued_at": NOW, "expires_at": NOW + 60.0,
        "applied_assignment_id": null,
    });
    json!({
        "protocol": SIGNED,
        "message": message,
        "signature": member.sign(&message).unwrap(),
        "verification_key": member.public_key_record(),
    })
}

struct Outcome {
    got: Result<Value, &'static str>,
    routes: RouteTable,
    grant_ids_drawn: usize,
}

/// The seed answers the member's request for its one assignment row: `stage_pack` stored with
/// `artifact_sources`, the row expiring at `row_expires_at`.
fn deliver(stage_pack: &Value, artifact_sources: Option<Value>, row_expires_at: f64) -> Outcome {
    let (seed, member) = (seed(), member());
    let mut table = seed_table(&member);
    let mut routes = RouteTable::default();
    routes.insert_replace(RouteRow {
        node_id: NODE.to_owned(),
        assignment_id: ASSIGNMENT.to_owned(),
        endpoint_id: member.endpoint_id().to_owned(),
        verification_key_digest: member.verification_key_digest(),
        stage_pack: stage_pack.clone(),
        stage_pack_digest: stage_pack["message"]["pack_digest"]
            .as_str()
            .unwrap_or_default()
            .to_owned(),
        peer_endpoint_id: "7".repeat(64),
        route_generation: 1,
        forward_to: false,
        issued_at: NOW - 10.0,
        expires_at: row_expires_at,
        controller_endpoint_ids_json: String::from("[]"),
        withdrawn: false,
        delivered_at: None,
        delivered_generation: None,
        applied_at: None,
        applied_generation: None,
        artifact_sources,
    });
    let service = RouteService {
        swarm_id: SWARM,
        seed_node_id: SEED_NODE,
        seed_endpoint_id: seed.endpoint_id(),
        incarnation: "seed-boot-1",
    };
    let mut drawn = 0usize;
    let mut grant_ids = || {
        drawn += 1;
        format!("grant-{drawn}")
    };
    let mut authority = GrantAuthority {
        signer: &seed,
        grant_ids: &mut grant_ids,
        limits: limits(),
    };
    let got = answer_with_grant(
        &mut table,
        &mut routes,
        &service,
        &mut authority,
        &request(&member),
        NOW,
        &mut || String::from("route-delivery-0"),
    )
    .map_err(|e| e.0);
    Outcome {
        got,
        routes,
        grant_ids_drawn: drawn,
    }
}

/// One field of the delivered grant's message, or the refusal.
fn grant_field(got: &Result<Value, &'static str>, field: &str) -> Result<Value, &'static str> {
    got.as_ref()
        .map(|d| d["artifact_grant"]["message"][field].clone())
        .map_err(|e| *e)
}

fn refusal(got: &Result<Value, &'static str>) -> Result<(), &'static str> {
    got.as_ref().map(|_| ()).map_err(|e| *e)
}

/// A valid architecture control is granted: matrices and vectors deduplicate to the last matrix
/// (`v_proj.weight`, 512 bytes at hidden 16 / kv rows 16) plus the last vector (`v_proj.bias`, 32 bytes).
fn assert_control_granted(arch: [u64; 5]) {
    let pack = signed_pack(&arch_manifest(arch));
    let got = deliver(&pack, Some(one_source()), NOW + 7200.0).got;
    assert_eq!(
        grant_field(&got, "allowed_object_digests"),
        Ok(json!([MATRIX, VECTOR])),
        "{arch:?}"
    );
    assert_eq!(
        grant_field(&got, "maximum_total_bytes"),
        Ok(json!(544)),
        "{arch:?}"
    );
    assert_eq!(
        grant_field(&got, "manifest_pack_digest"),
        Ok(pack["message"]["pack_digest"].clone()),
        "{arch:?}"
    );
}

fn assert_architecture_refused(arch: [u64; 5]) {
    let got = deliver(
        &signed_pack(&arch_manifest(arch)),
        Some(one_source()),
        NOW + 7200.0,
    )
    .got;
    assert_eq!(
        refusal(&got),
        Err("stage_manifest_architecture_invalid"),
        "{arch:?}"
    );
}

#[test]
fn route_assignment_v2_grant_dedupes_repeated_digests() {
    let pack = signed_pack(&dedupe_manifest());
    let got = deliver(&pack, Some(one_source()), NOW + 7200.0).got;
    assert_eq!(
        grant_field(&got, "allowed_object_digests"),
        Ok(json!([A, B]))
    );
    assert_eq!(grant_field(&got, "maximum_total_bytes"), Ok(json!(12288)));
    // The pack travels unchanged beside its grant (route_assignment.py:802-808).
    assert_eq!(
        got.as_ref()
            .map(|d| d["stage_pack"].clone())
            .map_err(|e| *e),
        Ok(pack)
    );
}

#[test]
fn route_assignment_v2_grant_ttl_is_min_of_default_and_row_expiry() {
    let pack = signed_pack(&dedupe_manifest());
    // Row expires in 60 s: min(900000, 1800000060000 - 1800000000000) = 60000 (route_assignment.py:827).
    let soon = deliver(&pack, Some(one_source()), NOW + 60.0).got;
    assert_eq!(
        grant_field(&soon, "issued_at_unix_ms"),
        Ok(json!(1_800_000_000_000u64))
    );
    assert_eq!(
        grant_field(&soon, "not_before_unix_ms"),
        Ok(json!(1_800_000_000_000u64))
    );
    assert_eq!(
        grant_field(&soon, "expires_at_unix_ms"),
        Ok(json!(1_800_000_060_000u64))
    );
    // Row expires in 2 h: min(900000, 7200000) = 900000 (DEFAULT_GRANT_TTL_MS, artifact_grant.py:37).
    let later = deliver(&pack, Some(one_source()), NOW + 7200.0).got;
    assert_eq!(
        grant_field(&later, "expires_at_unix_ms"),
        Ok(json!(1_800_000_900_000u64))
    );
}

#[test]
fn route_assignment_v2_grant_missing_sources_refused() {
    let pack = signed_pack(&dedupe_manifest());
    let missing = deliver(&pack, None, NOW + 7200.0);
    assert_eq!(
        refusal(&missing.got),
        Err("seed_route_assignment_artifact_sources_required")
    );
    // The refusal follows the committed delivery bookkeeping (route_assignment.py:738-746 before :824-825) and
    // precedes the grant id (:831).
    assert_eq!(
        missing
            .routes
            .row_state(NODE)
            .map(|row| row["delivered_generation"].clone()),
        Some(json!(1))
    );
    assert_eq!(missing.grant_ids_drawn, 0);
    // A stored but empty or duplicate-only source list leaves no authorized source: `_check_grant_shape` refuses
    // (artifact_grant.py:261 `_sorted_unique`, :272-273 ids sorted and unique), wrapped as :843-844.
    for sources in [
        json!([]),
        json!([
            {"endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY},
            {"endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY},
        ]),
    ] {
        let got = deliver(&pack, Some(sources.clone()), NOW + 7200.0).got;
        assert_eq!(
            refusal(&got),
            Err("seed_route_assignment_grant_invalid:artifact_grant_invalid"),
            "{sources}"
        );
    }
    // Control: the same pack with its one source is granted.
    let granted = deliver(&pack, Some(one_source()), NOW + 7200.0).got;
    assert_eq!(
        grant_field(&granted, "maximum_total_bytes"),
        Ok(json!(12288))
    );
}

#[test]
fn route_assignment_v2_grant_heads_above_hidden_refused() {
    assert_control_granted([16, 32, 8, 8, 2]);
    // num_attention_heads = hidden_size + 1 (qwen_stage_manifest.py:235 maximum=hidden).
    assert_architecture_refused([16, 32, 17, 8, 2]);
    // heads == hidden: head_dim 1 is below :237's minimum 2, and head_dim 2 breaks :241's heads * head_dim ==
    // hidden.
    assert_architecture_refused([16, 32, 16, 16, 1]);
    assert_architecture_refused([16, 32, 16, 16, 2]);
}

#[test]
fn route_assignment_v2_grant_kv_heads_above_heads_refused() {
    // kv_heads == heads is the bound itself (:236 maximum=heads).
    assert_control_granted([16, 32, 8, 8, 2]);
    // num_key_value_heads = num_attention_heads + 1 (:236).
    assert_architecture_refused([16, 32, 8, 9, 2]);
    // kv_heads within the bound but not dividing heads (:241 heads % kv_heads).
    assert_architecture_refused([16, 32, 8, 3, 2]);
}

#[test]
fn route_assignment_v2_grant_head_dim_above_hidden_refused() {
    // head_dim == hidden is the bound itself (:237 maximum=hidden), with one head of one kv head.
    assert_control_granted([16, 32, 1, 1, 16]);
    // head_dim = hidden_size + 1 (:237).
    assert_architecture_refused([16, 32, 8, 8, 17]);
    // An odd head_dim whose product still equals hidden (5 * 3 == 15; :241 head_dim % 2).
    assert_architecture_refused([15, 32, 5, 5, 3]);
}

#[test]
fn route_assignment_v2_grant_issued_envelope_verifies() {
    let (seed, member) = (seed(), member());
    let pack = signed_pack(&dedupe_manifest());
    let pack_digest = pack["message"]["pack_digest"].clone();
    let outcome = deliver(&pack, Some(two_sources()), NOW + 7200.0);
    let got = &outcome.got;
    // artifact_grant.py:220-242: the seed's binding to this member and assignment, the sources sorted by
    // endpoint id with `direct_addrs` defaulted, the default rate and chunk caps (32 << 20, 4 << 20).
    assert_eq!(
        got.as_ref()
            .map(|d| d["artifact_grant"]["message"].clone())
            .map_err(|e| *e),
        Ok(json!({
            "protocol": "mycelium.mobile_artifact_grant.v1",
            "grant_id": "grant-1",
            "swarm_id": SWARM,
            "seed_node_id": SEED_NODE,
            "recipient_node_id": NODE,
            "recipient_endpoint_id": member.endpoint_id(),
            "recipient_key_digest": member.verification_key_digest(),
            "assignment_id": ASSIGNMENT,
            "manifest_pack_digest": pack_digest,
            "allowed_object_digests": [A, B],
            "authorized_sources": [
                {"direct_addrs": [], "endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY},
                {"direct_addrs": ["192.0.2.7:4433"], "endpoint_id": SOURCE_C, "verification_key_digest": SOURCE_C_KEY},
            ],
            "maximum_total_bytes": 12288,
            "maximum_bytes_per_second": 33_554_432,
            "maximum_chunk_bytes": 4_194_304,
            "issued_at_unix_ms": 1_800_000_000_000u64,
            "not_before_unix_ms": 1_800_000_000_000u64,
            "expires_at_unix_ms": 1_800_000_900_000u64,
        }))
    );
    assert_eq!(outcome.grant_ids_drawn, 1);
    let envelope = got
        .as_ref()
        .map(|d| d["artifact_grant"].clone())
        .unwrap_or(Value::Null);
    // The member verifies it under the pinned seed key, and it is bound to this member and manifest.
    let verified = ag::verify_grant(&envelope, &seed.public_key_record(), NOW_MS, limits());
    assert_eq!(
        verified
            .as_ref()
            .map(|g| g.grant_id.as_str())
            .map_err(|e| e.0),
        Ok("grant-1")
    );
    assert_eq!(
        verified.as_ref().map_err(|e| e.0).and_then(|g| g
            .check_binding(
                NODE,
                member.endpoint_id(),
                &member.verification_key_digest(),
                pack_digest.as_str().unwrap_or_default(),
            )
            .map_err(|e| e.0)),
        Ok(())
    );
    // Near-miss: another key pinned as the seed's is refused.
    assert_eq!(
        ag::verify_grant(&envelope, &member.public_key_record(), NOW_MS, limits()).map(|_| ()),
        Err(ag::GrantError("artifact_grant_signer_mismatch"))
    );
}

/// `SOURCE_B` stored with exactly these `direct_addrs` (artifact_grant.py:230-232 keeps a present value).
fn source_b_with(direct_addrs: Value) -> Value {
    json!([{"endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY, "direct_addrs": direct_addrs}])
}

/// The grant message `issue_grant` builds for the dedupe pack and `SOURCE_B` with `direct_addrs`
/// (artifact_grant.py:220-241): the same literal fields as `route_assignment_v2_grant_issued_envelope_verifies`.
fn dedupe_grant_message(member: &Identity, pack_digest: &Value, direct_addrs: Value) -> Value {
    json!({
        "protocol": "mycelium.mobile_artifact_grant.v1",
        "grant_id": "grant-1",
        "swarm_id": SWARM,
        "seed_node_id": SEED_NODE,
        "recipient_node_id": NODE,
        "recipient_endpoint_id": member.endpoint_id(),
        "recipient_key_digest": member.verification_key_digest(),
        "assignment_id": ASSIGNMENT,
        "manifest_pack_digest": pack_digest,
        "allowed_object_digests": [A, B],
        "authorized_sources": [
            {"direct_addrs": direct_addrs, "endpoint_id": SOURCE_B, "verification_key_digest": SOURCE_B_KEY},
        ],
        "maximum_total_bytes": 12288,
        "maximum_bytes_per_second": 33_554_432,
        "maximum_chunk_bytes": 4_194_304,
        "issued_at_unix_ms": 1_800_000_000_000u64,
        "not_before_unix_ms": 1_800_000_000_000u64,
        "expires_at_unix_ms": 1_800_000_900_000u64,
    })
}

/// `message` seed-signed as a grant envelope.
fn seed_signed(message: &Value) -> Value {
    let seed = seed();
    json!({
        "protocol": SIGNED,
        "message": message,
        "signature": seed.sign(message).unwrap(),
        "verification_key": seed.public_key_record(),
    })
}

/// The seed answers with `SOURCE_B` carrying `direct_addrs`; the delivered grant message must be exactly the
/// literal one, and the member must verify the delivered envelope under the pinned seed key. Returns the verified
/// grant's id and its typed hints for `SOURCE_B`, or the refusal.
fn assert_delivered_and_verified(
    direct_addrs: Value,
) -> Result<(String, Option<Vec<String>>), &'static str> {
    let (seed, member) = (seed(), member());
    let pack = signed_pack(&dedupe_manifest());
    let pack_digest = pack["message"]["pack_digest"].clone();
    let outcome = deliver(
        &pack,
        Some(source_b_with(direct_addrs.clone())),
        NOW + 7200.0,
    );
    assert_eq!(
        outcome
            .got
            .as_ref()
            .map(|d| d["artifact_grant"]["message"].clone())
            .map_err(|e| *e),
        Ok(dedupe_grant_message(
            &member,
            &pack_digest,
            direct_addrs.clone()
        )),
        "{direct_addrs}"
    );
    assert_eq!(outcome.grant_ids_drawn, 1, "{direct_addrs}");
    let envelope = outcome
        .got
        .as_ref()
        .map(|d| d["artifact_grant"].clone())
        .unwrap_or(Value::Null);
    ag::verify_grant(&envelope, &seed.public_key_record(), NOW_MS, limits())
        .map(|g| {
            let hints = g
                .source_addrs
                .get(SOURCE_B)
                .map(|hints| hints.iter().map(ToString::to_string).collect());
            (g.grant_id, hints)
        })
        .map_err(|e| e.0)
}

/// A `null` hint: `canonical_socket_addr(None)` is `None` (artifact_grant.py:180-181), so the shape rule
/// `canonical_socket_addr(addr) != addr` (:269-271) holds for it; the product writer applies the same predicate
/// (route_assignment.py:471), `issue_grant` signs the source as stored (:230-232, :242-243) and `verify_grant`
/// re-runs the same shape check (:289). The typed hints keep string hints only, as the oracle's consumer does
/// (`[str(a) for a in direct_addrs if isinstance(a, str)]`, scripts/run_a12_phone_route.py:820). Checked against
/// an execution of the read-only oracle (f4e366e9): answer -> grant with `direct_addrs: [None]`, verify ok.
#[test]
fn route_assignment_v2_grant_null_direct_addr_matches_oracle() {
    assert_eq!(
        assert_delivered_and_verified(json!([null])),
        Ok((String::from("grant-1"), Some(Vec::new())))
    );
}

/// A scoped IPv6 hint: `ipaddress.ip_address("fe80::1%en0")` keeps the scope (`_split_scope_id`), its text form
/// is `fe80::1%en0`, so `canonical_socket_addr("[fe80::1%en0]:4433")` is the input itself (artifact_grant.py:183-189)
/// and the grant is issued and verified with the hint byte for byte; likewise a numeric scope. Checked against an
/// execution of the read-only oracle (f4e366e9): both answered with the hint preserved, verify ok.
#[test]
fn route_assignment_v2_grant_scoped_ipv6_direct_addr_matches_oracle() {
    for hint in ["[fe80::1%en0]:4433", "[fe80::1%2]:4433"] {
        assert_eq!(
            assert_delivered_and_verified(json!([hint])),
            Ok((String::from("grant-1"), Some(vec![hint.to_owned()]))),
            "{hint}"
        );
    }
}

/// Hints whose `canonical_socket_addr` (artifact_grant.py:175-189) is not the hint itself, each refused by the
/// shape rule (:269-271) on both sides: answering wraps it as `seed_route_assignment_grant_invalid:` (route_assignment.py
/// :843-844), `verify_grant` of the same message seed-signed refuses `artifact_grant_invalid` (:289). Each row was
/// run through the read-only oracle (f4e366e9) with these exact codes.
#[test]
fn route_assignment_v2_grant_noncanonical_direct_addr_refused() {
    let member = member();
    let pack = signed_pack(&dedupe_manifest());
    let pack_digest = pack["message"]["pack_digest"].clone();
    // Control: a canonical v4 hint is granted and verifies.
    let control = dedupe_grant_message(&member, &pack_digest, json!(["192.0.2.7:4433"]));
    assert_eq!(
        grant_field(
            &deliver(
                &pack,
                Some(source_b_with(json!(["192.0.2.7:4433"]))),
                NOW + 7200.0
            )
            .got,
            "authorized_sources"
        ),
        Ok(control["authorized_sources"].clone())
    );
    assert_eq!(
        ag::verify_grant(
            &seed_signed(&control),
            &seed().public_key_record(),
            NOW_MS,
            limits()
        )
        .map(|g| g.grant_id),
        Ok(String::from("grant-1"))
    );
    let rows: [(&str, Value); 22] = [
        // An uncompressed zero hextet: the text form is `fe80::1` (`_compress_hextets`).
        ("v6 not compressed", json!(["[fe80:0::1]:4433"])),
        ("v6 upper case", json!(["[FE80::1]:4433"])),
        ("v6 full form", json!(["[0:0:0:0:0:0:0:1]:4433"])),
        // rpartition splits the last `:`; `fe80::1` renders bracketed.
        ("v6 unbracketed", json!(["fe80::1:4433"])),
        // A v4-mapped address renders dotted, `::ffff:192.0.2.1` (`_ipv4_mapped_ipv6_to_str`).
        ("v4-mapped in hex", json!(["[::ffff:c000:201]:4433"])),
        ("v6 empty scope", json!(["[fe80::1%]:4433"])),
        ("v6 second scope separator", json!(["[fe80::1%en0%1]:4433"])),
        ("v6 slash in scope", json!(["[fe80::1%en/0]:4433"])),
        ("v4 bracketed", json!(["[192.0.2.7]:4433"])),
        ("v4 leading-zero octet", json!(["192.0.2.07:4433"])),
        ("host name", json!(["localhost:4433"])),
        ("port 0", json!(["192.0.2.7:0"])),
        ("leading-zero port", json!(["192.0.2.7:04433"])),
        ("port 65536", json!(["192.0.2.7:65536"])),
        ("missing port", json!(["192.0.2.7"])),
        ("empty port", json!(["192.0.2.7:"])),
        ("leading space", json!([" 192.0.2.7:4433"])),
        ("trailing space", json!(["192.0.2.7:4433 "])),
        ("empty string", json!([""])),
        ("integer", json!([1])),
        ("boolean", json!([true])),
        // One bad hint among good ones refuses the source.
        ("one of two", json!(["192.0.2.7:4433", "[FE80::1]:4433"])),
    ];
    for (name, direct_addrs) in rows {
        let got = deliver(
            &pack,
            Some(source_b_with(direct_addrs.clone())),
            NOW + 7200.0,
        )
        .got;
        assert_eq!(
            refusal(&got),
            Err("seed_route_assignment_grant_invalid:artifact_grant_invalid"),
            "{name}: {direct_addrs}"
        );
        let message = dedupe_grant_message(&member, &pack_digest, direct_addrs.clone());
        assert_eq!(
            ag::verify_grant(
                &seed_signed(&message),
                &seed().public_key_record(),
                NOW_MS,
                limits()
            )
            .map(|g| g.grant_id),
            Err(ag::GrantError("artifact_grant_invalid")),
            "{name}: {direct_addrs}"
        );
    }
}
