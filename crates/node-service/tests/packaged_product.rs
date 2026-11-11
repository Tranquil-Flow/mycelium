// SPDX-License-Identifier: AGPL-3.0-only
//! The packaged executable serves v0.1.4's product evidence routes (`GET /api/v1/product/{snapshot,
//! export,events}`, `mycelium_ui_gateway/app.py:348-352, 591-670` over `mycelium_product_spine/
//! app.py` @ f4e366e9) as the headless launcher does (crates/api/examples/headless/product.rs): one
//! application while the owner's seed runs, bound to its swarm, its replay window under
//! `<state>/serving/product`, a refused window answered with its typed reason until the owner's
//! `POST /api/v1/product/reset`, another swarm's window set aside (the set-asides pruned to the
//! newest 3, B15). The routes sit behind the packaged window channel exactly like `/api/status`
//! (GET) and every other owner action (POST).
#![cfg(feature = "desktop-shell")]
use api::{
    gateway::{Qualification, QualificationSource, Unavailable},
    resources::{Observation, Observer},
    schema::PublicError,
};
use budgets::{BudgetId as B, Entries};
use desktop::{
    channel::{Channel, ORIGIN, Request},
    core::{Core, Reply},
    lifecycle::{Cleanup, Poll},
    services::{ServiceRequest, Services},
};
use node_service::{
    desktop_host::DesktopServices,
    service::{Driver, Job, NodeService},
};
use serde_json::{Value, json};
use std::{
    collections::BTreeSet,
    num::NonZeroUsize,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    sync::Arc,
    time::{Duration, Instant},
};

const SNAPSHOT: &str = "/api/v1/product/snapshot";
const EXPORT: &str = "/api/v1/product/export";
const EVENTS: &str = "/api/v1/product/events";
const RESET: &str = "/api/v1/product/reset";
const STATE_FILE: &str = "product-evidence-state.v1.json";

