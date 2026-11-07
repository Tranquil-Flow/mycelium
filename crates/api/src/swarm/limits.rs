// SPDX-License-Identifier: AGPL-3.0-only
//! Limits (directory.py:36-57) from the L1 registry, and the sliding-window
//! limiter (ratelimit.py:9-38 @ f4e366e9). Memory only; a restart forgets it.
use budgets::{BudgetId, Entries};
use std::collections::{HashMap, VecDeque};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct Limits {
    pub max_body: usize,
    pub max_swarms: usize,
    pub max_swarms_per_owner: usize,
    pub max_requests_per_swarm: usize,
    pub max_offers_per_swarm: usize,
    pub max_grants_per_grantee: usize,
    pub max_live_nonces: usize,
    pub heartbeat_ttl: i64,
    pub skew: i64,
    pub offer_ttl: i64,
    pub writes_per_ip_per_minute: usize,
    pub writes_per_key_per_minute: usize,
    pub reads_per_ip_per_minute: usize,
    pub queries_per_ip_per_minute: usize,
    pub queries_per_key_per_minute: usize,
    pub writes_per_minute: usize,
    pub heartbeats_per_minute: usize,
}
fn count(id: BudgetId) -> Result<usize, &'static str> {
    usize::try_from(id.count().map_err(|_| "budget_unavailable")?.get())
        .map_err(|_| "budget_unavailable")
}
fn seconds(id: BudgetId) -> Result<i64, &'static str> {
    i64::try_from(
        id.span(Entries::ZERO)
            .map_err(|_| "budget_unavailable")?
            .millis()
            / 1000,
    )
    .map_err(|_| "budget_unavailable")
}
impl Limits {
    /// Registry-sourced defaults; the table is the only home of these values.
    pub fn source() -> Result<Self, &'static str> {
        use BudgetId as B;
        Ok(Self {
            max_body: usize::try_from(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxBodyL38
                    .bytes()
                    .map_err(|_| "budget_unavailable")?
                    .get(),
            )
            .map_err(|_| "budget_unavailable")?,
            max_swarms: count(B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxSwarmsL39)?,
            max_swarms_per_owner: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxSwarmsPerOwnerL40,
            )?,
            max_requests_per_swarm: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxRequestsPerSwarmL41,
            )?,
            max_offers_per_swarm: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxOffersPerSwarmL42,
            )?,
            max_grants_per_grantee: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxGrantsPerGranteeL43,
            )?,
            max_live_nonces: count(B::PyMyceliumSwarmDirectoryDirectoryLimitsMaxLiveNoncesL44)?,
            heartbeat_ttl: seconds(B::PyMyceliumSwarmDirectoryDirectoryLimitsHeartbeatTtlL45)?,
            skew: seconds(B::PyMyceliumSwarmDirectoryDirectoryLimitsSkewL46)?,
            offer_ttl: seconds(B::PyMyceliumSwarmDirectoryDirectoryLimitsOfferTtlL47)?,
            writes_per_ip_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsWritesPerIpPerMinuteL48,
            )?,
            writes_per_key_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsWritesPerKeyPerMinuteL49,
            )?,
            reads_per_ip_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsReadsPerIpPerMinuteL50,
            )?,
            queries_per_ip_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsQueriesPerIpPerMinuteL52,
            )?,
            queries_per_key_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsQueriesPerKeyPerMinuteL53,
            )?,
            writes_per_minute: count(B::PyMyceliumSwarmDirectoryDirectoryLimitsWritesPerMinuteL55)?,
            heartbeats_per_minute: count(
                B::PyMyceliumSwarmDirectoryDirectoryLimitsHeartbeatsPerMinuteL57,
            )?,
        })
    }
}
pub struct WindowLimiter {
    limit: usize,
    window: i64,
    max_keys: usize,
    hits: HashMap<String, VecDeque<i64>>,
}
impl WindowLimiter {
    pub fn new(limit: usize, max_keys: usize) -> Result<Self, &'static str> {
        Ok(Self {
            limit,
            window: seconds(BudgetId::PyDirectoryRateWindow)?,
            max_keys,
            hits: HashMap::new(),
        })
    }
    pub fn allow(&mut self, key: &str, now: i64) -> bool {
        if !self.hits.contains_key(key) {
            if self.hits.len() >= self.max_keys {
                self.prune(now);
                if self.hits.len() >= self.max_keys {
                    return false; // fail closed under a flood of fresh keys
                }
            }
            self.hits.insert(key.to_string(), VecDeque::new());
        }
        let window = self.window;
        let bucket = self.hits.entry(key.to_string()).or_default();
        while bucket.front().is_some_and(|t| now - t >= window) {
            bucket.pop_front();
        }
        if bucket.len() >= self.limit {
            return false;
        }
        bucket.push_back(now);
        true
    }
    fn prune(&mut self, now: i64) {
        let window = self.window;
        self.hits
            .retain(|_, b| b.back().is_some_and(|t| now - t < window));
    }
}
