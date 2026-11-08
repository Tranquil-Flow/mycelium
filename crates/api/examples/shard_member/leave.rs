// SPDX-License-Identifier: AGPL-3.0-only
//! Member leave through the product `api::leave::MemberLeave` state machine:
//! fence, drain, unload (native close/reap), publish a member-signed withdrawal,
//! withdraw standing consent, disconnect (stop renewal), retire+refund custody.
use super::membership::Membership;
use api::{
    joined_shard::{self, JoinedShard, native::NativeShard},
    leave::{ComputeCleanup, Steps},
    preferences::standing_consent,
};
use protocol::identity::Identity;
use serde_json::{Value, json};
use std::{path::PathBuf, time::Instant};
pub struct Leave<'a> {
    pub root: PathBuf,
    pub signer: &'a Identity,
    pub native: &'a mut Option<NativeShard>,
    pub shard: &'a mut Option<JoinedShard>,
    pub session: &'a mut Option<Membership>,
    pub fenced: &'a mut bool,
    pub began: Instant,
    pub withdrawal: Option<Value>,
    pub unload: Value,
    pub timings: Vec<Value>,
    pub refunded: bool,
}
impl Leave<'_> {
    fn run(&mut self, name: &str) -> Result<(), String> {
        match name {
            "fence" => *self.fenced = true,
            "unload" => {
                if let Some(n) = self.native.as_mut()
                    && n.worker_pid().is_some()
                {
                    let pid = n.worker_pid();
                    self.unload = match n.close() {
                        Ok(stats) => {
                            json!({"worker_pid":pid,"graceful":true,"context_closed":stats["context_closed"]})
                        }
                        Err(_) => {
                            json!({"worker_pid":pid,"graceful":false,"reap":n.abort().map_err(|e| e.0.to_owned())?})
                        }
                    };
                }
            }
            "publish_withdrawal" => {
                let offer = match (self.native.as_ref(), self.shard.as_ref()) {
                    (Some(n), _) => n.offer().clone(),
                    _ => return Ok(()),
                };
                let member = self.session.as_ref().ok_or("member_session_missing")?;
                let held = member.session.lock().map_err(|_| "session_poisoned")?;
                self.withdrawal = Some(
                    joined_shard::withdrawal(
                        held.as_ref().ok_or("member_session_missing")?,
                        self.signer,
                        &offer,
                        &self.unload,
                        api::app_host::now(),
                    )
                    .map_err(|e| e.0.to_owned())?,
                );
            }
            "withdraw_consent" => {
                standing_consent::withdraw(&self.root, api::app_host::now())
                    .map_err(|e| e.0.to_owned())?;
            }
            // Dropping Membership stops AND joins the renewal thread: the lease ages out.
            "disconnect" => *self.session = None,
            "cancel_work" => {
                if let Some(n) = self.native.as_mut() {
                    n.discard().map_err(|e| e.0.to_owned())?;
                    self.refunded = true;
                }
                *self.native = None;
                if let Some(s) = self.shard.as_mut() {
                    s.discard().map_err(|e| e.0.to_owned())?;
                    self.refunded = true;
                }
                *self.shard = None;
            }
            _ => {}
        }
        Ok(())
    }
}
impl Steps for Leave<'_> {
    fn step(&mut self, name: &str) -> Result<(), Option<String>> {
        let result = self.run(name);
        self.timings.push(json!({"step":name,"ok":result.is_ok(),
            "at_ms":self.began.elapsed().as_secs_f64()*1000.0}));
        result.map_err(Some)
    }
    fn inflight(&mut self) -> Result<i64, api::leave::Unobservable> {
        // The operator channel is serial: no route epoch can be serving now.
        Ok(0)
    }
    fn compute_cleanup(&mut self) -> Option<Result<ComputeCleanup, String>> {
        Some(
            if self.native.is_none() && self.shard.is_none() && self.refunded {
                Ok(ComputeCleanup::Proven)
            } else if self.native.is_none() && self.shard.is_none() {
                Ok(ComputeCleanup::NoneOwned)
            } else {
                Err("member_compute_cleanup_unavailable".into())
            },
        )
    }
}
