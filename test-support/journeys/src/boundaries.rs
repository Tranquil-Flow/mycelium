// SPDX-License-Identifier: AGPL-3.0-only
//! Reachable loopback boundaries, not successful joined-member qualification.
use crate::{
    Result, duration,
    process::{App, Target},
    require,
    scenario::{Run, consent, no_standing},
};
use budgets::BudgetId;
use serde_json::{Value, json};
use std::{path::Path, thread, time::Instant};

const APP: &str = "O:path:mycelium_a13_macos/app.py";
const PAIR: &str = "O:path:mycelium_a13_macos/pairing.py";
const PEER: &str = "O:path:mycelium_a13_macos/peer.py";
const LEAVE: &str = "O:path:mycelium_a13_macos/member_leave.py";
const CONSENT: &str = "O:path:mycelium_a13_macos/standing_consent.py";
const RELAY: &str = "O:path:mycelium_a13_macos/public_relay.py";

/// The product can say `left` even when a cleanup step failed. Check the steps too.
pub fn validate_leave(value: &Value, mode: &str) -> Result<()> {
    require(matches!(mode, "instant" | "smooth"), "leave:mode")?;
    let mut names = vec!["fence"];
    if mode == "smooth" {
        names.extend(["unload", "publish_withdrawal"]);
    }
    names.extend(["withdraw_consent", "disconnect", "cancel_work"]);
    let steps: Vec<_> = names
        .into_iter()
        .map(|name| json!({"name":name,"ok":true,"code":null}))
        .collect();
    let mut expected =
        json!({"state":"left","mode":mode,"fallback":null,"handoff":"unavailable","steps":steps});
    // The Python golden predates this additive field. This corpus owns no compute;
    // if a proof is supplied, only the positive empty-owned-set proof is valid.
    if value.get("compute_cleanup").is_some() {
        expected["compute_cleanup"] = json!({"state":"none_owned","code":null});
    }
    require(value == &expected, "leave:incomplete_or_failed_steps")
}

fn post(
    run: &mut Run,
    app: &App,
    id: &str,
    obligation: &str,
    path: &str,
    body: Value,
    expected: (u16, Value),
) -> Result<Value> {
    let response = app.api()?.post(path, &body)?;
    run.observe(
        id,
        obligation,
        ("POST", path),
        response,
        expected.0,
        expected.1,
    )
}

fn wait_status(
    run: &mut Run,
    app: &mut App,
    id: &str,
    obligation: &str,
    expected: Value,
) -> Result<Value> {
    // Failed actions do not synchronously publish (app.py:1315-1325).
    // Keep the exact expected state; wait for the maintenance publisher, not a stale snapshot.
    let deadline = Instant::now() + duration(BudgetId::ModelSelection)?;
    let keys = expected
        .as_object()
        .ok_or("status:expected_map")?
        .keys()
        .map(String::as_str)
        .collect::<Vec<_>>();
    loop {
        let response = app.api()?.get("/api/status")?;
        require(response.status == 200, "status:poll_http")?;
        let value = response.json()?;
        if !crate::scenario::quiescent(&value) {
            app.abort_forbidden()?;
            return Err("isolation:forbidden_acquisition_or_serving".into());
        }
        app.check_processes()?;
        let projected = crate::project(&value, &keys)?;
        if projected == expected {
            return run.observe(
                id,
                obligation,
                ("GET", "/api/status"),
                response,
                200,
                expected,
            );
        }
        require(
            Instant::now() < deadline,
            &format!("status:{id}:publication_deadline:observed={projected}"),
        )?;
        thread::sleep(
            duration(BudgetId::OwnerPoll)?.min(deadline.saturating_duration_since(Instant::now())),
        );
    }
}

