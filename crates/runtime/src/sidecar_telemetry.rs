// SPDX-License-Identifier: AGPL-3.0-only
//! v0.1.4 sidecar transport telemetry: per-peer observation state, the observation /
//! admission-counter documents and the stderr event lines. Source: `sidecar.rs:1436,
//! 1488-1530,2062-2108,2411-2528 @ f4e366e9`.
//!
//! Pure data: the QUIC driver (`iroh_pool`) feeds it. Documents are assembled in the
//! sidecar's struct field order with `serde_json` string/float formatting, so the
//! bytes equal the Rust sidecar's. `measured_at_unix_ms` is a wall-clock stamp the
//! caller supplies ([`unix_time_ms`]); the state machine itself never reads a clock.
//! Event lines print their keys sorted because the sidecar builds them with
//! `serde_json::json!` without `preserve_order`.
use std::{
    collections::VecDeque,
    time::{SystemTime, UNIX_EPOCH},
};

/// Observation history kept per peer (sidecar.rs:2456).
const RTT_WINDOW: usize = 64;

#[derive(Debug, Default)]
pub struct ObservationState {
    pub connections_opened: u64,
    pub frames_sent: u64,
    pub attempts: u64,
    pub failures: u64,
    pub reconnect_count: u64,
    pub selected_path_changes: u64,
    pub path_class: String,
    pub relay_identity: Option<String>,
    pub cold_rtt_ms: Option<f64>,
    pub rtt_samples_ms: VecDeque<f64>,
    pub acknowledged_bytes: u64,
    pub acknowledged_elapsed_ms: f64,
    pub measured_at_unix_ms: u64,
}
/// Wall-clock milliseconds since the Unix epoch (saturating; 0 before the epoch).
pub fn unix_time_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .ok()
        .and_then(|elapsed| u64::try_from(elapsed.as_millis()).ok())
        .unwrap_or_default()
}
impl ObservationState {
    /// `note_attempt` (sidecar.rs:2062).
    pub fn note_attempt(&mut self, now_ms: u64) {
        self.attempts = self.attempts.saturating_add(1);
        self.measured_at_unix_ms = now_ms;
    }
    /// `evict_connection`'s failure count (sidecar.rs:2056-2059).
    pub fn note_failure(&mut self, now_ms: u64) {
        self.failures = self.failures.saturating_add(1);
        self.measured_at_unix_ms = now_ms;
    }
    /// A connection newly inserted into the pool (sidecar.rs:2034-2046): every open
    /// after the first for this `(peer, generation)` is a reconnect.
    pub fn note_connection_opened(
        &mut self,
        now_ms: u64,
        class: String,
        relay: Option<String>,
        rtt_ms: Option<f64>,
    ) {
        if self.connections_opened > 0 {
            self.reconnect_count = self.reconnect_count.saturating_add(1);
        }
        self.connections_opened = self.connections_opened.saturating_add(1);
        self.measured_at_unix_ms = now_ms;
        update_selected_path(self, class, relay, rtt_ms);
    }
    /// An empty matching ACK (sidecar.rs:2070-2088).
    pub fn note_delivered(
        &mut self,
        now_ms: u64,
        payload_bytes: u64,
        elapsed_ms: f64,
        class: String,
        relay: Option<String>,
        rtt_ms: Option<f64>,
    ) {
        self.frames_sent = self.frames_sent.saturating_add(1);
        self.acknowledged_bytes = self.acknowledged_bytes.saturating_add(payload_bytes);
        self.acknowledged_elapsed_ms += elapsed_ms;
        self.measured_at_unix_ms = now_ms;
        update_selected_path(self, class, relay, rtt_ms);
    }
}
/// sidecar.rs:2442-2460. The change counter moves unless this is the first
/// observation and it is `unknown`; negative and non-finite RTTs are ignored; the
/// first accepted sample is the cold RTT; 64 samples are kept.
pub fn update_selected_path(
    state: &mut ObservationState,
    class: String,
    relay: Option<String>,
    rtt_ms: Option<f64>,
) {
    if state.path_class != class && (!state.path_class.is_empty() || class != "unknown") {
        state.selected_path_changes = state.selected_path_changes.saturating_add(1);
    }
    state.path_class = class;
    state.relay_identity = relay;
    if let Some(rtt) = rtt_ms.filter(|value| value.is_finite() && *value >= 0.0) {
        state.cold_rtt_ms.get_or_insert(rtt);
        state.rtt_samples_ms.push_back(rtt);
        while state.rtt_samples_ms.len() > RTT_WINDOW {
            state.rtt_samples_ms.pop_front();
        }
    }
}

