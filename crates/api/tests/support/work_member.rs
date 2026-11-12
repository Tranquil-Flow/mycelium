// SPDX-License-Identifier: AGPL-3.0-only
//! Model-free, real enrolled Session and real signed announcement exchange.
#![allow(dead_code)]
use api::{app_host, handoff, peer, member::Session, preferences::{resource_policy, standing_consent},
    run_exchange::{Announce, owner_pin::{OwnerControl, VerifiedAnnouncement}},
    run_exchange_client::{ExchangeClient, Membership}, seed::SeedClient};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{path::PathBuf, sync::Arc};
pub struct Joined {
    pub root: PathBuf,
    pub member_root: PathBuf,
    pub seed: app_host::SeedRuntime,
    pub signer: Arc<Identity>,
    pub session: Session,
    pub owner: iroh::SecretKey,
    pub policy: Value,
}
impl Joined {
    pub fn new() -> Self {
        let root = std::env::temp_dir().join(format!("s4cb2-{}", app_host::hex(&app_host::random(16).unwrap())));
        std::fs::create_dir(&root).unwrap();
        let root = root.canonicalize().unwrap();
        let owner_root = root.join("owner");
        let member_root = root.join("member");
        std::fs::create_dir(&owner_root).unwrap();
        std::fs::create_dir(&member_root).unwrap();
        let seed = app_host::start_seed::<app_host::Unmanaged>(&owner_root, "s4cb2-swarm", None).unwrap();
        let request = app_host::build_pair_request(&member_root, &member_root).unwrap();
        let (signer, _, _) = app_host::identity(&member_root).unwrap();
        let (seed_signer, _, _) = app_host::identity_in(&owner_root, app_host::SEED_IDENTITY_FILE).unwrap();
        let coordinator = handoff::Coordinator { signer: seed_signer, swarm_id: "s4cb2-swarm".into(), seed_url: seed.seed_url.clone() };
        let ttl = budgets::BudgetId::PyFinalMyceliumNodeMembershipMessageTtlSecondsL137C37.span(budgets::Entries::ZERO).unwrap().millis()/1000;
        let offered = handoff::approve_pairing(&owner_root, true, Some(&coordinator), &request.to_value(), &json!(ttl),
            &json!({"storage_bytes":4_000_000_000u64,"network_bytes_per_day":4_000_000_000u64,"background_allowed":false}), app_host::now(), &handoff::Entropy::os).unwrap();
        let options = seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv { signer: &signer, clock: &app_host::now, random_hex: &random, seed: &options };
        let mut accepted = peer::accept_handoff(&member_root, true, &offered.to_value(), &env).unwrap();
        peer::renew(&member_root, true, accepted.session.as_mut(), &env).unwrap();
        let session = accepted.session.unwrap();
        let policy = json!({"peer_visibility":true,"power":"ac_only","storage_bytes":4_000_000_000u64,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"});
        resource_policy::write(&member_root, &policy).unwrap();
        standing_consent::grant(&member_root, &json!({"acknowledged":true,"disclosure_digest":standing_consent::DISCLOSURE_DIGEST,"caps":{"storage_bytes":4_000_000_000u64,"max_memory_bytes":4_000_000_000u64,"max_wall_seconds":600,"max_commands":1000}}), &policy, app_host::now(), Some(16_000_000_000)).unwrap();
        let b = session.binding().unwrap();
        standing_consent::bind(&member_root, &json!({"swarm_id":b.swarm_id,"node_id":b.node_id,"seed_key_digest":b.seed_key_digest}), app_host::now()).unwrap();
        Self { root, member_root, seed, signer: Arc::new(signer), session, owner: iroh::SecretKey::generate(), policy }
    }
    pub fn announced(&self) -> VerifiedAnnouncement {
        self.seed.exchange.set_owner_control(Some(OwnerControl::new(self.owner.public())));
        self.seed.exchange.announce(Announce { phase: "compile".into(), run_id: Some("run-s4cb2".into()), deployment_id: Some("dep-s4cb2".into()), ..Default::default() }).unwrap();
        self.read()
    }
    pub fn read(&self) -> VerifiedAnnouncement {
        let row = self.session.shard_identity(&self.signer, app_host::now()).unwrap();
        let key = self.seed.coordinator.signer().public_key_record();
        let client = SeedClient::new(&self.seed.seed_url, self.seed.coordinator.swarm_id(),
            key["verification_key_digest"].as_str().unwrap(), std::slice::from_ref(&key), &self.seed.options()).unwrap();
        let mut client = ExchangeClient::new(client, self.signer.clone(), Membership {
            swarm_id: self.seed.coordinator.swarm_id().into(), owner_node_id: self.seed.coordinator.seed_node_id().into(), owner_key: key,
            node_id: self.session.node_id().into(), generation: self.session.generation().unwrap(), incarnation: row["incarnation"].as_str().unwrap().into(),
        }, Box::new(app_host::now), Box::new(|| Some(app_host::hex(&app_host::random(16).unwrap()))));
        VerifiedAnnouncement::read(&mut client).unwrap()
    }
    pub fn resume(&mut self) {
        let options = self.seed.options();
        let random = |n| app_host::hex(&app_host::random(n).unwrap());
        let env = peer::JoinEnv { signer: &self.signer, clock: &app_host::now, random_hex: &random, seed: &options };
        self.session = peer::start(&self.member_root, &self.signer, true, &env).unwrap().session.unwrap();
        peer::renew(&self.member_root, true, Some(&mut self.session), &env).unwrap();
    }
}
impl Drop for Joined {
    fn drop(&mut self) {
        self.seed.close();
        if let Err(e) = std::fs::remove_dir_all(&self.root) { eprintln!("work_fixture_cleanup_failed:{e}"); }
    }
}
