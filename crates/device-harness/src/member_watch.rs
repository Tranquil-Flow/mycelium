// SPDX-License-Identifier: AGPL-3.0-only
//! Member liveness observation; intentionally no member action/repair capability.
use crate::{
    Host, Result,
    keeper::Alert,
    observe::{Mailbox, Metrics, Observer},
};
use budgets::Span;
use serde_json::Value;
use std::collections::BTreeSet;
pub trait Alerts {
    fn alert(&mut self, value: Alert) -> Result<()>;
}
pub fn inspect(status: &Value, expected: &BTreeSet<String>, silence: Span) -> Result<Vec<Alert>> {
    if silence == Span::ZERO
        || expected.is_empty()
        || expected.iter().any(|s| {
            s.is_empty()
                || !s
                    .bytes()
                    .all(|b| b.is_ascii_alphanumeric() || b"_-".contains(&b))
        })
    {
        return Err(crate::Fault::new(
            "member_watch_configuration_invalid",
            crate::FaultKind::Input,
        ));
    }
    let mut alerts = vec![];
    for peer in expected {
        let rows: Vec<_> = status["authority"]["members"]
            .as_array()
            .into_iter()
            .flatten()
            .filter(|row| row["node_id"] == peer.as_str())
            .collect();
        if rows.len() > 1 {
            return Err(crate::Fault::new(
                "member_clock_duplicate",
                crate::FaultKind::Observation,
            ));
        }
        let age = rows
            .first()
            .and_then(|r| r["clock"]["age_seconds"].as_f64())
            .and_then(|seconds| std::time::Duration::try_from_secs_f64(seconds).ok());
        let code = match age {
            Some(age) if age > std::time::Duration::from_millis(silence.millis()) => {
                Some("member_silent")
            }
            None => Some("member_clock_unobserved"),
            Some(_) => None,
        };
        if let Some(code) = code {
            alerts.push(Alert {
                code,
                peer: Some(peer.clone()),
            });
        }
    }
    Ok(alerts)
}
pub struct Watching<O, A> {
    pub inner: O,
    pub alerts: A,
    pub expected: BTreeSet<String>,
    pub silence: Span,
}
impl<O: Observer, A: Alerts> Observer for Watching<O, A> {
    fn status(&mut self, host: Host) -> Result<(u16, Value)> {
        let result = self.inner.status(host);
        if host == Host::Owner {
            let value = match &result {
                Ok((200, value)) => value,
                _ => &Value::Null,
            };
            for alert in inspect(value, &self.expected, self.silence)? {
                self.alerts.alert(alert)?;
            }
        }
        result
    }
    fn metrics(&mut self, host: Host) -> Result<Metrics> {
        self.inner.metrics(host)
    }
    fn mailbox(&mut self, host: Host) -> Result<Mailbox> {
        self.inner.mailbox(host)
    }
}
