// SPDX-License-Identifier: AGPL-3.0-only
//! Source-derived assertions shared unchanged by oracle and future Rust targets.
use crate::{
    Result,
    http::Response,
    io,
    process::{App, Target},
    project, require,
};
use serde_json::{Value, json};
use std::{fs, path::Path, time::Instant};
const APP: &str = "O:path:mycelium_a13_macos/app.py";
const UI: &str = "O:path:mycelium_a13_macos/web/index.html";
const MODE: &str = "O:path:mycelium_a13_macos/fast_mode.py";
const CONSENT: &str = "O:path:mycelium_a13_macos/standing_consent.py";
const PAIR: &str = "O:path:mycelium_a13_macos/pairing.py";
const MODEL: &str = "O:path:mycelium_a13_macos/run_model.py";
const STATS: &str = "O:path:mycelium_a13_macos/swarm_stats.py";

pub fn quiescent(value: &Value) -> bool {
    // Close persists `closed`; _settle_recovered returns it unchanged.
    value["acquisition"]["downloaded_bytes"] == 0
        && value["acquisition"]["total_bytes"] == 0
        && value["acquisition"]["error_code"].is_null()
        && matches!(
            value["acquisition"]["state"].as_str(),
            Some("idle" | "closed")
        )
        && value["serving"]["route_ready"] == false
        && value["network"]["state"] == "private"
        && (value["swarm"].is_null() || value["swarm"]["enabled"] == false)
}

pub struct Run {
    pub observations: Vec<Value>,
    pub launches: Vec<Value>,
}
impl Default for Run {
    fn default() -> Self {
        Self::new()
    }
}
impl Run {
    pub fn new() -> Self {
        Self {
            observations: Vec::new(),
            launches: Vec::new(),
        }
    }
    pub(crate) fn observe(
        &mut self,
        id: &str,
        obligation: &str,
        request: (&str, &str),
        response: Response,
        code: u16,
        expected: Value,
    ) -> Result<Value> {
        let (method, path) = request;
        require(
            response.status == code,
            &format!("journey:{id}:http_status:{}_not_{code}", response.status),
        )?;
        let value = response.json()?;
        let keys = expected
            .as_object()
            .ok_or("journey:expected_map")?
            .keys()
            .map(String::as_str)
            .collect::<Vec<_>>();
        let actual = project(&value, &keys)?;
        require(
            actual == expected,
            &format!("journey:{id}:fields_mismatch:observed={actual}"),
        )?;
        self.observations.push(json!({"id":id,"obligation_id":obligation,"method":method,"path":path,"status":response.status,"fields":actual}));
        Ok(value)
    }
    pub(crate) fn status(
        &mut self,
        app: &mut App,
        id: &str,
        obligation: &str,
        expected: Value,
    ) -> Result<Value> {
        let response = app.api()?.get("/api/status")?;
        let value = response.json()?;
        // No acquisition/model serving is ever permitted in this corpus.
        if !quiescent(&value) {
            let diagnostic = project(
                &value,
                &[
                    "/acquisition/state",
                    "/acquisition/downloaded_bytes",
                    "/acquisition/total_bytes",
                    "/serving/route_ready",
                    "/network/state",
                ],
            )?;
            app.receipt["safety_refusal_observation"] = diagnostic;
            app.abort_forbidden()?;
            return Err("isolation:forbidden_acquisition_or_serving".into());
        }
        app.check_processes()?;
        self.observe(
            id,
            obligation,
            ("GET", "/api/status"),
            response,
            200,
            expected,
        )
    }
    pub(crate) fn fact(&mut self, id: &str, obligation: &str, fields: Value) {
        self.observations.push(json!({"id":id,"obligation_id":obligation,"method":"ASSERT","path":"public-observation-relation","status":200,"fields":fields}));
    }
    pub(crate) fn finish(&mut self, app: &mut App, action: Result<()>) -> Result<()> {
        let cleanup = app.stop();
        self.launches.push(app.receipt.clone());
        match (action, cleanup) {
            (Ok(()), Ok(())) => Ok(()),
            (Err(error), Ok(())) | (Ok(()), Err(error)) => Err(error),
            (Err(error), Err(cleanup)) => Err(format!("{error};cleanup={cleanup}")),
        }
    }
}
pub(crate) fn consent(storage: u64) -> Value {
    // Exact browser envelope: web/app.js:297-299 @ f5bfb709. No member grant fabricated.
    json!({"consent":{"peer_visibility":true,"power":"ac_only","storage_bytes":storage,"network":"unmetered_only","background":false,"diagnostics":false,"thermal":"stop_on_serious"},"adaptive_sharing":true})
}
pub(crate) fn no_standing(app: &App) -> Result<()> {
    require(
        !app.scratch.state.join("standing-consent.json").exists(),
        "consent:owner_has_standing_grant",
    )
}
fn saved_policy(app: &App, storage: u64) -> Result<()> {
    let value: Value = serde_json::from_slice(
        &fs::read(app.scratch.state.join("resource-policy.json"))
            .map_err(|e| io("choice:read", e))?,
    )
    .map_err(|_| "choice:invalid_json")?;
    require(
        value == consent(storage)["consent"],
        "choice:persisted_policy_changed",
    )
}
fn storage_default(html: &str) -> Result<u64> {
    let (_, rest) = html
        .split_once("id=\"storage\"")
        .ok_or("ui:storage_select")?;
    let (select, _) = rest
        .split_once("</select>")
        .ok_or("ui:storage_select_end")?;
    for option in select.split("<option ").skip(1) {
        let (attributes, _) = option.split_once('>').ok_or("ui:option_end")?;
        if attributes.contains(" selected") {
            let (_, value) = attributes.split_once("value=\"").ok_or("ui:option_value")?;
            return value
                .split('"')
                .next()
                .ok_or("ui:option_value")?
                .parse()
                .map_err(|_| "ui:storage_not_integer".into());
        }
    }
    Err("ui:no_selected_storage".into())
}