fn millis(id: B) -> Duration {
    Duration::from_millis(id.span(Entries::ZERO).unwrap().millis())
}
struct MustNotRun;
impl Driver for MustNotRun {
    fn run(&self, _: &mut Job) -> Result<(), PublicError> {
        panic!("a product route started native work")
    }
}
fn state_root(name: &str) -> PathBuf {
    let root = std::env::temp_dir().canonicalize().unwrap().join(format!(
        "packaged-product-{name}-{}",
        node_service::service::random_id().unwrap()
    ));
    std::fs::DirBuilder::new()
        .mode(0o700)
        .create(&root)
        .unwrap();
    root
}
fn node() -> NodeService {
    NodeService::new(
        Arc::new(MustNotRun),
        "private-local-token".into(),
        node_service::native::development_qualification().unwrap(),
    )
}
/// The packaged composition (`desktop_host::compose` minus the native driver) over `root`.
fn compose(root: &Path) -> DesktopServices {
    DesktopServices::new(node(), NonZeroUsize::new(4).unwrap(), root)
}
/// The services' own Quit (fence, joined callbacks and loops, the node's cleanup), then dropped:
/// the next [`compose`] over the same root is a relaunch.
fn quit(host: DesktopServices) {
    let deadline = Instant::now() + millis(B::WorkerStartup);
    host.begin_shutdown(deadline).unwrap();
    while host.poll_shutdown().unwrap() != Poll::Verified {
        assert!(Instant::now() < deadline, "the services did not clean up");
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    }
    drop(host);
}
fn submit(host: &DesktopServices, method: &str, path: &str, headers: &[(&str, &str)]) -> Reply {
    submit_body(host, method, path, headers, Vec::new())
}
fn submit_body(
    host: &DesktopServices,
    method: &str,
    path: &str,
    headers: &[(&str, &str)],
    body: Vec<u8>,
) -> Reply {
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    host.submit(
        ServiceRequest {
            method: method.into(),
            target: path.into(),
            headers: headers
                .iter()
                .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
                .collect(),
            body,
        },
        Box::new(move |reply| tx.send(reply).unwrap()),
    );
    rx.recv_timeout(millis(B::WorkerStartup)).unwrap()
}
fn get(host: &DesktopServices, path: &str) -> Reply {
    submit(host, "GET", path, &[])
}
fn post(host: &DesktopServices, path: &str, body: Value) -> (u16, Value) {
    let reply = submit_body(
        host,
        "POST",
        path,
        &[
            ("x-mycelium-action", "1"),
            ("content-type", "application/json"),
        ],
        serde_json::to_vec(&body).unwrap(),
    );
    (reply.status, json_of(&reply))
}
fn json_of(reply: &Reply) -> Value {
    serde_json::from_slice(&reply.body)
        .unwrap_or_else(|_| panic!("not JSON: {}", String::from_utf8_lossy(&reply.body)))
}
fn answer(reply: &Reply) -> (u16, Value) {
    (reply.status, json_of(reply))
}
fn consent() -> Value {
    json!({
        "peer_visibility": true, "power": "ac_only",
        "storage_bytes": api::model_catalog::storage_consent_ceiling(),
        "network": "unmetered_only", "background": false, "diagnostics": false,
        "thermal": "stop_on_serious",
    })
}
fn create(host: &DesktopServices) {
    let created = post(host, "/api/create", json!({"consent": consent()}));
    assert_eq!(created.0, 200, "create: {}", created.1);
}
fn swarm_id(root: &Path) -> String {
    let record: Value =
        serde_json::from_slice(&std::fs::read(root.join("owner-swarm.json")).unwrap()).unwrap();
    record["swarm_id"].as_str().unwrap().to_owned()
}
/// The gateway's error document (`_send_error`, `app.py:1031-1053`).
fn unavailable(code: &str, retryable: bool) -> (u16, Value) {
    (
        503,
        json!({"protocol": "mycelium.product_ui.error.v1", "code": code, "retryable": retryable}),
    )
}
fn product_dir(root: &Path) -> PathBuf {
    root.join("serving/product")
}
fn mode(path: &Path) -> u32 {
    std::fs::symlink_metadata(path)
        .unwrap()
        .permissions()
        .mode()
        & 0o7777
}
fn source_state<'a>(snapshot: &'a Value, id: &str) -> &'a Value {
    snapshot["source_states"]
        .as_array()
        .unwrap()
        .iter()
        .find(|s| s["source_id"] == id)
        .unwrap_or_else(|| panic!("no {id} in {snapshot}"))
}
/// The SSE frames of one events answer: `id: <cursor>`, `event: product_snapshot`, `data: <event>`.
fn frames(reply: &Reply) -> Vec<(u64, Value)> {
    assert_eq!(
        reply.status,
        200,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    assert_eq!(reply.content_type, "text/event-stream; charset=utf-8");
    let text = String::from_utf8(reply.body.clone()).unwrap();
    assert!(text.ends_with("\n\n"), "{text}");
    text.trim_end_matches("\n\n")
        .split("\n\n")
        .map(|frame| {
            let lines: Vec<&str> = frame.split('\n').collect();
            assert_eq!(lines.len(), 3, "{frame}");
            assert_eq!(lines[1], "event: product_snapshot");
            let id: u64 = lines[0].strip_prefix("id: ").unwrap().parse().unwrap();
            let data: Value =
                serde_json::from_str(lines[2].strip_prefix("data: ").unwrap()).unwrap();
            assert_eq!(data["protocol"], "mycelium.product_event.v1");
            assert_eq!(data["cursor"], id);
            protocol::product::snapshot(&data["snapshot"]).unwrap();
            (id, data)
        })
        .collect()
}

/// No owner seed: every product route answers v0.1.4's `503 product_*_unavailable` (retryable, the
/// gateway's `_product_app is None`). Once `/api/create` started the seed the routes publish: one
/// validated snapshot per GET, the export, the events as resumable SSE frames, the window persisted
/// 0600 under `<state>/serving/product` and bound to the swarm; nothing private in the bodies. A
/// stopped authority answers 503 again, and the started one continues the same window.
#[test]
fn packaged_product_routes_are_served_once_the_owner_seed_runs() {
    let root = state_root("served");
    let host = compose(&root);
    for (path, code) in [
        (SNAPSHOT, "product_snapshot_unavailable"),
        (EXPORT, "product_export_unavailable"),
        (EVENTS, "product_events_unavailable"),
    ] {
        assert_eq!(answer(&get(&host, path)), unavailable(code, true), "{path}");
    }
    create(&host);
    let swarm = swarm_id(&root);

    let reply = get(&host, SNAPSHOT);
    assert_eq!(
        reply.status,
        200,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    assert_eq!(reply.content_type, "application/json");
    let snapshot = json_of(&reply);
    protocol::product::snapshot(&snapshot).unwrap();
    assert_eq!(snapshot["publication"]["cursor"], 1);
    assert_eq!(
        source_state(&snapshot, "membership-source")["status"],
        "current"
    );
    let devices: Vec<_> = snapshot["entities"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["kind"] == "device")
        .collect();
    assert_eq!(devices.len(), 1, "the owner is member 1: {snapshot}");
    assert!(
        !String::from_utf8_lossy(&reply.body).contains(&swarm),
        "the snapshot names the raw swarm id"
    );

    let export = get(&host, EXPORT);
    assert_eq!(export.status, 200);
    assert_eq!(json_of(&export)["publication"]["cursor"], 2);

    let all = frames(&get(&host, EVENTS));
    assert_eq!(all.iter().map(|(id, _)| *id).collect::<Vec<_>>(), [1, 2, 3]);
    let after = frames(&submit(&host, "GET", EVENTS, &[("last-event-id", "3")]));
    assert_eq!(after.iter().map(|(id, _)| *id).collect::<Vec<_>>(), [4]);
    assert_eq!(
        answer(&submit(&host, "GET", EVENTS, &[("last-event-id", "x")])),
        (
            400,
            json!({"protocol": "mycelium.product_ui.error.v1", "code": "invalid_last_event_id",
                   "retryable": false})
        )
    );
    // Only GET reaches a product read (`405`, the gateway's method refusal).
    assert_eq!(
        answer(&submit(&host, "POST", SNAPSHOT, &[])).1["code"],
        "method_not_allowed"
    );

    let state = product_dir(&root).join(STATE_FILE);
    assert_eq!(mode(&product_dir(&root)), 0o700);
    assert_eq!(mode(&state), 0o600);
    let stored: Value = serde_json::from_slice(&std::fs::read(&state).unwrap()).unwrap();
    assert_eq!(stored["binding"], swarm.as_str());
    assert_eq!(stored["events"].as_array().unwrap().len(), 4);

    assert_eq!(post(&host, "/api/stop", json!({})).0, 200);
    assert_eq!(
        answer(&get(&host, SNAPSHOT)),
        unavailable("product_snapshot_unavailable", true)
    );
    assert_eq!(post(&host, "/api/start", json!({})).0, 200);
    let resumed = json_of(&get(&host, SNAPSHOT));
    assert_eq!(
        resumed["publication"]["cursor"], 5,
        "the same window continues"
    );
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// This Mac as the window's consent permits it (no download runs here).
struct Permissive;
impl Observer for Permissive {
    fn observe(&self) -> Observation {
        Observation {
            power: "external".into(),
            thermal: "nominal".into(),
            network: "unmetered".into(),
        }
    }
}
/// A request as the packaged window delivers it: the channel decides first (window label, exact
/// Origin, `X-Mycelium-Action` and JSON on a POST), then the core answers.
fn through_window(core: &Arc<dyn Core>, request: Request) -> (u16, Value) {
    if Channel::default().authorize(&request).is_err() {
        return (403, json!({"error": "window_channel_rejected"}));
    }
    let (tx, rx) = std::sync::mpsc::sync_channel(1);
    core.submit(request, Box::new(move |reply| tx.send(reply).unwrap()));
    let reply = rx.recv_timeout(millis(B::WorkerStartup)).unwrap();
    answer(&reply)
}
fn window_request(window: &str, method: &str, path: &str, headers: &[(&str, &str)]) -> Request {
    Request {
        window: window.into(),
        method: method.into(),
        uri: format!("{ORIGIN}{path}"),
        headers: headers
            .iter()
            .map(|(k, v)| ((*k).to_owned(), (*v).to_owned()))
            .collect(),
        body: if method == "POST" {
            b"{}".to_vec()
        } else {
            Vec::new()
        },
    }
}

/// The product reads take the window's rules of `/api/status` and the reset those of every owner
/// action: refused for another window, a foreign or doubled Origin, and (POST) a missing
/// `X-Mycelium-Action` or JSON type; served otherwise. The packaged executable's composition
/// (`packaged::core_with`) serves them.
#[test]
fn packaged_product_routes_follow_the_window_rules_of_status_and_actions() {
    let state = state_root("window");
    let (core, mut cleanup) = node_service::packaged::core_with(
        &state,
        Arc::new(api::acquisition::HttpTransport::hugging_face(true)),
        Arc::new(Permissive),
    )
    .unwrap();
    cleanup.bind_clock(Instant::now()).unwrap();
    let refused = (403, json!({"error": "window_channel_rejected"}));
    let reads: [&[(&str, &str)]; 4] = [
        &[],
        &[("origin", ORIGIN)],
        &[("origin", "null")],
        &[("origin", ORIGIN), ("origin", ORIGIN)],
    ];
    for headers in reads {
        let status = through_window(&core, window_request("main", "GET", "/api/status", headers));
        let product = through_window(&core, window_request("main", "GET", SNAPSHOT, headers));
        assert_eq!(
            status.0 == 403,
            product == refused,
            "{headers:?}: status {status:?}, product {product:?}"
        );
        if status.0 != 403 {
            assert_eq!(product, unavailable("product_snapshot_unavailable", true));
        }
    }
    for path in ["/api/status", SNAPSHOT] {
        assert_eq!(
            through_window(&core, window_request("other", "GET", path, &[])),
            refused
        );
    }
    let action = [
        ("origin", ORIGIN),
        ("x-mycelium-action", "1"),
        ("content-type", "application/json"),
    ];
    let posts: [&[(&str, &str)]; 4] = [
        &action[..],
        &action[1..],
        &[action[0], action[2]],
        &[("origin", "null"), action[1], action[2]],
    ];
    for headers in posts {
        let unpublish = through_window(
            &core,
            window_request("main", "POST", "/api/swarm/unpublish", headers),
        );
        let reset = through_window(&core, window_request("main", "POST", RESET, headers));
        assert_eq!(unpublish == refused, reset == refused, "{headers:?}");
        if unpublish != refused {
            // No owner seed: nothing to reset (the headless answer).
            assert_eq!(
                reset,
                (
                    409,
                    json!({"protocol": "mycelium.product_ui.error.v1",
                           "code": "product_reset_unavailable", "retryable": false})
                )
            );
        }
    }
    let created = through_window(
        &core,
        Request {
            body: serde_json::to_vec(&json!({"consent": consent()})).unwrap(),
            ..window_request("main", "POST", "/api/create", &action)
        },
    );
    assert_eq!(created.0, 200, "create: {}", created.1);
    let served = through_window(&core, window_request("main", "GET", SNAPSHOT, &[]));
    assert_eq!(served.0, 200, "{}", served.1);
    assert_eq!(
        through_window(&core, window_request("main", "POST", RESET, &action)),
        (
            409,
            json!({"protocol": "mycelium.product_ui.error.v1",
                   "code": "product_evidence_state_not_refused", "retryable": false})
        )
    );
    cleanup
        .begin(desktop::lifecycle::quit_allowance().unwrap())
        .unwrap();
    let deadline = Instant::now() + millis(B::WorkerStartup);
    while cleanup.poll().unwrap() != Poll::Verified {
        assert!(
            Instant::now() < deadline,
            "the packaged core did not clean up"
        );
        std::thread::sleep(millis(B::OwnerPoll) / 5);
    }
    drop(core);
    std::fs::remove_dir_all(&state).unwrap_or_default();
}

fn write_private(path: &Path, bytes: &[u8]) {
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create(true)
        .truncate(true)
        .mode(0o600)
        .open(path)
        .unwrap();
    std::io::Write::write_all(&mut file, bytes).unwrap();
}
fn set_asides(root: &Path) -> BTreeSet<String> {
    std::fs::read_dir(product_dir(root))
        .unwrap()
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .filter(|name| name.starts_with(&format!("{STATE_FILE}.")))
        .collect()
}

/// A window that cannot be read (after a relaunch) refuses the routes with `503` and its typed
/// `reason`, not retryable, until the owner resets it: the reset moves the bytes aside (0600) and
/// the next GET opens a fresh window; a second reset has nothing to move.
#[test]
fn a_corrupt_window_is_refused_with_its_reason_until_the_owner_resets_it() {
    let root = state_root("corrupt");
    let host = compose(&root);
    create(&host);
    assert_eq!(get(&host, SNAPSHOT).status, 200);
    quit(host);
    let state = product_dir(&root).join(STATE_FILE);
    write_private(&state, b"{not a window");

    let host = compose(&root);
    let refused = |code: &str| {
        (
            503,
            json!({"protocol": "mycelium.product_ui.error.v1", "code": code,
                   "reason": "product_evidence_state_corrupt", "retryable": false}),
        )
    };
    assert_eq!(
        answer(&get(&host, SNAPSHOT)),
        refused("product_snapshot_unavailable")
    );
    assert_eq!(
        answer(&get(&host, EVENTS)),
        refused("product_events_unavailable")
    );
    assert_eq!(std::fs::read(&state).unwrap(), b"{not a window");

    let (status, reset) = post(&host, RESET, json!({}));
    assert_eq!(status, 200, "{reset}");
    assert_eq!(reset["protocol"], "mycelium.product_evidence_reset.v1");
    assert_eq!(reset["reason"], "product_evidence_state_corrupt");
    let moved = reset["set_aside_as"].as_str().unwrap();
    assert!(
        moved.starts_with(&format!("{STATE_FILE}.reset-")),
        "{moved}"
    );
    let moved = product_dir(&root).join(moved);
    assert_eq!(std::fs::read(&moved).unwrap(), b"{not a window");
    assert_eq!(mode(&moved), 0o600);

    let fresh = get(&host, SNAPSHOT);
    assert_eq!(fresh.status, 200);
    assert_eq!(json_of(&fresh)["publication"]["cursor"], 1);
    assert_eq!(
        post(&host, RESET, json!({})),
        (
            409,
            json!({"protocol": "mycelium.product_ui.error.v1",
                   "code": "product_evidence_state_not_refused", "retryable": false})
        )
    );
    // The reset is an owner action with an empty body.
    assert_eq!(post(&host, RESET, json!({"force": true})).0, 400);
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// A window bound to another swarm is never restored: it is set aside when the application opens
/// and this swarm starts its own window; the set-asides are pruned to the newest 3 of any kind
/// (B15), the one just moved always kept, the active window never touched.
#[test]
fn a_window_of_another_swarm_is_set_aside_and_old_set_asides_are_pruned() {
    let root = state_root("foreign");
    let host = compose(&root);
    create(&host);
    assert_eq!(get(&host, SNAPSHOT).status, 200);
    assert_eq!(get(&host, SNAPSHOT).status, 200);
    quit(host);
    let state = product_dir(&root).join(STATE_FILE);
    let mut window: Value = serde_json::from_slice(&std::fs::read(&state).unwrap()).unwrap();
    window["binding"] = json!("another-swarm-0123456789abcdef");
    let foreign = serde_json::to_vec(&window).unwrap();
    write_private(&state, &foreign);
    for planted in [
        "reset-1000",
        "reset-2000",
        "foreign-swarm-3000",
        "unbound-4000",
    ] {
        write_private(
            &product_dir(&root).join(format!("{STATE_FILE}.{planted}")),
            b"older",
        );
    }

    let host = compose(&root);
    let reply = get(&host, SNAPSHOT);
    assert_eq!(
        reply.status,
        200,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    assert_eq!(
        json_of(&reply)["publication"]["cursor"],
        1,
        "a fresh window"
    );
    let stored: Value = serde_json::from_slice(&std::fs::read(&state).unwrap()).unwrap();
    assert_eq!(stored["binding"], swarm_id(&root).as_str());
    let kept = set_asides(&root);
    assert_eq!(kept.len(), 3, "{kept:?}");
    assert!(
        kept.contains(&format!("{STATE_FILE}.unbound-4000")),
        "{kept:?}"
    );
    assert!(
        kept.contains(&format!("{STATE_FILE}.foreign-swarm-3000")),
        "{kept:?}"
    );
    let moved: Vec<_> = kept
        .iter()
        .filter(|name| !name.ends_with("-3000") && !name.ends_with("-4000"))
        .collect();
    assert_eq!(moved.len(), 1, "{kept:?}");
    assert!(
        moved[0].starts_with(&format!("{STATE_FILE}.foreign-swarm-")),
        "{kept:?}"
    );
    assert_eq!(
        std::fs::read(product_dir(&root).join(moved[0])).unwrap(),
        foreign
    );
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

fn source_notice<'a>(snapshot: &'a Value, source: &str) -> Option<&'a Value> {
    snapshot["notices"]
        .as_array()
        .unwrap()
        .iter()
        .find(|n| n["notice_id"] == format!("source-{source}-missing").as_str())
}
/// v0.1.4's sorted UTF-8 JSON bytes of `value` (`dumps_sorted`), as text for a readable diff.
fn sorted(value: &Value) -> String {
    String::from_utf8(api::wire::dumps_sorted(value, api::wire::Text::Utf8)).unwrap()
}
/// An owner whose seed runs, and its first snapshot.
fn served(name: &str) -> (PathBuf, DesktopServices, Value) {
    let root = state_root(name);
    let host = compose(&root);
    create(&host);
    let reply = get(&host, SNAPSHOT);
    assert_eq!(
        reply.status,
        200,
        "{}",
        String::from_utf8_lossy(&reply.body)
    );
    let snapshot = json_of(&reply);
    (root, host, snapshot)
}

/// `route_source` is `route.public_status` read through the live route (`_follow`,
/// supervisor.py:2153-2164 @ f4e366e9). The packaged process never opens a qualified live route
/// (`serving.route_ready: false`), so the read is v0.1.4's `RouteUnavailableError('route_not_open')`,
/// which `_follow` leaves untyped: `route_source_failed`, never a route invented from the
/// development slice.
#[test]
fn the_packaged_route_source_reads_the_live_route_it_does_not_have() {
    let (root, host, snapshot) = served("route");
    let row = source_state(&snapshot, "route-source");
    assert_eq!(
        sorted(row),
        sorted(
            &json!({"authority": "live_router_status", "generation": null,
                       "observed_at_unix_ms": null, "reason_code": "route_source_failed",
                       "source_id": "route-source", "status": "missing",
                       "valid_until_unix_ms": null})
        ),
        "{row}"
    );
    assert_eq!(
        source_notice(&snapshot, "route-source").unwrap()["code"],
        "route_source_failed"
    );
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// The v0.1.4 projector's own output on recorded steps (FX-B13-PRODUCT-SNAPSHOTS).
const ORACLE: &str = include_str!("../../api/tests/fixtures/oracle_product_snapshots.json");
/// `source_states` row and source notice of `source` in the oracle's snapshot at `case[step]`.
fn oracle(case: &str, step: usize, source: &str) -> (Value, Option<Value>) {
    let fixture: Value = serde_json::from_str(ORACLE).unwrap();
    let expected = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["case"] == case)
        .unwrap()["steps"][step]["expected"]
        .clone();
    (
        source_state(&expected, source).clone(),
        source_notice(&expected, source).cloned(),
    )
}
/// The source's `source_states` row and notice equal the oracle's at `case[step]`, byte for byte.
fn assert_source_as_oracle(snapshot: &Value, source: &str, case: &str, step: usize) {
    let (row, notice) = oracle(case, step, source);
    assert_eq!(
        sorted(source_state(snapshot, source)),
        sorted(&row),
        "{source}"
    );
    assert_eq!(
        source_notice(snapshot, source).map(sorted),
        notice.as_ref().map(sorted),
        "{source}"
    );
}
/// A qualification source that cannot be read (`qualification_source.current()` raising).
struct Unreadable;
impl QualificationSource for Unreadable {
    fn current(&self, _: Option<&str>) -> Result<Option<Qualification>, Unavailable> {
        Err(Unavailable)
    }
}

/// `qualification_source` is `route_qualification_to_dict(qualification_source.current())`
/// (supervisor.py:2166-2168): the serving root's qualification source. The packaged development
/// record has no `mycelium.route_qualification.v1` document (`qualification_source_missing`, the
/// oracle's `owner_only` row), and a source that cannot be read publishes
/// `qualification_source_failed` (the oracle's `serving[2]` row).
#[test]
fn the_packaged_qualification_source_is_the_serving_roots_qualifier() {
    let (root, host, snapshot) = served("qualification");
    assert_source_as_oracle(&snapshot, "qualification-source", "owner_only", 0);
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();

    let root = state_root("qualification-unreadable");
    let host = DesktopServices::with_qualification(
        node(),
        NonZeroUsize::new(4).unwrap(),
        &root,
        Arc::new(Unreadable),
    );
    create(&host);
    let snapshot = json_of(&get(&host, SNAPSHOT));
    assert_source_as_oracle(&snapshot, "qualification-source", "serving", 2);
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}

/// A qualifier whose current record has its public document: the oracle's synthetic
/// `incidents_and_conflict` record (`synthetic_test_fixture`, `route_ready: false`).
struct Recorded(Value);
impl QualificationSource for Recorded {
    fn current(&self, _: Option<&str>) -> Result<Option<Qualification>, Unavailable> {
        Ok(None)
    }
    fn record(&self) -> Result<Option<Value>, Unavailable> {
        Ok(Some(self.0.clone()))
    }
}

/// A record the serving root's source has is published as it is: with no live route to bind it to
/// it is `conflict` / `qualification_binding_conflict`, the oracle's `incidents_and_conflict` row.
#[test]
fn the_serving_roots_qualification_record_reaches_the_snapshot() {
    let fixture: Value = serde_json::from_str(ORACLE).unwrap();
    let record = fixture["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["case"] == "incidents_and_conflict")
        .unwrap()["steps"][0]["qualification"]
        .clone();
    assert_eq!(record["protocol"], "mycelium.route_qualification.v1");
    let root = state_root("qualification-record");
    let host = DesktopServices::with_qualification(
        node(),
        NonZeroUsize::new(4).unwrap(),
        &root,
        Arc::new(Recorded(record)),
    );
    create(&host);
    let snapshot = json_of(&get(&host, SNAPSHOT));
    assert_source_as_oracle(
        &snapshot,
        "qualification-source",
        "incidents_and_conflict",
        0,
    );
    quit(host);
    std::fs::remove_dir_all(&root).unwrap_or_default();
}
