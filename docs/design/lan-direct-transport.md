# Design: `LanDirect` transport for the iroh sidecar

Status: **design only**. No Rust is changed in this lane; nothing here has been
built, compiled or run. Every statement about iroh 1.0.2 behaviour is read from
source and marked **UNVERIFIED** until the offline two-Mac test below passes.
The Python side (`docs/LOCAL-FIRST.md`) already carries the network-mode switch
and refuses to start a stage-runtime peer in `lan` mode until
`mycelium_platform_neutral.network_mode.SIDECAR_LAN_SUPPORTED` is flipped by the
change this document describes.

## Problem

The sidecar has exactly three network configurations
(`native/iroh_transport/src/sidecar.rs`, `bind_endpoint`, ~835-860):

| flag | builder | reach |
|---|---|---|
| none (default) | `presets::N0` | n0 relays + n0 pkarr publish + n0 DNS lookup |
| `--force-relay` | `presets::N0` + `clear_ip_transports()` | relay only, still n0 DNS |
| `--local-only` | `presets::Minimal` + `RelayMode::Disabled` + `clear_ip_transports()` + bind `127.0.0.1:0` | this machine only |

Two machines on one LAN with no internet cannot use any of them: the default needs
the n0 relay (and refuses to report ready without a relay address), and
`--local-only` binds loopback and rejects every non-loopback peer address.

## Goal

A fourth, explicit mode, `--lan`, selected by `--network-mode lan`, in which the
sidecar talks to peers **directly over IP on the local network** and contacts no
hosted service: no relay, no pkarr publish, no DNS lookup. Default behaviour
(`internet`) must stay byte-for-byte what the L2-sealed build does.

## Changes (all behind the new flag)

1. **CLI** (`src/bin/mycelium-iroh-sidecar.rs`, `CommonArguments`, ~168-175):
   add `--lan: bool`, `conflicts_with_all = ["local_only", "force_relay"]`.
   `SidecarConfig` gains `lan: bool`; `run_sidecar` extends the existing
   `local_only && force_relay` guard to reject any two of the three
   (`SidecarError::InvalidNetworkMode`).
2. **Endpoint** (`bind_endpoint`): a new branch

   ```rust
   Endpoint::builder(presets::Minimal)   // no pkarr publisher, no DNS lookup
       .relay_mode(RelayMode::Disabled)  // no relay, ever
   // keep the default IP transports: bind 0.0.0.0:0 (and [::]:0 if enabled)
   ```

   `presets::Minimal` is the preset `--local-only` already uses (R2 H2); it adds
   no address-lookup services. Not calling `clear_ip_transports()` keeps the
   default wildcard IP binds. An explicit interface bind
   (`--lan-bind <ip>`, optional, default wildcard) is possible with
   `bind_addr(SocketAddrV4::new(ip, 0))` and is the answer for multi-homed
   hosts. **UNVERIFIED:** that `Minimal` + `Disabled` + default IP transports
   yields a working direct connection in iroh 1.0.2.
3. **Ready address** (`run_sidecar` ~588-592; `ready_address.rs`
   `address_is_usable`): pass `requires_relay = false` for `lan` as it already is
   for `local_only`. Usability is then "at least one IP address", but an
   address set containing only loopback or unspecified addresses must **not**
   count as ready in `lan` mode (extend `address_is_usable` with an
   `allow_loopback_only: bool`, false for `lan`), otherwise a host with no usable
   interface reports ready and silently cannot be reached. Unit-testable as a
   pure function.
4. **`validate_peer`** (~1850-1857): today `local_only` requires every peer
   address to be loopback. For `lan`, require every address to be an IP
   address (no `TransportAddr::Relay`, no custom transport) that is
   private (RFC 1918 IPv4, IPv6 ULA `fc00::/7`, or link-local `fe80::/10` with
   scope) and not loopback/unspecified/multicast. A relay address from a peer is
   a protocol error in `lan` mode, not something to ignore, because honouring it
   would contact a hosted relay. Pure function; extend the existing validation
   tests.
5. **Static peer addresses.** `ConfigurePeerPayload.endpoint_addr` already
   carries the full `EndpointAddr` (id + IP addrs), and connecting to an
   `EndpointAddr` that carries direct addresses needs no address lookup.
   Therefore the first milestone needs **no discovery service**: peers learn each
   other's IP addresses from the membership exchange (below). If a later
   multi-hop path needs lookup by id alone, register a
   `MemoryLookup` (`iroh-1.0.2/src/address_lookup/memory.rs`, available in the
   pinned crate set) and feed it from the same payload. **UNVERIFIED:** exact
   builder method for attaching it in 1.0.2.