fn left(run: &mut Run, app: &mut App, mode: &str) -> Result<()> {
    let response = app
        .api()?
        .post("/api/leave", &json!({"mode":mode,"confirm":true}))?;
    require(response.status == 200, "leave:http_status")?;
    // Smooth leave is asynchronous. Poll published state with named budgets, never sleep-to-pass.
    let deadline = Instant::now() + duration(BudgetId::WorkerStartup)?;
    let mut value = response.json()?;
    loop {
        if value["state"] == "left" {
            break;
        }
        require(Instant::now() < deadline, "leave:deadline")?;
        thread::sleep(duration(BudgetId::OwnerPoll)?);
        let response = app.api()?.get("/api/status")?;
        require(response.status == 200, "leave:poll_status")?;
        let status = response.json()?;
        require(crate::scenario::quiescent(&status), "leave:not_quiescent")?;
        value = status["leave"].clone();
    }
    validate_leave(&value, mode)?;
    run.fact(&format!("unjoined_{mode}_leave_complete"), LEAVE,
        json!({"mode":mode,"state":"left","all_steps_ok":true,"fallback":null,"handoff":"unavailable","joined_member":false}));
    run.status(app, &format!("unjoined_{mode}_leave_status"), LEAVE,
        json!({"/leave/state":"left","/leave/mode":mode,"/resource_policy/storage_bytes":2_000_000_000u64,
            "/authority/membership_state":"absent","/join_flow/standing_consent":null,"/demo_target/state":"not_joined"}))?;
    Ok(())
}

