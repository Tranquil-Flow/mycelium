// SPDX-License-Identifier: AGPL-3.0-only
//! Real L5 signatures and L9 receipt verification over development route identity.
use crate::loopback::Result;
use protocol::{
    canonical,
    identity::Identity,
    token::{GrantExpectation, GrantRegistry, GrantWindow},
};
use serde_json::{Value, json};
use std::io::Read;
pub fn identity(endpoint: &str) -> Result<Identity> {
    let mut secret = [0u8; 32];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut secret)?;
    let identity = Identity::from_secret(endpoint, &secret)?;
    secret.fill(0);
    Ok(identity)
}
pub fn wall_ms() -> Result<u64> {
    Ok(u64::try_from(
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_millis(),
    )?)
}
pub struct Receipts {
    pub subject: Value,
    pub scope: Value,
    digest: String,
    previous: String,
    sequence: u64,
    index: u64,
}
impl Receipts {
    pub fn open(
        owner: &Identity,
        member: (&Value, &str),
        id: &str,
        generation: u64,
        document: &Value,
        elapsed: std::time::Duration,
        remaining: std::time::Duration,
    ) -> Result<(Self, runtime::composition::Session)> {
        let (member_key, member_endpoint) = member;
        let now = wall_ms()?;
        let end = now
            .checked_add(u64::try_from(remaining.as_millis())?)
            .ok_or("expiry_overflow")?;
        // Receipts are presented at seconds since the job began, so the window lives on
        // that clock: it ends `remaining` after the moment of admission (`elapsed`).
        let deadline_on_job_clock = (elapsed + remaining).as_secs_f64();
        let q = &document["qualification"];
        let member_digest = member_key["verification_key_digest"]
            .as_str()
            .ok_or("member_key_missing")?;
        if member_digest.len() != 71
            || !member_digest.starts_with("sha256:")
            || !member_digest.as_bytes()[7..]
                .iter()
                .all(u8::is_ascii_hexdigit)
        {
            return Err("member_key_invalid".into());
        }
        if member_endpoint.is_empty() {
            return Err("member_endpoint_missing".into());
        }
        let scope = json!({"run_id":format!("slice-{generation}"),"request_id":id,"requester_id":"loopback-client",
            "owner_endpoint_id":owner.endpoint_id(),"owner_key_digest":owner.verification_key_digest(),
            "entry_node_id":"a-owner","entry_endpoint_id":owner.endpoint_id(),"entry_key_digest":owner.verification_key_digest(),
            "participants":[{"node_id":"a-owner","endpoint_id":owner.endpoint_id(),"generation":generation},{"node_id":"b-member","endpoint_id":member_endpoint,"generation":generation}],
            "control":{"deployment_id":"slice-development","deployment_epoch":generation,"qualification_digest":q["qualification_digest"],
            "command_id":id,"publisher_generation":generation,"absolute_deadline_ms":end,"request_attempt":1,"path_id":format!("path-{generation}"),"path_attempt":0,
            "path_digest":q["path_manifest_digest"],"topology_generation":generation,"cancellation_generation":0},
            "model_digest":q["manifest_digest"],"request_digest":canonical::digest(document)?});
        // member_key is an independently retained hello pin, not a fabricated peer key.
        if member_key["verification_key_digest"].as_str().is_none() {
            return Err("member_key_missing".into());
        }
        let stops = json!([151643, 151645]);
        let statement = json!({"protocol":protocol::token::GRANT_PROTOCOL,"scope":scope,"max_new_tokens":document["max_new_tokens"],"stop_token_ids":stops,"expires_at_unix_ms":end});
        let envelope = json!({"statement":statement,"signature":owner.sign(&statement)?});
        let grant = GrantRegistry::new(1)?.admit(
            &envelope,
            &GrantExpectation {
                scope: &scope,
                owner_key: &owner.public_key_record(),
                max_new_tokens: document["max_new_tokens"].as_u64().ok_or("token_limit")?,
                stop_token_ids: &stops,
                window: GrantWindow {
                    now_unix_ms: now,
                    now_monotonic_s: elapsed.as_secs_f64(),
                    process_deadline_monotonic_s: deadline_on_job_clock,
                    lease_deadline_monotonic_s: deadline_on_job_clock,
                    clock_uncertainty_ms: 0,
                },
            },
        )?;
        let digest = grant.digest().to_owned();
        let mut subject = scope["control"].clone();
        subject["request_id"] = json!(id);
        subject["cancellation_generation"] = json!(1);
        let session = runtime::composition::Session::new(
            grant,
            owner.public_key_record(),
            subject.clone(),
            Some(1),
        )?;
        Ok((
            Self {
                subject,
                scope,
                digest: digest.clone(),
                previous: digest,
                sequence: 0,
                index: 0,
            },
            session,
        ))
    }
    pub fn sign(
        &mut self,
        identity: &Identity,
        tokens: &[u32],
        completed: bool,
        stage_digest: &str,
    ) -> Result<Value> {
        let end = self
            .index
            .checked_add(tokens.len() as u64)
            .ok_or("index_overflow")?;
        let statement = json!({"protocol":protocol::token::RECEIPT_PROTOCOL,"grant_digest":self.digest,"sequence":self.sequence,"start_index":self.index,"end_index":end,"token_ids":tokens,
            "status":if completed{"COMPLETED"}else{"DECODING"},"error_code":null,"stage_receipt_refs":[{"node_id":"b-member","receipt_digest":stage_digest}],"previous_receipt_digest":self.previous});
        let envelope = json!({"signature":identity.sign(&statement)?,"statement":statement});
        self.previous = canonical::digest(&statement)?;
        self.sequence += 1;
        self.index = end;
        Ok(envelope)
    }
}
/// Call only AFTER native sessions/weights/context have actually closed. The owner
/// additionally reaps the exact child; this signed assertion alone is insufficient.
pub fn cleaned(identity: &Identity, subject: &Value, generation: u64, node: &str) -> Result<Value> {
    let mut cleanup = subject.clone();
    for key in [
        "runtime_clean",
        "transport_clean",
        "cancellation_worker_complete",
        "complete",
    ] {
        cleanup[key] = json!(true);
    }
    let observation = json!({"protocol":"mycelium.physical_node_observation.v1","event":"inference_cancelled","monotonic_ns":0,
        "run_id":format!("slice-{generation}"),"deployment_id":"slice-development","node_id":node,"host_id":"loopback-mac",
        "process_id":std::process::id(),"endpoint_id":identity.endpoint_id(),"peer_generation":generation,"state":"STOPPED","route_ready":false,
        "details":{"request_cleanup":cleanup}});
    Ok(
        json!({"ok":true,"result":{"observation":observation,"signature":identity.sign(&observation)?,"verification_key":identity.public_key_record()}}),
    )
}