6. **No mDNS in this pass.** mDNS lives in the separate
   `iroh-mdns-address-lookup` crate, which is not in `Cargo.lock`; adding it needs
   a network fetch and a re-qualification. It is a follow-up, not a prerequisite.
7. **Portmapper.** iroh's default `portmapper` feature probes the local router
   for UPnP/NAT-PMP/PCP. That is local traffic, not a hosted service, but a
   LAN-only build may disable it. Not required for the first milestone.

## Membership exchange (Python side, already mostly present)

The node advertises `endpoint_addrs` to the seed on join/resume
(`mycelium_node/__main__.py`); the seed returns the member table to peers. In
`lan` mode the sidecar's live `EndpointAddr` (IP addrs only) must be the value
that reaches the seed and then each peer's `ConfigurePeerPayload`. Two checks
to add with the Rust change: the node must publish the sidecar's *ready*
addresses rather than the `--advertise` URL for transport purposes (the URL is
membership/control metadata), and the seed must not rewrite or drop private
addresses (today's internet path may filter; **UNVERIFIED**, read
`mycelium_seed` before relying on it).

## Python flags already in place (this lane)

`--network-mode lan` on `mycelium_node` -> `build_physical_node_command(...,
sidecar_lan=True)` -> `physical_inference_node.py --sidecar-lan` ->
`NativeSidecarProcess(lan=True)` -> sidecar argv `--lan`. Until the Rust flag
exists the chain deliberately stops at node preflight
(`SIDECAR_LAN_SUPPORTED = False`). Not yet forwarded: `mycelium_live/route.py`
(only `--sidecar-force-relay` is) and the mobile core
(`native/mobile_core/src/transport.rs` `Reachability`).

## Tests

Rust unit (run only after the physical gate releases; `cargo check` first):

- CLI: `--lan` conflicts with `--local-only` and `--force-relay`; absent flag
  leaves the config byte-identical to today's.
- `address_is_usable(addr, requires_relay=false, allow_loopback_only=false)`:
  empty -> false, loopback-only -> false, one private IP -> true.
- `validate_peer` in `lan`: accepts RFC 1918 and ULA, rejects relay address,
  public IPv4, loopback, unspecified, empty set.
- `bind_endpoint(lan)` in-process on loopback test endpoints, asserting
  `Endpoint::addr()` has no relay entry.

Python unit: already covered by `tests/network_mode/`.

## Offline two-Mac test plan (the proof; nothing below has been run)

Setup: two Macs on one switch/router or a phone hotspot with the WAN uplink
unplugged (or hotspot data disabled), DHCP-assigned private addresses, macOS
firewall allowing the sidecar and Python. `HF_HUB_OFFLINE=1`, no `cloudflared`
on `PATH`, model snapshot pre-seeded on both, system DNS pointed at nothing
(`networksetup -setdnsservers <svc> 127.0.0.1`) so any accidental lookup fails
loudly.

1. Mac A: `python -m mycelium_seed --network-mode lan ...`; mint an invite;
   Mac B: `python -m mycelium_node --network-mode lan ...` joins. Both must log
   no DNS query. (Membership only, works before the Rust change.)
2. Both: start sidecars with `--lan`. Expect `address_ready` within the window
   with only `ip` addresses and **no** `relay` entry in the ready record.
3. Run the existing `transport probe` between them. Pass criteria:
   `selected_path = direct`; zero relay bytes; round trip completes with the
   WAN unplugged; sidecar process has no socket to a public address
   (`lsof -i -P -n -p <pid>`, `nettop`).
4. Negative: configure a peer with a relay address or public IP in `lan` mode
   -> `validate_peer` rejects. Pull the LAN cable mid-run -> bounded failure,
   not a hang.
5. Positive control: repeat with `--network-mode internet` and WAN restored;
   result must equal the sealed baseline.
6. Record: hardware, macOS build, router/hotspot model, client-isolation
   setting, all commands, raw `lsof` output. Mark UNVERIFIED claims verified
   only from this record.

## Risks

- **AP client isolation** (guest Wi-Fi, hotels) blocks LAN peers; there is no
  relay fallback in this mode by construction. A doctor/preflight command (R2 T14)
  should say so.
- Windows Defender Firewall prompts for inbound UDP; Linux hosts with `ufw`
  need an allow rule. Both unexercised.
- IPv6 link-local addresses need scope ids; first milestone may restrict to
  IPv4 RFC 1918.
- Touching the qualified transport: everything is behind `--lan`; the default
  branch of `bind_endpoint` must stay textually unchanged so the sealed build
  and its evidence remain valid.