pub fn execute(target: &Target, real_home: &Path, run: &mut Run) -> Result<()> {
    // A genuine unjoined target supplies a real signed request. It never contacts an owner.
    let mut member = App::launch(target, real_home, None)?;
    let mut signed_request = Value::Null;
    let action = (|| {
        run.status(&mut member,"first_run",APP,json!({"/installation_role":"owner","/resource_policy":null,"/authority/swarm_created":false,"/authority/membership_state":"absent","/fast_mode/enabled":false,"/fast_mode/source":"default","/fast_mode/locked":false,"/run_model/selected":"a9-route","/run_model/source":"demo_default","/run_model/locked":false,"/network/state":"private","/swarm/enabled":false,"/serving/route_ready":false}))?;
        let page = member.api()?.get("/")?;
        require(page.status == 200, "ui:status")?;
        let html = page.text()?;
        let actual = storage_default(html)?;
        require(actual == 4_000_000_000, "ui:fresh_storage_default")?;
        require(
            html.contains("<option value=\"600\">10 minutes</option>"),
            "ui:member_10min_option",
        )?;
        require(
            html.contains("id=\"create-with-relay\">"),
            "ui:relay_default_not_unchecked",
        )?;
        run.fact("onboarding_default_fields",UI,json!({"fresh_storage_bytes":actual,"ten_minute_option_present":true,"relay_unchecked":true}));
        let response = member
            .api()?
            .post("/api/join/request", &consent(2_000_000_000))?;
        require(response.status == 200, "saved2:join_request_status")?;
        signed_request = response.json()?;
        require(
            signed_request["signature"].is_object(),
            "pair:missing_signature",
        )?;
        run.status(&mut member,"saved2_unjoined_target",CONSENT,json!({"/installation_role":"target","/resource_policy/storage_bytes":2_000_000_000u64,"/join_flow/standing_consent":null}))?;
        saved_policy(&member, 2_000_000_000)?;
        let response = member
            .api()?
            .post("/api/join/request", &consent(4_000_000_000))?;
        run.observe(
            "saved2_no_silent_upgrade",
            APP,
            ("POST", "/api/join/request"),
            response,
            400,
            json!({"/error":"consent_retry_changed"}),
        )?;
        Ok(())
    })();
    run.finish(&mut member, action)?;
    let mut member_restart = App::launch(target, real_home, Some(&member.scratch))?;
    let action = (|| {
        run.status(&mut member_restart,"saved2_restart_preserved",CONSENT,json!({"/acquisition/state":"closed","/installation_role":"target","/resource_policy/storage_bytes":2_000_000_000u64,"/join_flow/standing_consent":null}))?;
        saved_policy(&member_restart, 2_000_000_000)?;
        // Reissued/retrieved via public surface only, never from private pairing files.
        let response = member_restart
            .api()?
            .post("/api/pair/request", &json!({}))?;
        require(response.status == 200, "pair:request_status")?;
        signed_request = response.json()?;
        Ok(())
    })();
    run.finish(&mut member_restart, action)?;
    member.scratch.remove()?;
    member_restart.scratch.remove()?;

    let mut owner = App::launch(target, real_home, None)?;
    let action = (|| {
        let api = owner.api()?;
        let response = api.request(
            "GET",
            "/api/status",
            &[],
            &[],
            false,
            budgets::BudgetId::ModelSelection,
        )?;
        run.observe(
            "missing_cookie_refused",
            APP,
            ("GET", "/api/status"),
            response,
            401,
            json!({"/error":"open_from_native_launcher"}),
        )?;
        let response = api.request(
            "POST",
            "/api/create",
            b"{}",
            &[
                ("Content-Type", "application/json"),
                ("X-Mycelium-Action", "1"),
                ("Origin", "http://127.0.0.1:1"),
            ],
            true,
            budgets::BudgetId::ModelSelection,
        )?;
        run.observe(
            "wrong_origin_refused",
            APP,
            ("POST", "/api/create"),
            response,
            403,
            json!({"/error":"origin_rejected"}),
        )?;
        let response = api.post("/api/create", &json!({}))?;
        run.observe(
            "missing_consent_refused",
            APP,
            ("POST", "/api/create"),
            response,
            400,
            json!({"/error":"consent_required"}),
        )?;
        let response = api.post("/api/pair/preview", &json!({"request":signed_request}))?;
        let verified = response.json()?;
        require(
            verified["node_id"] == signed_request["node_id"]
                && verified["identity_key_digest"] == signed_request["identity_key_digest"],
            "pair:verified_identity_changed",
        )?;
        run.observe(
            "valid_signed_request_preview",
            PAIR,
            ("POST", "/api/pair/preview"),
            response,
            200,
            json!({"/peer_class":"mac_mlx_iroh"}),
        )?;
        let mut tampered = signed_request.clone();
        tampered["node_id"] = json!(format!(
            "{}-tampered",
            signed_request["node_id"].as_str().ok_or("pair:node_id")?
        ));
        let response = api.post("/api/pair/preview", &json!({"request":tampered}))?;
        run.observe(
            "tampered_signed_request_refused",
            PAIR,
            ("POST", "/api/pair/preview"),
            response,
            409,
            json!({"/error":"pairing_request_signature_invalid"}),
        )?;
        let response = api.post("/api/create", &consent(4_000_000_000))?;
        require(response.status == 200, "create:http_status")?;
        run.status(&mut owner,"fresh4_owner_created",APP,json!({"/installation_role":"owner","/authority/swarm_created":true,"/authority/membership_state":"current","/authority/member_count":1,"/authority/contribution_enabled":false,"/authority/local_tls":"pinned_loopback","/resource_policy/storage_bytes":4_000_000_000u64,"/network/state":"private"}))?;
        no_standing(&owner)?;
        saved_policy(&owner, 4_000_000_000)?;
        run.fact(
            "owner_no_standing_member_grant",
            CONSENT,
            json!({"standing_grant_exists":false}),
        );
        let response = owner.api()?.get("/api/stats")?;
        run.observe(
            "stats_not_exposed_on_private_app",
            STATS,
            ("GET", "/api/stats"),
            response,
            404,
            json!({"/error":"not_found"}),
        )?;
        let response = owner
            .api()?
            .post("/api/run-model", &json!({"choice":"qwen3-0.6b"}))?;
        run.observe(
            "choose_model_without_download",
            MODEL,
            ("POST", "/api/run-model"),
            response,
            200,
            json!({"/selected":"qwen3-0.6b","/source":"saved","/locked":false}),
        )?;
        let response = owner
            .api()?
            .post("/api/fast-mode", &json!({"enabled":true}))?;
        run.observe(
            "fast_mode_saved",
            MODE,
            ("POST", "/api/fast-mode"),
            response,
            200,
            json!({"/enabled":true,"/source":"saved","/locked":false}),
        )?;
        run.status(&mut owner,"model_selection_idle",MODEL,json!({"/acquisition/state":"idle","/acquisition/downloaded_bytes":0,"/serving/route_ready":false}))?;
        Ok(())
    })();
    run.finish(&mut owner, action)?;
    let mut fast = App::launch(target, real_home, Some(&owner.scratch))?;
    let action = (|| {
        run.status(&mut fast,"fast_restart_preserved",MODE,json!({"/fast_mode/enabled":true,"/fast_mode/source":"saved","/run_model/selected":"qwen3-0.6b","/run_model/source":"saved","/resource_policy/storage_bytes":4_000_000_000u64,"/authority/swarm_created":true,"/authority/membership_state":"current"}))?;
        no_standing(&fast)?;
        let response = fast
            .api()?
            .post("/api/fast-mode", &json!({"enabled":false}))?;
        run.observe(
            "normal_mode_saved",
            MODE,
            ("POST", "/api/fast-mode"),
            response,
            200,
            json!({"/enabled":false,"/source":"saved","/locked":false}),
        )?;
        Ok(())
    })();
    run.finish(&mut fast, action)?;
    let mut normal = App::launch(target, real_home, Some(&fast.scratch))?;
    let action = (|| {
        run.status(&mut normal,"normal_restart_preserved",MODE,json!({"/fast_mode/enabled":false,"/fast_mode/source":"saved","/fast_mode/locked":false,"/run_model/selected":"qwen3-0.6b","/resource_policy/storage_bytes":4_000_000_000u64}))?;
        no_standing(&normal)?;
        Ok(())
    })();
    run.finish(&mut normal, action)?;
    owner.scratch.remove()?;
    fast.scratch.remove()?;
    normal.scratch.remove()?;
    run.fact("clean_teardown", "O:path:mycelium_a13_macos/__main__.py", json!({"all_exit_zero":true,"leftover_pids":0,"stale_control_files":0,"scratch_removed":true,"no_serving_or_download":true}));
    Ok(())
}
pub fn run_record(target: &Target, real_home: &Path) -> Value {
    let started = Instant::now();
    let mut run = Run::new();
    let result = execute(target, real_home, &mut run);
    json!({"schema":"mycelium.journeys.v1","implementation":target.implementation,"source_sha":target.source_sha,"executable_sha256":target.executable_sha256,"elapsed_ms":started.elapsed().as_millis(),"result":if result.is_ok(){"PASS"}else{"FAIL"},"error":result.err(),"observations":run.observations,"launches":run.launches,"exclusions":[{"obligation_id":CONSENT,"journey":"joined_member_600s_standing_grant_and_two_node_join","result":"NOT_RUN","reason":"Production join-window transport requires public HTTPS relay; private constructor seam forbidden."},{"obligation_id":CONSENT,"journey":"cross_version_saved2_upgrade","result":"NOT_RUN","reason":"Only one executable version authorized. Same-byte restart preserves 2 GB and rejects a silent change; not an installer or cross-version upgrade."},{"obligation_id":STATS,"journey":"published_stats_shape","result":"NOT_RUN","reason":"Stats POST belongs to external directory publication, not private /api/status listener. Publication forbidden."},{"obligation_id":"O:path:mycelium_a13_macos/chat.py","journey":"product_sse_serve_chat_cancel","result":"NOT_RUN","reason":"Requires serving/model acquisition; forbidden in this slice. SSE client has real loopback adapter tests, not product stream evidence."},{"obligation_id":"O:path:mycelium_a13_macos/demo_chain.py","journey":"plan_stage_reservations_worker_deadlines_rollover","result":"NOT_RUN","reason":"Requires joined members/model preparation. No F14/F15/F15b full regression or historical RED claim."}]})
}