fn pair_boundaries(run: &mut Run, owner: &mut App, member: &mut App) -> Result<()> {
    require(
        owner.scratch.home != member.scratch.home
            && owner.scratch.state != member.scratch.state
            && owner.api()?.origin != member.api()?.origin
            && owner.receipt["pid"] != member.receipt["pid"],
        "pair:instances_not_distinct",
    )?;
    post(
        run,
        owner,
        "boundary_owner_created",
        APP,
        "/api/create",
        consent(4_000_000_000),
        (
            200,
            json!({"/membership_state":"current","/route_ready":false}),
        ),
    )?;
    let request = post(
        run,
        member,
        "boundary_member_request",
        PAIR,
        "/api/join/request",
        consent(2_000_000_000),
        (
            200,
            json!({"/protocol":"mycelium.pairing_request.v1","/peer_class":"mac_mlx_iroh"}),
        ),
    )?;
    require(request["signature"].is_object(), "pair:signature_missing")?;
    run.status(owner, "simultaneous_owner_status", APP,
        json!({"/authority/member_count":1,"/authority/membership_state":"current","/resource_policy/storage_bytes":4_000_000_000u64}))?;
    run.status(member, "simultaneous_unjoined_member_status", CONSENT,
        json!({"/authority/membership_state":"absent","/resource_policy/storage_bytes":2_000_000_000u64,"/join_flow/standing_consent":null}))?;
    no_standing(owner)?;
    run.fact("two_live_isolated_instances", APP,
        json!({"distinct_homes":true,"distinct_state_dirs":true,"distinct_listeners":true,"distinct_pids":true,"owner_standing_grant_exists":false}));

    // This is a chosen invitation lifetime, not a member's execution grant.
    let invitation_seconds = duration(BudgetId::WorkerStartup)?.as_secs();
    let window = post(
        run,
        owner,
        "join_window_waits_for_public_relay",
        PAIR,
        "/api/pair/window/open",
        json!({"ttl_seconds":invitation_seconds}),
        (
            200,
            json!({"/state":"waiting_for_relay","/fix/action":"start_relay"}),
        ),
    )?;
    require(
        window["pending_id"].is_string()
            && window.get("join_link").is_none()
            && window.get("code").is_none(),
        "pair:pending_window_redeemable",
    )?;
    run.status(owner, "pending_window_status", PAIR,
        json!({"/join_flow/pending_windows/0/state":"waiting_for_relay","/join_flow/pending_windows/0/ttl_seconds":invitation_seconds}))?;
    let revoked = post(
        run,
        owner,
        "pending_window_revoked",
        PAIR,
        "/api/pair/window/revoke",
        json!({"window_id":window["pending_id"]}),
        (200, json!({"/state":"revoked"})),
    )?;
    require(
        revoked["window_id"] == window["pending_id"],
        "pair:revocation_identity",
    )?;
    let status = owner.api()?.get("/api/status")?.json()?;
    require(
        status["join_flow"]["pending_windows"] == json!([])
            && status["join_flow"]["pairing_windows"] == json!([]),
        "pair:window_not_removed",
    )?;
    run.fact(
        "revoked_window_absent",
        PAIR,
        json!({"pending_windows":0,"redeemable_windows":0}),
    );
    // The app-supported external provider rejects local origins BEFORE starting a proxy.
    post(
        run,
        owner,
        "external_loopback_relay_refused",
        RELAY,
        "/api/network/start",
        json!({"provider":"external","public_origin":"https://127.0.0.1","proxy_port":49152,
            "relay_acknowledged":true,"max_seconds":duration(BudgetId::RelayMinimumWindow)?.as_secs(),
            "cloudflare_front":false,"auto_renew":false,"long_lived":false}),
        (400, json!({"/error":"relay_origin_invalid"})),
    )?;
    run.status(owner, "relay_refusal_keeps_owner_private", RELAY,
        json!({"/network/state":"private","/authority/member_count":1,"/serving/route_ready":false}))?;

    let approval = json!({"request":request,"ttl_seconds":invitation_seconds,
        "quota":{"storage_bytes":2_000_000_000u64,"network_bytes_per_day":2_000_000_000u64,"background_allowed":false}});
    let handoff = post(
        run,
        owner,
        "manual_pairing_handoff_issued",
        PAIR,
        "/api/pair/approve",
        approval.clone(),
        (
            200,
            json!({"/protocol":"mycelium.encrypted_invitation_handoff.v1"}),
        ),
    )?;
    require(
        handoff["ciphertext"]
            .as_str()
            .is_some_and(|s| !s.is_empty()),
        "pair:ciphertext_missing",
    )?;
    let repeated = post(
        run,
        owner,
        "manual_pairing_approval_idempotent",
        PAIR,
        "/api/pair/approve",
        approval.clone(),
        (
            200,
            json!({"/protocol":"mycelium.encrypted_invitation_handoff.v1"}),
        ),
    )?;
    require(handoff == repeated, "pair:approval_not_idempotent")?;
    let mut changed = approval;
    changed["quota"]["storage_bytes"] = json!(4_000_000_000u64);
    post(
        run,
        owner,
        "manual_pairing_quota_change_refused",
        PAIR,
        "/api/pair/approve",
        changed,
        (409, json!({"/error":"pairing_approval_changed"})),
    )?;
    let mut tampered = handoff.clone();
    tampered["request_digest"] = json!(format!("sha256:{}", "0".repeat(64)));
    post(
        run,
        member,
        "handoff_request_binding_refused",
        PAIR,
        "/api/pair/accept",
        json!({"handoff":tampered}),
        (409, json!({"/error":"pairing_handoff_request_mismatch"})),
    )?;
    // The genuine handoff contains the actual owner's loopback TLS endpoint. No CA injection.
    post(
        run,
        member,
        "manual_join_loopback_tls_untrusted",
        PEER,
        "/api/pair/accept",
        json!({"handoff":handoff}),
        (409, json!({"/error":"peer_transport_unreachable"})),
    )?;
    wait_status(
        run,
        member,
        "failed_manual_join_is_not_membership",
        PEER,
        json!({"/authority/membership_state":"absent","/authority/pairing_state":"handoff_retained",
            "/join_flow/standing_consent":null,"/resource_policy/storage_bytes":2_000_000_000u64}),
    )?;
    run.status(
        owner,
        "failed_manual_join_did_not_enroll_member",
        APP,
        json!({"/authority/member_count":1,"/authority/contribution_enabled":false}),
    )?;
    no_standing(owner)?;
    post(
        run,
        member,
        "saved2_after_failed_join_no_upgrade",
        CONSENT,
        "/api/join/request",
        consent(4_000_000_000),
        (400, json!({"/error":"consent_retry_changed"})),
    )?;
    post(
        run,
        member,
        "unjoined_standing_withdraw_missing",
        CONSENT,
        "/api/consent/standing/withdraw",
        json!({"confirm":true}),
        (400, json!({"/error":"standing_consent_missing"})),
    )?;
    post(
        run,
        member,
        "unjoined_window_poll_missing",
        PAIR,
        "/api/join/window/poll",
        json!({}),
        (400, json!({"/error":"join_window_missing"})),
    )?;
    post(
        run,
        member,
        "unjoined_window_cancel",
        PAIR,
        "/api/join/window/cancel",
        json!({}),
        (200, json!({"/state":"cancelled"})),
    )?;
    let response = member.api()?.get("/api/stats")?;
    run.observe(
        "member_private_stats_not_exposed",
        "O:path:mycelium_a13_macos/swarm_stats.py",
        ("GET", "/api/stats"),
        response,
        404,
        json!({"/error":"not_found"}),
    )?;
    post(
        run,
        owner,
        "no_model_no_execution_proposal",
        "O:path:mycelium_a13_macos/chain_compile.py",
        "/api/serving/status",
        json!({}),
        (
            200,
            json!({"/compilation":null,"/serving":null,"/route_ready":false}),
        ),
    )?;
    post(
        run,
        owner,
        "owner_cannot_leave_as_member",
        LEAVE,
        "/api/leave",
        json!({"mode":"instant","confirm":true}),
        (400, json!({"/error":"target_role_required"})),
    )?;
    post(
        run,
        member,
        "leave_requires_confirmation",
        LEAVE,
        "/api/leave",
        json!({"mode":"instant"}),
        (400, json!({"/error":"leave_confirmation_required"})),
    )?;
    post(
        run,
        member,
        "leave_rejects_unknown_mode",
        LEAVE,
        "/api/leave",
        json!({"mode":"unknown","confirm":true}),
        (400, json!({"/error":"leave_mode_invalid"})),
    )?;
    left(run, member, "instant")?;
    Ok(())
}

