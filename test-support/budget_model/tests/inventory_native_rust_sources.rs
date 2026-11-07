// SPDX-License-Identifier: AGPL-3.0-only
//! SLICE-1 round-4 registry requests: NodeControl concurrency and artifact carrier limits.
//! These are Rust constants in the pinned v0.1.4 native crates (outside the Python-only L0
//! census), so provenance is `path:line @ f4e366e9 sha256:<file>; <symbol>`. Each row pins
//! the value, the quantity class, the purpose and the exact oracle source line text.
use budgets::BudgetId;
use budgets::table::{Phase, Provenance, Purpose, Value};
use std::collections::BTreeSet;

const SIDECAR: &str = "native/iroh_transport/src/sidecar.rs";
const SIDECAR_SHA: &str = "b3c4b75a445562f93956b0bff30d12c6ae4f07d9d46510a37a2b6a5686de7d54";
const ARTIFACT: &str = "native/mobile_core/src/artifact_net.rs";
const LOCAL: &str = "native/iroh_transport/src/local.rs";
const LOCAL_SHA: &str = "cd1999ac7f3000d1328a5447e437aa6f4c902bf2f1d6f107ecbd692dbce2ba0a";
const ARTIFACT_SHA: &str = "2c9330c04a18b01ab5022455092d6a5582a3885bcddb5fc92e2736448ca12d12";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Q {
    Ms(u64),
    Bytes(u64),
    Count(u64),
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum P {
    Policy,
    Resource,
    Op(Phase),
}

struct Row {
    id: &'static str,
    quantity: Q,
    purpose: P,
    file: &'static str,
    sha: &'static str,
    line: u32,
    /// Verbatim v0.1.4 line text (trimmed) at `file:line`.
    text: &'static str,
    symbol: &'static str,
    /// Substring of `text` naming the constant/expression the value was read from.
    needle: &'static str,
}

const ROWS: [Row; 12] = [
    Row {
        id: "NodeControlCallConcurrency",
        quantity: Q::Count(4),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 100,
        text: "pub const CONTROL_CALL_CONCURRENCY: usize = 4;",
        symbol: "CONTROL_CALL_CONCURRENCY; semaphore at :1720",
        needle: "CONTROL_CALL_CONCURRENCY",
    },
    Row {
        id: "NodeControlCallTimeoutCeiling",
        quantity: Q::Ms(60_000),
        purpose: P::Policy,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 96,
        text: "pub const CONTROL_CALL_TIMEOUT_CEILING: Duration = Duration::from_secs(60);",
        symbol: "CONTROL_CALL_TIMEOUT_CEILING",
        needle: "CONTROL_CALL_TIMEOUT_CEILING",
    },
    Row {
        id: "ArtifactMaxRequestBytes",
        quantity: Q::Bytes(262_144),
        purpose: P::Resource,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 59,
        text: "const MAX_REQUEST_BYTES: usize = 256 * 1024;",
        symbol: "MAX_REQUEST_BYTES",
        needle: "MAX_REQUEST_BYTES",
    },
    Row {
        id: "ArtifactMaxHeaderBytes",
        quantity: Q::Bytes(65_536),
        purpose: P::Resource,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 60,
        text: "const MAX_HEADER_BYTES: usize = 64 * 1024;",
        symbol: "MAX_HEADER_BYTES",
        needle: "MAX_HEADER_BYTES",
    },
    Row {
        id: "ArtifactIoTimeout",
        quantity: Q::Ms(30_000),
        purpose: P::Op(Phase::Unclassified),
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 61,
        text: "const IO_TIMEOUT: Duration = Duration::from_secs(30);",
        symbol: "IO_TIMEOUT",
        needle: "IO_TIMEOUT",
    },
    Row {
        id: "ArtifactMaxConnections",
        quantity: Q::Count(16),
        purpose: P::Resource,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 68,
        text: "pub const MAX_ARTIFACT_CONNECTIONS: usize = 16;",
        symbol: "MAX_ARTIFACT_CONNECTIONS",
        needle: "MAX_ARTIFACT_CONNECTIONS",
    },
    Row {
        id: "ArtifactMaxConnectionsPerPeer",
        quantity: Q::Count(2),
        purpose: P::Resource,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 69,
        text: "pub const MAX_ARTIFACT_CONNECTIONS_PER_PEER: usize = 2;",
        symbol: "MAX_ARTIFACT_CONNECTIONS_PER_PEER",
        needle: "MAX_ARTIFACT_CONNECTIONS_PER_PEER",
    },
    Row {
        id: "ArtifactMaxBlockingWork",
        quantity: Q::Count(4),
        purpose: P::Resource,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 70,
        text: "const MAX_BLOCKING_WORK: usize = 4;",
        symbol: "MAX_BLOCKING_WORK",
        needle: "MAX_BLOCKING_WORK",
    },
    Row {
        id: "ArtifactMaxPace",
        quantity: Q::Ms(10_000),
        purpose: P::Policy,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 71,
        text: "const MAX_PACE: Duration = Duration::from_secs(10);",
        symbol: "MAX_PACE",
        needle: "MAX_PACE",
    },
    Row {
        id: "ArtifactReadTimeout",
        quantity: Q::Ms(10_000),
        purpose: P::Policy,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 87,
        text: "read_timeout: Duration::from_secs(10),",
        symbol: "ArtifactLimits::default().read_timeout",
        needle: "read_timeout",
    },
    Row {
        id: "ArtifactIdleTimeout",
        quantity: Q::Ms(20_000),
        purpose: P::Policy,
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 88,
        text: "idle_timeout: Duration::from_secs(20),",
        symbol: "ArtifactLimits::default().idle_timeout",
        needle: "idle_timeout",
    },
    Row {
        id: "ArtifactConnectTimeout",
        quantity: Q::Ms(30_000),
        purpose: P::Op(Phase::Unclassified),
        file: ARTIFACT,
        sha: ARTIFACT_SHA,
        line: 92,
        text: "const CONNECT_TIMEOUT: Duration = Duration::from_secs(30);",
        symbol: "CONNECT_TIMEOUT",
        needle: "CONNECT_TIMEOUT",
    },
];

const SIDECAR_ROWS: [Row; 14] = [
    Row {
        id: "SidecarStreamIo",
        quantity: Q::Ms(60000),
        purpose: P::Op(Phase::Control),
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 54,
        text: "const STREAM_IO_TIMEOUT: Duration = Duration::from_secs(60);",
        symbol: "STREAM_IO_TIMEOUT",
        needle: "STREAM_IO_TIMEOUT",
    },
    Row {
        id: "SidecarPeerConnect",
        quantity: Q::Ms(5000),
        purpose: P::Op(Phase::Start),
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 66,
        text: "const PEER_CONNECT_TIMEOUT: Duration = Duration::from_secs(5);",
        symbol: "PEER_CONNECT_TIMEOUT",
        needle: "PEER_CONNECT_TIMEOUT",
    },
    Row {
        id: "SidecarLocalHandshake",
        quantity: Q::Ms(2000),
        purpose: P::Op(Phase::Control),
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 67,
        text: "const LOCAL_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(2);",
        symbol: "LOCAL_HANDSHAKE_TIMEOUT",
        needle: "LOCAL_HANDSHAKE_TIMEOUT",
    },
    Row {
        id: "SidecarRetryInitial",
        quantity: Q::Ms(100),
        purpose: P::Policy,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 68,
        text: "const INITIAL_RETRY_DELAY: Duration = Duration::from_millis(100);",
        symbol: "INITIAL_RETRY_DELAY",
        needle: "INITIAL_RETRY_DELAY",
    },
    Row {
        id: "SidecarRetryMax",
        quantity: Q::Ms(5000),
        purpose: P::Policy,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 69,
        text: "const MAX_RETRY_DELAY: Duration = Duration::from_secs(5);",
        symbol: "MAX_RETRY_DELAY",
        needle: "MAX_RETRY_DELAY",
    },
    Row {
        id: "SidecarOutboundLifetime",
        quantity: Q::Ms(300000),
        purpose: P::Policy,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 87,
        text: "pub const OUTBOUND_DELIVERY_DEADLINE: Duration = Duration::from_secs(300);",
        symbol: "OUTBOUND_DELIVERY_DEADLINE; doc :70-86 derives it as 240 s Python delivery budget + 60 s margin, half the 600 s relay tombstone horizon",
        needle: "OUTBOUND_DELIVERY_DEADLINE",
    },
    Row {
        id: "SidecarQueueCapacity",
        quantity: Q::Count(128),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 51,
        text: "pub const DEFAULT_QUEUE_CAPACITY: usize = 128;",
        symbol: "DEFAULT_QUEUE_CAPACITY",
        needle: "DEFAULT_QUEUE_CAPACITY",
    },
    Row {
        id: "SidecarSeenFactor",
        quantity: Q::Count(8),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 1715,
        text: "let seen_limit = capacity.saturating_mul(8).max(64);",
        symbol: "seen_limit multiplier in capacity.saturating_mul(8).max(64)",
        needle: "saturating_mul(8)",
    },
    Row {
        id: "SidecarSeenFloor",
        quantity: Q::Count(64),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 1715,
        text: "let seen_limit = capacity.saturating_mul(8).max(64);",
        symbol: "seen_limit floor in capacity.saturating_mul(8).max(64)",
        needle: "max(64)",
    },
    Row {
        id: "SidecarReceivePollWait",
        quantity: Q::Ms(250),
        purpose: P::Op(Phase::Control),
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 52,
        text: "pub const RECEIVE_POLL_WAIT: Duration = Duration::from_millis(250);",
        symbol: "RECEIVE_POLL_WAIT",
        needle: "RECEIVE_POLL_WAIT",
    },
    Row {
        id: "SidecarLocalSessions",
        quantity: Q::Count(16),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 106,
        text: "const MAX_LOCAL_SESSIONS: usize = 16;",
        symbol: "MAX_LOCAL_SESSIONS",
        needle: "MAX_LOCAL_SESSIONS",
    },
    Row {
        id: "SidecarControlCallMaxBytes",
        quantity: Q::Bytes(4194304),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 95,
        text: "pub const CONTROL_CALL_MAX_BYTES: usize = 4 * 1024 * 1024;",
        symbol: "CONTROL_CALL_MAX_BYTES, mirrors native/mobile_core/src/node_control.rs:58 MAX_COMMAND_BYTES",
        needle: "CONTROL_CALL_MAX_BYTES",
    },
    Row {
        id: "SidecarHelloMaxBytes",
        quantity: Q::Bytes(4096),
        purpose: P::Resource,
        file: LOCAL,
        sha: LOCAL_SHA,
        line: 27,
        text: "pub const MAX_HELLO_BYTES: usize = 4 * 1024;",
        symbol: "MAX_HELLO_BYTES",
        needle: "MAX_HELLO_BYTES",
    },
    Row {
        id: "SidecarOperationalFrameMax",
        quantity: Q::Bytes(16777216),
        purpose: P::Resource,
        file: SIDECAR,
        sha: SIDECAR_SHA,
        line: 50,
        text: "pub const OPERATIONAL_MAX_FRAME_BYTES: usize = 16 * 1024 * 1024;",
        symbol: "OPERATIONAL_MAX_FRAME_BYTES, equals native/iroh_transport/src/remote.rs:9 REMOTE_OPERATIONAL_FRAME_BYTES (wire constant, protocol::remote owns the wire form)",
        needle: "OPERATIONAL_MAX_FRAME_BYTES",
    },
];

fn quantity(value: Value) -> Q {
    match value {
        Value::Duration(span) => Q::Ms(span.millis()),
        Value::Storage(bytes) => Q::Bytes(bytes.get()),
        Value::Count(entries) => Q::Count(entries.get()),
        other => panic!("unexpected value class {other:?}"),
    }
}

fn purpose(purpose: Purpose) -> P {
    match purpose {
        Purpose::Policy => P::Policy,
        Purpose::Resource => P::Resource,
        // Inventory-only phase: L2 has not mapped a qualified phase for these carrier waits.
        Purpose::Operational(phase) => P::Op(phase),
        other => panic!("unexpected purpose {other:?}"),
    }
}

#[test]
fn native_rust_ids_pin_value_class_purpose_and_exact_oracle_source() {
    for row in ROWS.iter().chain(SIDECAR_ROWS.iter()) {
        let entry = BudgetId::from_name(row.id)
            .unwrap_or_else(|| panic!("{} is not registered", row.id))
            .entry();
        assert_eq!(quantity(entry.value), row.quantity, "{}", row.id);
        assert_eq!(purpose(entry.purpose), row.purpose, "{}", row.id);
        let Provenance::Source(source) = entry.provenance else {
            panic!("{}: source constant masquerading as measurement", row.id)
        };
        assert_eq!(
            source,
            format!(
                "{}:{} @ f4e366e9 sha256:{}; {}",
                row.file, row.line, row.sha, row.symbol
            ),
            "{}",
            row.id
        );
        // The pinned source text is the declaration the value was read from.
        assert!(row.text.contains(row.needle), "{}", row.id);
    }
}

#[test]
fn native_rust_ids_are_exactly_the_registered_26_and_not_python_site_ids() {
    let names: BTreeSet<_> = ROWS.iter().map(|row| row.id).collect();
    assert_eq!(names.len(), 12);
    let sidecar: BTreeSet<_> = SIDECAR_ROWS.iter().map(|row| row.id).collect();
    assert_eq!(sidecar.len(), 14);
    assert!(names.is_disjoint(&sidecar));
    let names: BTreeSet<_> = names.union(&sidecar).copied().collect();
    for name in &names {
        assert!(
            !name.starts_with("Py"),
            "{name} must not masquerade as a Py site id"
        );
    }
    // They are listed in the non-Py legacy id file exactly once each.
    let listed: Vec<_> = include_str!("../data/legacy-budget-ids.txt")
        .lines()
        .filter(|line| names.contains(line))
        .collect();
    assert_eq!(listed.len(), 26);
    assert_eq!(listed.iter().copied().collect::<BTreeSet<_>>(), names);
}

#[test]
fn source_specific_artifact_waits_are_not_the_worker_startup_budget() {
    // INTEGRATOR RULING for SLICE-1/L9: read 10 s, idle 20 s, io 30 s as in v0.1.4 use the
    // source-specific ids; `WorkerStartup` (also 30 s) is another quantity with other provenance.
    let startup = BudgetId::WorkerStartup.entry();
    let io = BudgetId::ArtifactIoTimeout.entry();
    assert_eq!(quantity(startup.value), quantity(io.value));
    assert_ne!(startup.provenance, io.provenance);
    assert_ne!(startup.id, io.id);
    let read = quantity(BudgetId::ArtifactReadTimeout.entry().value);
    let idle = quantity(BudgetId::ArtifactIdleTimeout.entry().value);
    assert_eq!((read, idle), (Q::Ms(10_000), Q::Ms(20_000)));
    assert_ne!(read, quantity(startup.value));
    assert_ne!(idle, quantity(startup.value));
    // The carrier's own read deadline is shorter than its idle deadline and io timeout.
    assert!(
        matches!((read, idle, quantity(io.value)), (Q::Ms(r), Q::Ms(i), Q::Ms(o)) if r < i && i < o)
    );
}

#[test]
fn artifact_connection_and_blocking_limits_keep_their_own_ids() {
    // Same-value ids with different sources must not be interchangeable: per-peer vs the
    // (also 2 entries) staged copy factor; blocking work vs control concurrency (both 4).
    let per_peer = BudgetId::ArtifactMaxConnectionsPerPeer.entry();
    let copy = BudgetId::StagedCopyFactor.entry();
    assert_eq!(quantity(per_peer.value), quantity(copy.value));
    assert_ne!(per_peer.provenance, copy.provenance);
    let blocking = BudgetId::ArtifactMaxBlockingWork.entry();
    let control = BudgetId::NodeControlCallConcurrency.entry();
    assert_eq!(quantity(blocking.value), quantity(control.value));
    assert_ne!(blocking.provenance, control.provenance);
    assert_eq!(
        quantity(BudgetId::ArtifactMaxHeaderBytes.entry().value),
        Q::Bytes(64 * 1024)
    );
    assert_eq!(
        quantity(BudgetId::ArtifactMaxRequestBytes.entry().value),
        Q::Bytes(256 * 1024)
    );
}

#[test]
fn sidecar_control_call_bounds_alias_the_node_control_ids_not_duplicates() {
    // L9 packet r8 lists SidecarControlCallTimeoutCeiling (:96) and SidecarControlCallConcurrency (:100);
    // they are the same source lines as the SLICE-1 NodeControl ids, so no second id exists.
    for name in [
        "SidecarControlCallTimeoutCeiling",
        "SidecarControlCallConcurrency",
    ] {
        assert!(
            BudgetId::from_name(name).is_none(),
            "{name} must alias, not duplicate"
        );
    }
    let ceiling = BudgetId::NodeControlCallTimeoutCeiling.entry();
    let concurrency = BudgetId::NodeControlCallConcurrency.entry();
    assert_eq!(quantity(ceiling.value), Q::Ms(60_000));
    assert_eq!(quantity(concurrency.value), Q::Count(4));
    let (Provenance::Source(a), Provenance::Source(b)) =
        (ceiling.provenance, concurrency.provenance)
    else {
        panic!("source constants")
    };
    assert!(a.starts_with("native/iroh_transport/src/sidecar.rs:96 @ f4e366e9"));
    assert!(b.starts_with("native/iroh_transport/src/sidecar.rs:100 @ f4e366e9"));
}

#[test]
fn sidecar_waits_keep_distinct_ids_from_equal_valued_budgets() {
    // The sidecar's own 5 s peer connect is not Python's caller-visible 1 s deadline, nor the
    // artifact carrier waits; same-valued ids with other provenance stay separate.
    let connect = BudgetId::SidecarPeerConnect.entry();
    assert_eq!(quantity(connect.value), Q::Ms(5_000));
    let retry_max = BudgetId::SidecarRetryMax.entry();
    assert_eq!(quantity(retry_max.value), quantity(connect.value));
    assert_ne!(retry_max.provenance, connect.provenance);
    let stream = BudgetId::SidecarStreamIo.entry();
    let ceiling = BudgetId::NodeControlCallTimeoutCeiling.entry();
    assert_eq!(quantity(stream.value), quantity(ceiling.value));
    assert_ne!(stream.provenance, ceiling.provenance);
    // Outbound lifetime = 240 s Python delivery budget + 60 s margin = half the 600 s tombstone horizon.
    assert_eq!(
        quantity(BudgetId::SidecarOutboundLifetime.entry().value),
        Q::Ms(300_000)
    );
    assert_eq!(300_000, 240_000 + 60_000);
    assert_eq!(300_000 * 2, 600_000);
    // seen_limit = max(capacity * 8, 64) with the source capacity 128.
    let (Q::Count(cap), Q::Count(factor), Q::Count(floor)) = (
        quantity(BudgetId::SidecarQueueCapacity.entry().value),
        quantity(BudgetId::SidecarSeenFactor.entry().value),
        quantity(BudgetId::SidecarSeenFloor.entry().value),
    ) else {
        panic!("counts")
    };
    assert_eq!(
        (cap, factor, floor, (cap * factor).max(floor)),
        (128, 8, 64, 1_024)
    );
}
