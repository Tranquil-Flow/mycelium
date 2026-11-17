// SPDX-License-Identifier: AGPL-3.0-only
//! Lane XB (ruling 29 A+B), patch 2 (decisions D1-D4 of `plans/briefs/XB-p2-2026-11-17.md`):
//! where a run's work endpoints reach the other Mac.
//!
//! - D1 **The owner chooses, the members follow.** The OWNER picks, in its window, how its next
//!   run reaches the joined Macs: `same_network` (the default: exactly the direct-LAN carrier) or
//!   `relay` (Macs on different networks, through Mycelium's relay). The choice is read once, when
//!   the run starts ([`State::begin_run`]), kept for the whole run, and published SIGNED in the run
//!   announcement (`api::run_exchange::work_network`). A joined Mac binds in exactly the mode the
//!   owner signed ([`follow`]); nothing on the joined Mac decides it.
//! - D2 **Portmapping is a per-Mac preference** (default off), each Mac's own, applied only to
//!   that Mac's endpoints in a `relay` run: UPnP / PCP / NAT-PMP; its SSDP multicast can raise a
//!   firewall prompt on macOS.
//! - D3 **Fail closed, named.** A relay run whose relay this Mac cannot reach is refused
//!   `relay_unreachable` (the owner before dialing, a joined Mac before announcing ready); a signed
//!   mode this Mac cannot bind is a named refusal ([`RELAY_MISMATCH`], [`UNAVAILABLE`],
//!   [`INVALID`]), never a timeout.
//! - D4 **Only our relay:** a signed relay that is not this Mac's configured relay is refused.
//!
//! Preferences: `<network root>/network-reach.json`, a private (0600) canonical JSON object
//! `{"portmapping":false,"protocol":"mycelium.a13_network_reach.v1","reach":"relay"}` written only
//! through [`save`] (atomic; the window's `/api/network/reach` action). `reach` is read only by an
//! OWNER run's start; `portmapping` by every endpoint start of this Mac. Missing = defaults; an
//! invalid file is a named error (`network_reach_invalid`) in status, refuses an owner run by name
//! and refuses a joined Mac's relay run by name; saving from the window replaces it.
use super::{NetworkChoice, Resolved, WorkNetwork};
use api::{
    preferences::private_state,
    run_exchange::{
        MEMBER_REPORT,
        work_network::{RELAY_MISMATCH, RunReach},
    },
};
use runtime::iroh_host::{LanPolicy, MYCELIUM_RELAY_URL, RELAY_UNREACHABLE, Reach};
use serde_json::{Map, Value, json};
use std::{path::Path, sync::Mutex};

pub const FILE: &str = "network-reach.json";
pub const PROTOCOL: &str = "mycelium.a13_network_reach.v1";
pub const INVALID: &str = "network_reach_invalid";
/// A relay run on a Mac whose work transport is fixed to its own loopback.
pub const UNAVAILABLE: &str = "work_network_unavailable";
/// A change of the owner's choice while its run is active.
pub const RUN_ACTIVE: &str = "network_reach_run_active";
/// The window's action path.
pub const ACTION: &str = "/api/network/reach";