fn string(text: &str) -> String {
    serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_owned())
}
fn float(value: f64) -> String {
    serde_json::to_string(&value).unwrap_or_else(|_| "null".to_owned())
}
fn optional(text: Option<&str>) -> String {
    text.map_or_else(|| "null".to_owned(), string)
}
/// One `mycelium.iroh_sidecar.transport_observation.v1` document
/// (sidecar.rs:2462-2518). `state == None` is a configured peer never used.
pub fn document_json(
    remote_endpoint_id: &str,
    generation: u64,
    state: Option<&ObservationState>,
) -> String {
    let empty = ObservationState::default();
    let state = state.unwrap_or(&empty);
    let samples = state.rtt_samples_ms.len();
    let warm = if samples == 0 {
        0.0
    } else {
        state.rtt_samples_ms.iter().sum::<f64>() / samples as f64
    };
    let jitter = if samples == 0 {
        0.0
    } else {
        let variance = state
            .rtt_samples_ms
            .iter()
            .map(|value| (value - warm).powi(2))
            .sum::<f64>()
            / samples as f64;
        variance.sqrt()
    };
    let goodput = if state.acknowledged_elapsed_ms <= 0.0 {
        0.0
    } else {
        state.acknowledged_bytes as f64 / (state.acknowledged_elapsed_ms / 1_000.0)
    };
    let loss = if state.attempts == 0 {
        0.0
    } else {
        state.failures as f64 / state.attempts as f64
    };
    let class = if state.path_class.is_empty() {
        "unknown"
    } else {
        state.path_class.as_str()
    };
    format!(
        "{{\"protocol\":\"mycelium.iroh_sidecar.transport_observation.v1\",\"remote_endpoint_id\":{},\"connection_generation\":{generation},\"path_class\":{},\"relay_identity\":{},\"relay_region\":null,\"cold_rtt_ms\":{},\"warm_rtt_ms\":{},\"observed_goodput_bps\":{},\"jitter_ms\":{},\"loss_ratio\":{},\"sample_count\":{samples},\"connections_opened\":{},\"frames_sent\":{},\"reconnect_count\":{},\"selected_path_changes\":{},\"measured_at_unix_ms\":{}}}",
        string(remote_endpoint_id),
        string(class),
        optional(state.relay_identity.as_deref()),
        float(state.cold_rtt_ms.unwrap_or(0.0)),
        float(warm),
        float(goodput),
        float(jitter),
        float(loss),
        state.connections_opened,
        state.frames_sent,
        state.reconnect_count,
        state.selected_path_changes,
        state.measured_at_unix_ms,
    )
}
/// `{"protocol":"…transport_observations.v1","observations":[…]}`; the caller sorts
/// documents by remote endpoint id like the sidecar (sidecar.rs:2090-2107).
pub fn envelope_json(documents: &[String]) -> String {
    format!(
        "{{\"protocol\":\"mycelium.iroh_sidecar.transport_observations.v1\",\"observations\":[{}]}}",
        documents.join(",")
    )
}
/// `mycelium.iroh_sidecar.inbound_admission.v1` (sidecar.rs:1479-1486,1820-1838).
pub fn admission_counters_json(
    inbound_identity_rejections: u64,
    inbound_frames_admitted: u64,
    candidate_identity_rejections: u64,
    measured_at_unix_ms: u64,
) -> String {
    format!(
        "{{\"protocol\":\"mycelium.iroh_sidecar.inbound_admission.v1\",\"inbound_identity_rejections\":{inbound_identity_rejections},\"inbound_frames_admitted\":{inbound_frames_admitted},\"candidate_identity_rejections\":{candidate_identity_rejections},\"measured_at_unix_ms\":{measured_at_unix_ms}}}"
    )
}

/// The two stderr events `log_transport_event` prints (sidecar.rs:2015,2043,2520).
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TransportEvent {
    ConnectionOpened,
    PeerConnectDeadlineExceeded,
}
impl TransportEvent {
    pub const fn name(self) -> &'static str {
        match self {
            Self::ConnectionOpened => "transport_connection_opened",
            Self::PeerConnectDeadlineExceeded => "peer_connect_deadline_exceeded",
        }
    }
    /// The exact line: keys sorted, compact.
    pub fn line(self, remote_endpoint_id: &str, generation: u64) -> String {
        format!(
            "{{\"connection_generation\":{generation},\"event\":\"{}\",\"remote_endpoint_id\":{}}}",
            self.name(),
            string(remote_endpoint_id)
        )
    }
}
