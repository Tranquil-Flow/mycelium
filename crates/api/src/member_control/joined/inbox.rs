// SPDX-License-Identifier: AGPL-3.0-only
//! Bounded latest rendezvous per member, on the already authenticated seed TLS origin.
//! Every take rechecks the signed registration against current seed identity and current run.
use super::*;
use crate::{run_exchange::RunExchange, run_exchange_client::ExchangeClient};
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};
pub struct Inbox {
    exchange: Arc<RunExchange>,
    documents: Mutex<BTreeMap<String, Value>>,
}
impl Inbox {
    pub fn new(exchange: Arc<RunExchange>) -> Self {
        Self {
            exchange,
            documents: Mutex::default(),
        }
    }
    pub fn get(
        &self,
        coordinator: &seed::Coordinator,
        node: &str,
        owner: iroh::EndpointId,
        run: &str,
        deployment: &str,
        allowed: &dyn Fn(SocketAddr) -> bool,
    ) -> Result<Verified> {
        let d = self
            .documents
            .lock()
            .map_err(|_| fail("member_work_inbox_poisoned"))?
            .get(node)
            .cloned()
            .ok_or(fail("member_work_unavailable"))?;
        let a = self.exchange.announcement(Some(node)).to_value();
        if a["run_id"] != run
            || a["deployment_id"] != deployment
            || a["owner_control"]["endpoint_id"] != owner.to_string()
        {
            return Err(fail("member_work_run_changed"));
        }
        verify(
            coordinator,
            &d,
            owner,
            run,
            deployment,
            allowed,
            crate::app_host::now(),
        )
    }
    fn receive(&self, coordinator: &seed::Coordinator, document: &Value) -> Result<Value> {
        let node = document["message"]["registration"]["message"]["member"]["node_id"]
            .as_str()
            .ok_or(fail("member_work_binding_invalid"))?;
        let a = self.exchange.announcement(Some(node)).to_value();
        let owner = a["owner_control"]["endpoint_id"]
            .as_str()
            .and_then(|s| s.parse().ok())
            .ok_or(fail("owner_pin_missing"))?;
        let run = a["run_id"]
            .as_str()
            .ok_or(fail("member_work_run_changed"))?;
        let deployment = a["deployment_id"]
            .as_str()
            .ok_or(fail("member_work_run_changed"))?;
        // Storage admission only. The owner applies its actual dial policy on get, before dialing.
        let verified = verify(
            coordinator,
            document,
            owner,
            run,
            deployment,
            &|_| true,
            crate::app_host::now(),
        )?;
        let mut documents = self
            .documents
            .lock()
            .map_err(|_| fail("member_work_inbox_poisoned"))?;
        let now = crate::app_host::now();
        documents.retain(|_, d| d["message"]["expires_at"].as_f64().is_some_and(|e| e > now));
        if !documents.contains_key(node) && documents.len() >= contract::MAX_MEMBERS {
            return Err(fail("member_work_inbox_full"));
        }
        if documents.get(node).is_some_and(|d| {
            d["message"]["issued_at"].as_f64() > document["message"]["issued_at"].as_f64()
        }) {
            return Err(fail("member_work_binding_replayed"));
        }
        // Guard the actual storage write against generation/key/incarnation changes after verify.
        coordinator
            .with_current_member(node, |row, _| {
                let member = &verified.registration()["message"]["member"];
                if [
                    "node_id",
                    "endpoint_id",
                    "verification_key_digest",
                    "generation",
                    "incarnation",
                ]
                .iter()
                .any(|f| row[*f] != member[*f])
                {
                    return Err(seed::SeedError::new("joined_shard_member_changed"));
                }
                documents.insert(node.to_owned(), document.clone());
                Ok(())
            })
            .map_err(|_| fail("joined_shard_member_changed"))?;
        envelope(
            coordinator.signer(),
            json!({"protocol":contract::ACK,
            "document_digest":protocol::canonical::digest(document).map_err(|_| fail("member_work_binding_invalid"))?}),
        )
    }
}
impl seed::http::RouteExtension for Inbox {
    fn post(
        &self,
        coordinator: &seed::Coordinator,
        path: &str,
        document: &Value,
    ) -> Option<std::result::Result<Value, seed::SeedError>> {
        (path == contract::PATH).then(|| {
            self.receive(coordinator, document)
                .map_err(|e| seed::SeedError::new(e.code))
        })
    }
}
/// Member verifies the pinned seed's acknowledgement of these exact signed bytes.
pub fn publish(client: &ExchangeClient, document: &Value) -> Result<()> {
    let response = client.post(contract::PATH, document).map_err(|e| {
        eprintln!("member_work_publish_failed:{}", e.0);
        fail("member_work_publish_failed")
    })?;
    let ack = open(&response)?;
    exact(ack, &["protocol", "document_digest"])?;
    if response["verification_key"] != client.member().owner_key
        || ack["protocol"] != contract::ACK
        || ack["document_digest"]
            != protocol::canonical::digest(document)
                .map_err(|_| fail("member_work_binding_invalid"))?
    {
        return Err(fail("member_work_ack_invalid"));
    }
    Ok(())
}