/// The stored preferences. `Default` = same network, no portmapping.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct ReachChoice {
    /// The OWNER's choice for its next run (`relay`); a joined Mac never reads it.
    pub across_networks: bool,
    /// This Mac's portmapping preference (D2).
    pub portmapping: bool,
}
impl ReachChoice {
    /// `same_network` / `relay`, as status names it.
    pub fn name(self) -> &'static str {
        if self.across_networks {
            "relay"
        } else {
            "same_network"
        }
    }
    pub fn document(self) -> Value {
        json!({"protocol": PROTOCOL, "reach": self.name(), "portmapping": self.portmapping})
    }
}
fn limit() -> Result<u64, &'static str> {
    private_state::private_json_limit().map_err(|e| e.0)
}
/// The stored preferences; a missing file is the default.
pub fn load(root: &Path) -> Result<ReachChoice, &'static str> {
    let Some(map) = private_state::load(&root.join(FILE), limit()?).map_err(|_| INVALID)? else {
        return Ok(ReachChoice::default());
    };
    if map.len() != 3 || map.get("protocol").and_then(Value::as_str) != Some(PROTOCOL) {
        return Err(INVALID);
    }
    let portmapping = map
        .get("portmapping")
        .and_then(Value::as_bool)
        .ok_or(INVALID)?;
    match map.get("reach").and_then(Value::as_str) {
        Some("same_network") => Ok(ReachChoice {
            across_networks: false,
            portmapping,
        }),
        Some("relay") => Ok(ReachChoice {
            across_networks: true,
            portmapping,
        }),
        _ => Err(INVALID),
    }
}
/// Store the preferences (atomic, private); the next start reads them.
pub fn save(root: &Path, choice: ReachChoice) -> Result<(), &'static str> {
    private_state::store(&root.join(FILE), &choice.document(), limit()?).map_err(|e| e.0)
}
/// D1 / D4: the network a run binds when its owner signed `signed`. The same network: the
/// configured network exactly as it is (today's carrier). Across networks: this Mac's own
/// reading under the cross-network policy at `relay`, THIS Mac's configured relay, which the
/// signed one must equal ([`RELAY_MISMATCH`] otherwise: a URL is never trusted because it is
/// signed), with this Mac's own portmapping preference (D2).
pub fn follow(
    configured: &WorkNetwork,
    relay: &Reach,
    signed: &RunReach,
    portmapping: bool,
) -> Result<WorkNetwork, &'static str> {
    let RunReach::Relay(url) = signed else {
        return Ok(configured.clone());
    };
    if relay.relay_url().ok().as_ref() != Some(url) {
        return Err(RELAY_MISMATCH);
    }
    let across = |policy: &LanPolicy| {
        policy
            .clone()
            .across_networks(url.clone())
            .with_portmapping(portmapping)
    };
    match configured {
        WorkNetwork::ThisMac => Ok(WorkNetwork::Across {
            reach: relay.clone(),
            portmapping,
        }),
        WorkNetwork::Read(reader, policy) => Ok(WorkNetwork::Read(reader.clone(), across(policy))),
        WorkNetwork::Fixed(NetworkChoice(Some((ip, policy)))) => Ok(WorkNetwork::Fixed(
            NetworkChoice(Some((*ip, across(policy)))),
        )),
        WorkNetwork::Fixed(NetworkChoice(None)) => Err(UNAVAILABLE),
        WorkNetwork::Across { .. } => Ok(WorkNetwork::Across {
            reach: relay.clone(),
            portmapping,
        }),
    }
}
/// Lane XB patch 3 (REVIEW XB2-02): what a joined Mac does with its work service when an
/// announcement arrives.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Next {
    /// The service stays: it is bound in exactly the reach this announcement signs.
    Reuse,
    /// No service fits (none yet, or one bound in another reach): close it, bind anew.
    Rebind,
}
/// The decision for one announcement: `bound` the reach of this Mac's current service, `signed`
/// what the announcement signs (`RunReach::from_announcement`), `relay` this Mac's relay.
pub fn next_service(
    bound: Option<&RunReach>,
    signed: Result<RunReach, &'static str>,
    relay: &Reach,
) -> Result<Next, &'static str> {
    let signed = signed?;
    if let RunReach::Relay(url) = &signed
        && relay.relay_url().ok().as_ref() != Some(url)
    {
        return Err(RELAY_MISMATCH);
    }
    match bound {
        Some(bound) if *bound == signed => Ok(Next::Reuse),
        _ => Ok(Next::Rebind),
    }
}
/// Lane XB patch 3 (REVIEW XB2-01): after resolution, a run signed across networks must have
/// resolved to exactly that mode at the signed relay; anything else (no usable interface, an
/// ambiguous or VPN selection, an unreadable one: a loopback-only result) is the named refusal
/// [`UNAVAILABLE`], never a loopback-only service. A same-network run keeps its deliberate
/// local-only behaviour.
pub fn bound_as_signed(signed: &RunReach, choice: &NetworkChoice) -> Result<(), &'static str> {
    match signed {
        RunReach::SameNetwork => Ok(()),
        RunReach::Relay(url) => match &choice.0 {
            Some((_, policy)) if policy.relay() == Some(url) => Ok(()),
            _ => Err(UNAVAILABLE),
        },
    }
}
/// The owner's choice for the run starting now, in its signed form (its relay: this Mac's).
pub fn owner_choice(root: &Path, relay: &Reach) -> Result<RunReach, &'static str> {
    if load(root)?.across_networks {
        Ok(RunReach::Relay(relay.relay_url().map_err(|_| INVALID)?))
    } else {
        Ok(RunReach::SameNetwork)
    }
}
/// D2: this Mac's portmapping preference for a relay run starting now.
pub fn portmapping(root: &Path) -> Result<bool, &'static str> {
    load(root).map(|choice| choice.portmapping)
}
/// Lane XB patch 3 (REVIEW XB2-04): what status says of a requested portmapping. iroh 1.0.2
/// exposes a granted mapping only through its `metrics` feature, which this build does not
/// enable: the request is known, its outcome is not.
pub const PORTMAPPING_OUTCOME: &str = "requested, outcome not observable in this build";
/// `{reach, relay_url, portmapping, portmapping_outcome}` of a bound choice (the ACTIVE
/// endpoint's, for status): `portmapping` is whether this Mac's endpoints REQUESTED a mapping.
pub fn active_document(choice: &NetworkChoice) -> Value {
    match &choice.0 {
        Some((_, policy)) => {
            let requested = policy.relay().is_some() && policy.portmapping();
            json!({
                "reach": policy.reach(),
                "relay_url": policy.relay().map(ToString::to_string),
                "portmapping": requested,
                "portmapping_outcome": requested.then_some(PORTMAPPING_OUTCOME),
            })
        }
        None => json!({"reach": "local_only", "relay_url": null, "portmapping": false,
            "portmapping_outcome": null}),
    }
}
/// Lane XB patch 3 (REVIEW XB2-04): every live connection of this Mac to another Mac, sampled
/// now (`runtime::iroh_paths`): `{remote, direction, reach, path}`, `path` = `relay` / `direct` /
/// `none` as iroh selected it at this moment.
pub fn live_paths() -> Value {
    runtime::iroh_paths::snapshot()
        .into_iter()
        .map(|sample| {
            json!({"remote": sample.remote, "direction": sample.direction,
                "reach": sample.reach, "path": sample.path})
        })
        .collect()
}
/// A named refusal of a joined Mac's work start, kept for the run it refused.
#[derive(Clone, Debug)]
struct Refusal {
    code: String,
    announcement: Value,
}
/// This Mac's reach state: its configured relay, the owner run's choice, the active endpoint's
/// mode and a joined Mac's last named refusal.
pub struct State {
    relay: Reach,
    /// Owner: the run's choice and this Mac's portmapping, both read once at the run's start.
    run: Mutex<Option<Result<(RunReach, bool), &'static str>>>,
    /// Joined Mac: the run whose work it first started, with its portmapping then (XB2-05).
    member_run: Mutex<Option<(String, bool)>>,
    active: Mutex<Option<Value>>,
    refusal: Mutex<Option<Refusal>>,
    /// Test-only (REVIEW XB2-06): the window action's next run waits here between its load and
    /// its save, so a test can overlap a second action with that window.
    #[cfg(test)]
    after_load: Mutex<Option<Box<dyn FnOnce() + Send>>>,
}
fn held<T>(mutex: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}
impl State {
    pub fn new(relay: Reach) -> Self {
        Self {
            relay,
            run: Mutex::new(None),
            member_run: Mutex::new(None),
            active: Mutex::new(None),
            refusal: Mutex::new(None),
            #[cfg(test)]
            after_load: Mutex::new(None),
        }
    }
    /// Test-only (REVIEW XB2-06): hold the window action's next run between load and save.
    #[cfg(test)]
    pub(crate) fn hold_after_load(&self, hold: Box<dyn FnOnce() + Send>) {
        *held(&self.after_load) = Some(hold);
    }
    /// The one relay this Mac's endpoints register at (Mycelium's unless a test overrides it).
    pub fn relay(&self) -> &Reach {
        &self.relay
    }
    /// Owner: the choice of the run starting now, read once and kept for the run, with this
    /// Mac's portmapping preference (REVIEW XB2-05: frozen with the run, like the choice; a
    /// change saved meanwhile applies to the next run).
    pub fn begin_run(&self, root: &Path) -> Result<RunReach, &'static str> {
        let choice = owner_choice(root, &self.relay)
            .and_then(|choice| portmapping(root).map(|portmapping| (choice, portmapping)));
        *held(&self.run) = Some(choice.clone());
        *held(&self.active) = None;
        choice.map(|(choice, _)| choice)
    }
    /// Owner: the active run's choice and its frozen portmapping; `None` without a run.
    pub fn run_binding(&self) -> Option<Result<(RunReach, bool), &'static str>> {
        held(&self.run).clone()
    }
    /// Joined Mac (REVIEW XB2-05): this Mac's portmapping preference for the run `run_id`,
    /// read at the run's first work start and kept for every retry of that run.
    pub fn member_portmapping(&self, run_id: &str, root: &Path) -> Result<bool, &'static str> {
        let mut frozen = held(&self.member_run);
        if let Some((run, on)) = frozen.as_ref()
            && run == run_id
        {
            return Ok(*on);
        }
        let on = portmapping(root)?;
        *frozen = Some((run_id.to_owned(), on));
        Ok(on)
    }
    /// Owner: the active run's choice; `None` without a run.
    pub fn run_choice(&self) -> Option<Result<RunReach, &'static str>> {
        held(&self.run)
            .clone()
            .map(|run| run.map(|(choice, _)| choice))
    }
    /// Owner: the run ended.
    pub fn end_run(&self) {
        *held(&self.run) = None;
        *held(&self.active) = None;
    }
    /// The endpoints of a run or a work service bound `choice` (status's `active`).
    pub fn activate(&self, choice: &NetworkChoice) {
        *held(&self.active) = Some(active_document(choice));
        *held(&self.refusal) = None;
    }
    pub fn deactivate(&self) {
        *held(&self.active) = None;
    }
    /// A joined Mac refused the run `announcement` names, by `code` (D3).
    pub fn refuse(&self, announcement: &Value, code: &str) {
        eprintln!("work_network_refused:{code}");
        *held(&self.refusal) = Some(Refusal {
            code: code.to_owned(),
            announcement: announcement.clone(),
        });
        *held(&self.active) = None;
    }
    pub fn clear_refusal(&self) {
        *held(&self.refusal) = None;
    }
    /// The joined Mac's report of its refusal, so the owner's run names it (state `error`,
    /// `detail` = the code) instead of waiting out a timeout.
    pub fn refusal_report(&self) -> Option<Value> {
        let refusal = held(&self.refusal).clone()?;
        let a = &refusal.announcement;
        let model = match &a["model"] {
            Value::Object(m) if m["model_id"].is_string() && m["resolved_commit"].is_string() => {
                json!({"model_id": m["model_id"], "resolved_commit": m["resolved_commit"]})
            }
            _ => Value::Null,
        };
        Some(json!({"protocol": MEMBER_REPORT, "run_id": a["run_id"],
            "deployment_id": a["deployment_id"], "state": "error", "detail": refusal.code,
            "model": model, "progress": null, "descriptor": null,
            "issued_at": api::app_host::now()}))
    }
    /// The joined Mac's own card shows the refusal (`demo_target`, state `error`).
    pub fn card(&self, card: &mut Value) {
        let Some(refusal) = held(&self.refusal).clone() else {
            return;
        };
        if let Some(fields) = card.as_object_mut() {
            fields.insert("state".into(), json!("error"));
            fields.insert("detail".into(), json!(refusal.code));
            fields.insert("run_id".into(), refusal.announcement["run_id"].clone());
        }
    }
    /// `status.network_reach`: the ACTIVE endpoints' mode (`null` when none is bound), the
    /// owner's choice for its next run, this Mac's portmapping preference, a named error and the
    /// live path of every connection to another Mac (`paths`, sampled now: XB2-04).
    pub fn status(&self, root: &Path) -> Value {
        let stored = load(root);
        let error = held(&self.refusal)
            .as_ref()
            .map(|r| Value::from(r.code.clone()))
            .or_else(|| stored.err().map(Value::from))
            .unwrap_or(Value::Null);
        json!({
            "active": held(&self.active).clone(),
            "next_run": stored.map(ReachChoice::name).unwrap_or("same_network"),
            "portmapping": stored.map(|c| c.portmapping).unwrap_or(false),
            "relay_url": self.relay.relay_url().ok().map(|u| u.to_string()),
            "error": error,
            "paths": live_paths(),
        })
    }
}
/// The window's `/api/network/reach` action: exactly `{"reach": "same_network" | "relay"}`
/// (owner only, refused while its run is active) or `{"portmapping": bool}` (any role). Stores
/// atomically, starting from the defaults when the stored file is invalid, and answers status.
pub fn action(
    root: &Path,
    state: &State,
    body: &Map<String, Value>,
    owner: bool,
) -> Result<Value, &'static str> {
    let mut stored = load(root).unwrap_or_default();
    #[cfg(test)]
    {
        // Taken first: the hook never runs while the slot's lock is held.
        let hold = held(&state.after_load).take();
        if let Some(hold) = hold {
            hold();
        }
    }
    match (body.len(), body.get("reach"), body.get("portmapping")) {
        (1, Some(reach), None) => {
            if !owner {
                return Err("owner_role_required");
            }
            if state.run_choice().is_some() {
                return Err(RUN_ACTIVE);
            }
            stored.across_networks = match reach.as_str() {
                Some("same_network") => false,
                Some("relay") => true,
                _ => return Err("invalid_request"),
            };
        }
        (1, None, Some(Value::Bool(on))) => stored.portmapping = *on,
        _ => return Err("invalid_request"),
    }
    save(root, stored)?;
    Ok(state.status(root))
}
/// Lane XB3 at a run's start: a `relay` run whose relay cannot be reached fails closed with
/// `relay_unreachable` before anything is dialed; a same-network run needs no relay.
pub fn require_relay(resolved: &Resolved) -> Result<(), &'static str> {
    match &resolved.choice.0 {
        Some((interface, policy)) if policy.relay().is_some() => {
            runtime::iroh_host::relay_reachable(*interface, policy)
        }
        _ => Ok(()),
    }
}
/// The plain message of a `relay_unreachable` refusal.
pub fn relay_refusal() -> (&'static str, String) {
    (
        RELAY_UNREACHABLE,
        format!(
            "This Mac cannot reach the Mycelium relay ({MYCELIUM_RELAY_URL}), which a run across \
             networks needs to find the other Mac. Check this Mac's internet connection and start \
             again, or put both Macs on the same network and choose \"Macs on the same network\"."
        ),
    )
}
/// D3: the named refusals a joined Mac reports when it cannot bind its run's signed reach.
pub const MEMBER_REFUSALS: [&str; 6] = [
    RELAY_UNREACHABLE,
    RELAY_MISMATCH,
    UNAVAILABLE,
    INVALID,
    api::run_exchange::work_network::INVALID,
    api::run_exchange::work_network::VERSION_UNSUPPORTED,
];
/// The owner's refusal of a run whose joined Mac `node` reported `code` (D3).
pub fn member_refusal(node: &str, code: &str) -> (&'static str, String) {
    let short = node.get(..12).unwrap_or(node);
    if code == RELAY_UNREACHABLE {
        return (
            "joined_mac_relay_unreachable",
            format!(
                "The joined Mac {short} cannot reach the Mycelium relay ({MYCELIUM_RELAY_URL}), \
                 which a run across networks needs. Check that Mac's internet connection, then \
                 press Start again."
            ),
        );
    }
    (
        "joined_mac_network_refused",
        format!(
            "The joined Mac {short} cannot work across networks as this run asks ({code}). \
             Update Mycelium on both Macs to the same version, or choose \"Macs on the same \
             network\" and put both Macs on one network, then press Start again."
        ),
    )
}
/// The plain message of an owner run refused at its start for its own reach (D1 / D3).
pub fn owner_refusal_message(code: &str) -> String {
    match code {
        INVALID => "This Mac's network setting could not be read. Choose \"Macs on the same \
                    network\" or \"Macs on different networks\" again in the window, then \
                    press Start again."
            .into(),
        _ => format!(
            "This Mac cannot work across networks ({code}). Choose \"Macs on the same \
             network\" and put both Macs on one network, then press Start again."
        ),
    }
}
/// Lane XB patch 2 (REVIEW XB-12): a relay run's dial that got no answer. Across networks
/// the joined Mac's advertised private address names nothing on this network: no LAN
/// permission probe, no same-network advice.
pub const SILENT_VIA_RELAY: &str = "joined_mac_did_not_answer_via_relay";
/// The plain message of [`SILENT_VIA_RELAY`].
pub fn silent_via_relay_message(cause: &str) -> String {
    format!(
        "The joined Mac did not answer through the Mycelium relay ({cause}). Keep Mycelium \
         open on that Mac, check its internet connection (its card names a relay problem if it \
         has one), then press Start again."
    )
}
/// The policy of a `relay` run's transport (Mycelium's relay unless a test overrides it).
pub(super) fn across_policy(reach: &Reach, portmapping: bool) -> Option<LanPolicy> {
    let relay = reach.relay_url().ok()?;
    Some(
        LanPolicy::private_only()
            .across_networks(relay)
            .with_portmapping(portmapping),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::IpAddr, os::unix::fs::PermissionsExt};

    fn root(tag: &str) -> std::path::PathBuf {
        let base = std::env::var_os("TMPDIR").map_or_else(std::env::temp_dir, Into::into);
        let dir = base.join(format!("xb-reach-{tag}-{}", std::process::id()));
        std::fs::remove_dir_all(&dir).ok();
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        dir
    }
    fn reading(ip: &str) -> api::lan_interface::Selection {
        api::lan_interface::Selection::Lan(ip.parse::<IpAddr>().unwrap())
    }
    fn mycelium() -> iroh::RelayUrl {
        MYCELIUM_RELAY_URL.parse().unwrap()
    }

    /// D1: a joined Mac follows the signed choice. The same network keeps the configured
    /// carrier exactly; across networks this Mac's own interface under the cross-network policy
    /// at ITS relay, with ITS portmapping preference; a foreign signed relay is refused by name
    /// (D4) and a loopback-fixed Mac cannot cross networks (named).
    #[test]
    fn a_joined_mac_binds_in_the_mode_the_owner_signed() {
        let same = follow(
            &WorkNetwork::ThisMac,
            &Reach::Shipped,
            &RunReach::SameNetwork,
            true,
        );
        assert_eq!(same, Ok(WorkNetwork::ThisMac));
        let across = follow(
            &WorkNetwork::ThisMac,
            &Reach::Shipped,
            &RunReach::Relay(mycelium()),
            false,
        )
        .unwrap();
        let resolved = across.resolution_with(|| reading("172.20.10.4"));
        assert_eq!(resolved.transport, "relay:172.20.10.4");
        assert_eq!(resolved.reach, "relay");
        let Some((_, policy)) = &resolved.choice.0 else {
            panic!("{resolved:?}")
        };
        assert_eq!(policy.relay(), Some(&mycelium()));
        assert!(!policy.portmapping());
        assert!(resolved.choice.allows("203.0.113.9:4433".parse().unwrap()));
        let mapped = follow(
            &WorkNetwork::ThisMac,
            &Reach::Shipped,
            &RunReach::Relay(mycelium()),
            true,
        )
        .unwrap()
        .resolution_with(|| reading("10.0.0.7"));
        assert!(
            mapped
                .choice
                .0
                .as_ref()
                .is_some_and(|(_, p)| p.portmapping())
        );
        let foreign: iroh::RelayUrl = "https://relay.example.org".parse().unwrap();
        assert_eq!(
            follow(
                &WorkNetwork::ThisMac,
                &Reach::Shipped,
                &RunReach::Relay(foreign),
                false
            ),
            Err(RELAY_MISMATCH)
        );
        assert_eq!(
            follow(
                &WorkNetwork::loopback(),
                &Reach::Shipped,
                &RunReach::Relay(mycelium()),
                false
            ),
            Err(UNAVAILABLE)
        );
        // No private network on this Mac: still no run across networks on loopback.
        let none = across.resolution_with(|| {
            api::lan_interface::Selection::LocalOnly(api::lan_interface::NO_PRIVATE_ADDRESS)
        });
        assert_eq!(none.choice.0, None);
        assert_eq!(none.reach, "local_only");
    }

    /// Lane XB patch 3 (REVIEW XB2-02): EVERY announcement's signed reach is validated in full
    /// before a work service is reused, in both directions: a LAN service meeting a malformed or
    /// unsupported field, and a relay service meeting a foreign relay, are named refusals, never
    /// a silent reuse; a valid change of mode rebinds; only an identical binding is reused.
    #[test]
    fn every_announcement_is_validated_before_a_work_service_is_reused() {
        let same = RunReach::SameNetwork;
        let ours = RunReach::Relay(mycelium());
        let foreign = RunReach::Relay("https://relay.example.org".parse().unwrap());
        let shipped = Reach::Shipped;
        let key = api::run_exchange::work_network::FIELD;
        let field = |value: Value| RunReach::from_value(&json!({ key: value }));
        let malformed = field(json!({"protocol": api::run_exchange::work_network::PROTOCOL,
            "reach": "relay"}));
        let unsupported = field(json!({"protocol": "mycelium.a13_work_network.v2",
            "reach": "relay", "relay_url": "https://relay.mycelium.now/"}));
        assert_eq!(malformed, Err(api::run_exchange::work_network::INVALID));
        assert_eq!(
            unsupported,
            Err(api::run_exchange::work_network::VERSION_UNSUPPORTED)
        );
        // Only an identical binding is reused.
        assert_eq!(
            next_service(Some(&same), Ok(same.clone()), &shipped),
            Ok(Next::Reuse)
        );
        assert_eq!(
            next_service(Some(&ours), Ok(ours.clone()), &shipped),
            Ok(Next::Reuse)
        );
        // A valid change of mode (or no service yet) binds anew.
        for (bound, signed) in [
            (Some(&same), ours.clone()),
            (Some(&ours), same.clone()),
            (None, ours.clone()),
            (None, same.clone()),
        ] {
            assert_eq!(
                next_service(bound, Ok(signed.clone()), &shipped),
                Ok(Next::Rebind),
                "{bound:?} -> {signed:?}"
            );
        }
        // LAN service -> malformed / unsupported field: named, never an update.
        for bound in [Some(&same), Some(&ours), None] {
            assert_eq!(
                next_service(bound, malformed.clone(), &shipped),
                Err(api::run_exchange::work_network::INVALID),
                "{bound:?}"
            );
            assert_eq!(
                next_service(bound, unsupported.clone(), &shipped),
                Err(api::run_exchange::work_network::VERSION_UNSUPPORTED),
                "{bound:?}"
            );
        }
        // Relay service -> a foreign relay: named (D4), never reused on the old relay.
        for bound in [Some(&ours), Some(&same), None] {
            assert_eq!(
                next_service(bound, Ok(foreign.clone()), &shipped),
                Err(RELAY_MISMATCH),
                "{bound:?}"
            );
        }
    }

    /// Lane XB patch 3 (REVIEW XB2-05): a joined Mac's portmapping preference is frozen with the
    /// run it first started work for: a change saved during that run waits for the next run.
    #[test]
    fn a_joined_macs_portmapping_is_frozen_for_its_run() {
        let dir = root("member-pm");
        let state = State::new(Reach::Shipped);
        assert_eq!(state.member_portmapping("run-1", &dir), Ok(false));
        save(
            &dir,
            ReachChoice {
                across_networks: false,
                portmapping: true,
            },
        )
        .unwrap();
        assert_eq!(
            state.member_portmapping("run-1", &dir),
            Ok(false),
            "the running run keeps its value"
        );
        assert_eq!(
            state.member_portmapping("run-2", &dir),
            Ok(true),
            "the next run reads it"
        );
        std::fs::remove_dir_all(&dir).ok();
    }

    /// Lane XB patch 3 (REVIEW XB2-04): status carries LIVE path evidence, sampled when it is
    /// read, for every connection to another Mac (`paths`: remote, direction, the run's reach, and
    /// the path iroh selected now): here a relay-only test double (`relay`; the probe and the
    /// hotspot run show a real one) and a real same-network connection on loopback, the LAN double
    /// (`direct`). A connection that ends leaves the list. Portmapping is named honestly: requested,
    /// its outcome not observable in this build; status never claims a mapping.
    #[test]
    fn status_samples_each_live_peer_path_and_names_portmapping_honestly() {
        use runtime::iroh_host::LoopbackHost;
        use transport::Channel;
        let dir = root("paths");
        let state = State::new(Reach::Shipped);
        let paths = |state: &State| state.status(&dir)["paths"].clone();
        let has = |paths: &Value, remote: &str, path: &str| {
            paths.as_array().is_some_and(|all| {
                all.iter()
                    .any(|p| p["remote"] == remote && p["path"] == path)
            })
        };
        let relayed = runtime::iroh_paths::register(
            "relay-double".into(),
            "out",
            "relay",
            Box::new(|| Some("relay")),
        );
        let now = paths(&state);
        assert!(has(&now, "relay-double", "relay"), "{now}");
        assert!(
            now.as_array().unwrap().iter().any(|p| p
                == &json!({"remote": "relay-double", "direction": "out", "reach": "relay",
                    "path": "relay"})),
            "{now}"
        );
        drop(relayed);
        assert!(!has(&paths(&state), "relay-double", "relay"));
        // The LAN double: two direct-LAN endpoints on loopback under an explicit-peer policy.
        let rt = tokio::runtime::Builder::new_multi_thread()
            .worker_threads(2)
            .enable_all()
            .build()
            .unwrap();
        let clock = api::managed_carrier::HostClock::default();
        let loopback = IpAddr::from([127, 0, 0, 1]);
        let until = |clock: &api::managed_carrier::HostClock| {
            api::joined_member::work::until(clock, budgets::BudgetId::WorkerStartup).unwrap()
        };
        let bind = |key: &iroh::SecretKey| {
            LoopbackHost::bind_lan(
                &rt,
                &clock,
                until(&clock),
                key.clone(),
                &[Channel::Router],
                loopback,
                LanPolicy::with_explicit(vec![loopback]).unwrap(),
            )
            .unwrap()
        };
        let (member_key, owner_key) = (iroh::SecretKey::generate(), iroh::SecretKey::generate());
        let mut member = bind(&member_key);
        member.admit_peer_ids(&[owner_key.public()]).unwrap();
        let address = member.address().unwrap();
        let mut owner = bind(&owner_key);
        let short: String = member_key.public().to_string().chars().take(12).collect();
        std::thread::scope(|scope| {
            let accepting = scope.spawn(|| loop {
                match member.accept(&clock, until(&clock)) {
                    Err(transport::Error::Authentication) => continue,
                    other => return other.unwrap(),
                }
            });
            let dialled = owner.connect(&address, &clock, until(&clock)).unwrap();
            let inbound = accepting.join().unwrap();
            let began = std::time::Instant::now();
            while !has(&paths(&state), &short, "direct") && began.elapsed().as_secs() < 5 {
                std::thread::sleep(std::time::Duration::from_millis(50));
            }
            let now = paths(&state);
            assert!(has(&now, &short, "direct"), "{now}");
            dialled.close(0u32.into(), b"done");
            rt.block_on(inbound.closed());
        });
        assert!(!has(&paths(&state), &short, "direct"));
        owner.close(&clock, until(&clock)).unwrap();
        member.close(&clock, until(&clock)).unwrap();
        // Portmapping: requested, never claimed.
        let relay_run = NetworkChoice(Some((
            loopback,
            LanPolicy::private_only()
                .across_networks(mycelium())
                .with_portmapping(true),
        )));
        state.activate(&relay_run);
        let status = state.status(&dir);
        assert_eq!(
            status["active"]["portmapping_outcome"],
            "requested, outcome not observable in this build",
            "{status}"
        );
        assert!(!status.to_string().contains("portmapped"), "{status}");
        std::fs::remove_dir_all(&dir).ok();
    }

    /// The owner's stored choice, its signed form, the window's action and status; an invalid
    /// file is named (never silently the same network) and replaced by the next save.
    #[test]
    fn the_owners_choice_is_stored_signed_and_named_and_an_invalid_file_is_refused_by_name() {
        let dir = root("owner");
        let state = State::new(Reach::Shipped);
        assert_eq!(
            owner_choice(&dir, &Reach::Shipped),
            Ok(RunReach::SameNetwork)
        );
        let body = |v: Value| v.as_object().unwrap().clone();
        let status = action(&dir, &state, &body(json!({"reach": "relay"})), true).unwrap();
        assert_eq!(status["next_run"], "relay");
        assert_eq!(status["active"], Value::Null);
        assert_eq!(
            owner_choice(&dir, &Reach::Shipped),
            Ok(RunReach::Relay(mycelium()))
        );
        let status = action(&dir, &state, &body(json!({"portmapping": true})), false).unwrap();
        assert_eq!(
            (status["next_run"].as_str(), status["portmapping"].as_bool()),
            (Some("relay"), Some(true))
        );
        assert_eq!(
            action(&dir, &state, &body(json!({"reach": "relay"})), false),
            Err("owner_role_required")
        );
        for bad in [
            json!({}),
            json!({"reach": "lan"}),
            json!({"portmapping": 1}),
            json!({"reach": "relay", "portmapping": true}),
        ] {
            assert_eq!(
                action(&dir, &state, &body(bad), true),
                Err("invalid_request")
            );
        }
        assert_eq!(state.begin_run(&dir), Ok(RunReach::Relay(mycelium())));
        assert_eq!(
            action(&dir, &state, &body(json!({"reach": "same_network"})), true),
            Err(RUN_ACTIVE)
        );
        state.end_run();
        // A damaged file: named in status and at the next run's start; a save replaces it.
        std::fs::write(
            dir.join(FILE),
            b"{\"portmapping\":false,\"protocol\":\"x\",\"reach\":\"relay\"}\n",
        )
        .unwrap();
        std::fs::set_permissions(dir.join(FILE), std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(load(&dir), Err(INVALID));
        assert_eq!(state.status(&dir)["error"], INVALID);
        assert_eq!(state.begin_run(&dir), Err(INVALID));
        state.end_run();
        assert_eq!(portmapping(&dir), Err(INVALID));
        action(&dir, &state, &body(json!({"reach": "same_network"})), true).unwrap();
        assert_eq!(load(&dir), Ok(ReachChoice::default()));
        assert_eq!(state.status(&dir)["error"], Value::Null);
        std::fs::remove_dir_all(&dir).ok();
    }
}
