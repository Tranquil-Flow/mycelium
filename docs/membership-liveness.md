# Membership liveness semantics

This document freezes the Phase 2R.4 liveness boundary before physical qualification.

## Signed evidence

`mycelium.membership.heartbeat.v1` is the only member-to-seed liveness message. Its `liveness_source` is one of:

- `scheduled_heartbeat`: ordinary idle or in-flight keepalive; receipt fields are null.
- `activation_receipt`: node-observed authenticated Iroh delivery evidence; receipt digest and signed-assignment peer node are required.

A node may emit `activation_receipt` only from a 32-byte `DeliveryReceipt` whose EndpointID, membership generation, delivery semantics, and Router wire protocol match a currently valid seed-signed peer record. Receipt replay is rejected. Seed verifies the node signature and renews the same membership lease path used by scheduled heartbeats.

Seed route authority remains absent: liveness updates membership eligibility but never constructs, mutates, or promotes a route.

## Suppression and due time

A seed-accepted activation receipt suppresses exactly the node's next scheduled heartbeat. The node consumes that suppression once, then resumes scheduled heartbeat emission when idle. A forced operational heartbeat does not consume the scheduled suppression.

Seed persists `next_heartbeat_due_at` so this deliberate skipped beat is not classified as failure:

- join or scheduled heartbeat: next beat due after one keepalive interval;
- activation receipt: next beat due after two keepalive intervals.

Default intervals are:

- keepalive interval: 30 seconds;
- evidence freshness: 90 seconds;
- membership lease: 300 seconds.

Callers may configure these before startup. Values must be finite and positive, and evidence freshness must exceed the keepalive interval.

## State classification

- `idle_fresh`: no active requests and heartbeat not overdue.
- `active_fresh`: active requests and heartbeat not overdue, including receipt suppression grace.
- `liveness_stale`: idle member has missed at least one due heartbeat.
- `active_decode_transport_failure`: member reported an in-flight request, then missed its due heartbeat. This is not treated as idle death.
- `dead`: idle member has missed at least two due heartbeats and signed evidence freshness has expired.

One missed heartbeat never marks a member dead. New assignments fail closed for stale/dead targets and stale/dead peers. A valid signed heartbeat can recover a stale/dead liveness classification while the membership lease remains valid. Expired membership leases remain independently rejected.

## Durable fields

SQLite schema v4 persists heartbeat sequence, last liveness time, next due time, last activity-receipt time, active request count, and lifecycle state. Migrated legacy rows use zero timestamps and therefore fail closed until fresh signed evidence arrives.