pub fn execute(target: &Target, home: &Path, run: &mut Run) -> Result<()> {
    let mut owner = App::launch(target, home, None)?;
    let mut member = match App::launch(target, home, None) {
        Ok(member) => member,
        Err(error) => return run.finish(&mut owner, Err(error)),
    };
    let action = pair_boundaries(run, &mut owner, &mut member);
    // Always stop BOTH owned processes, even if either action or cleanup failed.
    let member_result = run.finish(&mut member, action);
    run.finish(&mut owner, member_result)?;
    let mut restarted = App::launch(target, home, Some(&member.scratch))?;
    let action = (|| {
        run.status(&mut restarted, "unjoined_leave_restart_preserved", LEAVE,
            json!({"/leave/state":"left","/leave/mode":"instant","/resource_policy/storage_bytes":2_000_000_000u64,
                "/authority/membership_state":"absent","/join_flow/standing_consent":null}))?;
        left(run, &mut restarted, "smooth")?;
        Ok(())
    })();
    run.finish(&mut restarted, action)?;
    owner.scratch.remove()?;
    member.scratch.remove()?;
    restarted.scratch.remove()?;
    run.fact("boundary_clean_teardown", "O:path:mycelium_a13_macos/__main__.py",
        json!({"all_exit_zero":true,"leftover_pids":0,"scratch_removed":true,"no_serving_or_download":true}));
    Ok(())
}

pub fn run_record(target: &Target, home: &Path) -> Value {
    let started = Instant::now();
    let mut run = Run::new();
    let result = execute(target, home, &mut run);
    json!({"schema":"mycelium.journeys.v1","scenario":"loopback-boundaries",
    "implementation":target.implementation,"source_sha":target.source_sha,"executable_sha256":target.executable_sha256,
    "elapsed_ms":started.elapsed().as_millis(),"result":if result.is_ok(){"PASS"}else{"FAIL"},
    "error":result.err(),"observations":run.observations,"launches":run.launches,
    "exclusions":[
        {"journey":"two_instance_join_and_bound_600s_2GB_consent","result":"BLOCKED","reason":"Join windows wait for public HTTPS relay; external relay rejects loopback origins. Manual recipient-encrypted handoff cannot use owner's private loopback TLS CA through the production Web-PKI client. No public CLI/env trust or join transport seam; private constructor injection forbidden."},
        {"journey":"joined_member_leave_and_owner_restart_recovery_F3","result":"BLOCKED","reason":"Requires successful joined membership. Exercised leave is explicitly UNJOINED, including cleanup steps and restart persistence."},
        {"journey":"published_stats_shape","result":"BLOCKED","reason":"Private app endpoint is 404. Stats publication is external-directory POST, requiring separately authorized public origin/registration."},
        {"journey":"placement_reservation_worker_deadlines_F14_F15_F15b","result":"BLOCKED","reason":"No plan-only HTTP route. Compilation/proposals require prepared model source and distinct-host observations; public serving/status has null compilation/serving. No model acquisition, private injection or worker serving authorized."},
        {"journey":"serve_chat_cancel_product_SSE_F5_stall","result":"BLOCKED","reason":"No model-backed running route or compilation stall allowed. Idle status and leave/window cancellation are not active chat cancellation or F5 proof."},
        {"journey":"cross_version_saved2_upgrade","result":"BLOCKED","reason":"Only final executable is launch-authorized. Same-byte 2GB persistence and no-upgrade refusals are not installer/cross-version migration."}
    ]})
}
