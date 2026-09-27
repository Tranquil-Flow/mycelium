# Mycelium node app architecture (Android first, iOS-ready, browser later)

Status: design, validated by an on-device Android spike (section 9). Scope: the native
mobile member application that A12 names as the only qualifying product path. Owner
intent: install the app, scan a QR code, and the phone is a node. No Termux, no
Tailscale, no shell.

## 1. Why this app exists (A12 requirements source)

A12 is the requirements source, not only the protocol map:

- `mycelium_mobile/a12_activation.py:254` excludes `termux`, `adb` and `ssh` as product
  paths; `require_undivided_product_path` (`:616-626`) accepts only
  `signed_native_application` and `embedded_iroh_transport`. Termux stays a
  development/conformance tool (`termux_conformance` source kind, `:362-368`) and can
  never qualify a rung.
- Specification section 3 (`docs/superpowers/specs/2026-08-18-mycelium-a12-generic-mobile-activation.md:61-84`)
  lists what the application must contain: durable device-owned Ed25519 identity, A8
  seed-pin/bootstrap and signed A9 v2 membership clients, embedded authenticated Iroh
  transport bound to the signed EndpointID record, a native evidence collector with
  consent, an assignment-local A2 artifact client, a registered native runtime,
  lifecycle/drain/generation fencing, and a privacy-reduced status adapter.
- Enrollment starts with `route_ready=false` (`a12_activation.py:396`, `:687-692`).

## 2. Targets

| Target | Shell | Serving model | Status |
| --- | --- | --- | --- |
| Android (arm64-v8a, minSdk 24, target 37) | Tauri 2 + app-local Kotlin plugin | foreground service (`specialUse`) + partial wake lock, user-started, stoppable from the notification | spike proven on Pixel 8 Pro |
| iOS / iPadOS | Tauri 2 (WKWebView) | `foreground_active_only` (`a12_activation.py:253`); drain on scene background | project skeleton generated, unsigned |
| Browser (no install) | same React UI, `mobile_core` pure modules as wasm32 | tab-foreground only; probe/evidence role first | architecture only |

## 3. Crate and package layout

```
native/mobile_core/          Rust core, no UI, no platform APIs
  canonical.rs               RouteQualificationV1 canonical JSON (sorted keys, "," ":" , no NaN, UTF-8)
  identity.rs                Ed25519 member key, evidence signature byte-compatible with
                             mycelium_qualification/signing.py (domain mycelium.ed25519_evidence_signature.v1)
  membership.rs              A9 signed envelope + join/capability/heartbeat messages
  onboarding.rs              pairing request + encrypted_invitation_handoff.v1 decrypt (X25519/HKDF-SHA256/ChaCha20-Poly1305),
                             QR payload codec
  wire.rs                    Router frame (mycelium.router_wire.v1) + MYAC/MYTK activation codecs
  runtime.rs                 StageRuntime trait + registered backends (section 6)
  evidence.rs                mycelium.mobile_runtime_observation.v1 builder; A12 guard mirrors
  node.rs                    NodeCore facade: state machine, tokio tasks (join, heartbeat, serve)
  transport.rs  [feature]    iroh 1.0.2 Endpoint, ALPN mycelium.iroh.sidecar.v1 (not wasm)
apps/node-app/               Tauri 2 app (React 19 + Vite, vitest)
  src/                       screens: onboard / status / serving / settings
  src-tauri/                 thin command layer over NodeCore; node_service plugin glue
  src-tauri/gen/android/     Kotlin: NodeForegroundService, NodeServicePlugin
  src-tauri/gen/apple/       generated Xcode project (unsigned)
```

`native/iroh_transport` is reused as a library dependency for its frame codecs
(`src/protocol.rs`, `src/remote.rs`); its sidecar/Unix-socket layer is not used in the
app because the app embeds the Iroh endpoint directly. The crate's library checks clean
for `aarch64-linux-android` (cargo-ndk, API 26) and `aarch64-apple-ios` at `4fea2b6d`.

## 4. FFI boundary

Deviation from the proposed UniFFI boundary, with evidence: under Tauri the UI calls
Rust through Tauri commands directly, and the Android foreground service needs no JNI
into Rust because the core lives in the same process (the service only holds the
process in the foreground contract). Measured on the Pixel: first `invoke` round trip
11-16 ms including webview IPC setup. UniFFI would therefore add a code generator and a
second binding surface with no consumer. The boundary is instead the `NodeCore` facade
in `mobile_core`, written UniFFI-ready (owned types, no lifetimes, one error enum,
async via returned handles), so the fallback in section 10 is additive: add
`uniffi` behind a feature and generate Kotlin/Swift without touching core logic.

Commands (UI -> core): `core_info`, `onboarding_begin` (returns the QR/pairing request),
`onboarding_complete(handoff)`, `node_status`, `serving_start`, `serving_stop`,
`settings_get/set`. Events (core -> UI): `node://status` snapshots, throttled to at most
4 Hz so the webview never floods. All network, crypto and inference work runs on the
core's tokio runtime, never on the UI thread; commands return in bounded time and long
work reports through events.

## 5. Onboarding UX and protocol

One-scan flow over the existing A12/A13 handoff (`mycelium_mobile/android_onboarding.py:79-82, 267-547`):

1. The owner console shows an **invite QR**: `mycelium:` + base64url(canonical
   `{"protocol": "mycelium.pairing_channel.v1", "pairing_url", "expires_at"}`), a
   short-lived owner pairing channel, not a seed secret. The app accepts it from the
   camera (barcode-scanner plugin, camera permission requested at scan time only), as
   a `mycelium:` deep link opened by any QR reader, or pasted.
2. The app creates its durable Ed25519 identity (first run), an ephemeral X25519
   recipient key, and a signed `mycelium.pairing_request.v1` with `peer_class`
   `android_native_iroh`, `carrier` `signed_native_application`, TTL <= 600 s, and
   posts it to the channel.
3. The owner verifies the request, mints a seed-signed invite whose nonce is the
   pairing nonce, encrypts `mycelium.encrypted_invitation_payload.v1` to the recipient
   key (ECDH X25519, HKDF-SHA256 with a 32-byte salt and info
   `mycelium.a12.pairing.handoff.v1\0 || request_digest`, ChaCha20-Poly1305 with the
   canonical header as AAD) and returns the `encrypted_invitation_handoff.v1`.
4. The app decrypts, checks `request_digest`, `pairing_nonce == invite nonce`, expiry,
   quota and the pinned `seed_key_digest`, verifies the seed's signed identity
   statement with the pinned key, joins (`/seed/join`), verifies the signed
   acceptance, and heartbeats with `route_ready: false`.

The user never sees a seed URL, EndpointID or token. A two-QR fallback (device QR
shown to the owner) exists for owner consoles without a reachable channel. After a
restart an enrolled member resumes through `/seed/resume` with a fresh incarnation;
no invite is consumed.

## 6. Runtime adapter and the llama.cpp-vs-MLX question

Evidence (phase 0):

- Only residual-stream hidden states cross a hop, as MYAC payloads of shape
  `(1, seq, hidden)`, little-endian, dtype bound to the graph
  (`mycelium_router/payloads.py:19-28,176-235`; `mycelium_router/mlx_runtime.py:1112-1118`).
- KV is stage-local (`mlx_runtime.py:1-6, 411`); a route containing a `pixel-stdlib`
  stage is forced to `complete_context_replay` (`physical_inference_node.py:597-600`).
  A12 excludes cross-backend KV migration (`a12 spec :146-149`).
- `mycelium_router/llama_runtime.py:31-37,54-69,121-135` only drives `llama-server
  /completion` with prompt text for a whole model: no layer range, no hidden-state
  import/export, not wired into any route.
- Today's Pixel stage is `pixel-stdlib` Python: one GPT-2 block of the synthetic
  `local/tiny-gpt2-qualification` model, fp32, no KV (`mycelium_mobile/pixel_stage.py:374-378`;
  `pixel_runtime.py:130-135, 352`).

Verdict: a phone stage interoperates with MLX stages **at the activation boundary only**.
The phone owns whole stages with its own stage-local KV (or replay); KV never migrates
across backends, so no KV format interop is needed and none is claimed. What must match
is the MYAC activation contract and numerical parity of the phone's layer range against
the reference, which is an A12 native-parity gate item per registered backend.

Backends behind `StageRuntime` (registry-driven, never brand-driven):

1. `mobile-rust-reference` (first): a Rust port of the `pixel-stdlib` GPT-2 block
   executor, fp32, replay semantics, parity-tested against `pixel_stage.py` fixtures.
   It lets the app join today's qualification route shape with no Python.
2. `mobile-llamacpp` (next, feature-gated): llama.cpp via `llama-cpp-sys-2` for Qwen
   class stages, without patching llama.cpp source: a sliced GGUF holding only the
   assigned layer range, input injected through `llama_batch.embd` at absolute
   positions, output captured from the last block's `l_out` tensor through the
   scheduler eval callback (bypassing the final norm), stage-local llama.cpp KV.
   Vulkan/CPU on Android, Metal on iOS. Unproven: it needs a parity measurement against
   the MLX stage for the same layer range and serving representation before any
   qualification; GGUF quantization differs from MLX quantization, so parity is bound to
   an exact representation, not assumed.
3. Browser: WebGPU runtime is out of scope; the browser page starts as a probe/evidence
   contributor.

Registering a new `runtime_backend` touches shared contracts
(`mycelium_membership/contracts.py:39-76`, `stage_signatures.py:95-116`) and is an
owner-reviewed step (section 11).

## 7. Background execution per OS

- **Android**: `NodeForegroundService` with `foregroundServiceType="specialUse"` (a
  `dataSync` service is time-limited on Android 15+), ongoing low-importance
  notification with a Stop action, bounded partial wake lock (6 h, re-acquired per
  start), `START_NOT_STICKY` so an OS kill makes the member stale and it reconnects
  with a fresh incarnation instead of being silently resurrected. The service owns no
  protocol logic. Claimed only to the exact mode physically demonstrated (spec section
  6): screen-off survival was observed while charging; unplugged doze behaviour is a
  separate measurement.
  Measured on the Pixel (Android 16): with the app merely started behind the
  keyguard (process state TOP_SLEEPING, no foreground service), netpolicy reports
  `blocked=APP_BACKGROUND` and every socket except loopback fails, so the iroh
  endpoint cannot reach its relays. Serving therefore requires the user-started
  foreground service; there is no background-socket shortcut.
- **iOS/iPadOS**: `foreground_active_only`. On `scenePhase` leaving active the core
  stops admission, emits a signed drain transition if the OS allows, cancels within the
  grace budget, drops request/KV state, fences the incarnation and reconnects with a
  fresh one when active again (spec section 7 steps 1-6). No background inference claim.
- **Browser**: same as iOS; the page drains on `visibilitychange`.

## 8. A12 gate satisfaction

| A12 requirement | How the app satisfies it |
| --- | --- |
| Product path (`a12_activation.py:616-626`) | Emits `signed_native_application` (the APK/IPA) and `embedded_iroh_transport`; no Termux/ADB/SSH code path exists in the app |
| Signed native application | Release APK signed with the project release key (key held outside the repo; certificate SHA-256 pinned in evidence as the signer identity). Debug builds carry `.debug` suffix and cannot claim this path |
| Source kind (`:538-553`) | Every metric the collector produces is `native_device_observation`; unavailable APIs report the string `unknown`, never a number |
| Observation record (`:434-447`, `:723-769`) | `mycelium.mobile_runtime_observation.v1` with exactly the eleven fields; metric names `[a-z][a-z0-9_]{0,63}` |
| Required gate families (`:255-263`) thermal, power, memory, lifecycle, network, artifact, parity | thermal: Android `PowerManager.getCurrentThermalStatus`/thermal headroom, iOS `ProcessInfo.thermalState`; power: battery level, charging, power-save; memory: `ActivityManager.MemoryInfo`/`os_proc_available_memory`, sustained samples; lifecycle: service/scene transitions and incarnation; network: transport type, iroh path (direct/relay), loss/handoff events; artifact: digest-verified assignment-local promotion; parity: runtime parity digest vs reference fixture |
| Durable identity (`RUNG_REQUIRES` `:196-203`) | Ed25519 key in app-private sandboxed storage (0600) today, proven to survive restart; wrapping it with Android Keystore / iOS Keychain is a pending step before qualification; incarnation changes on every process start |
| Member-signed profile/report (`:994-1120`) | `identity.rs` reproduces `Ed25519EvidenceSigner.sign` byte-for-byte (canonical JSON, statement digest, domain-separated payload), cross-tested against the Python verifier |
| `route_ready=false` at enrollment (`:687-692`) | Join and heartbeat always send `route_ready:false`; only the seed/planner can later place the member |
| Family-bound claim (`:556-597`, `:242-250`) | The app reports a pseudonymous `family:<hex>` reference derived from registered capability fields (ABI, SDK, SoC capability class), never a brand/model string; initial claims use `claim_scope=family_bound`; a general claim still needs `MINIMUM_ANDROID_FAMILIES = 2` |
| No brand branching (inventory `brand_or_model_specific_branching_allowed: false`) | No code path reads manufacturer/model to change behaviour; behaviour keys off observed capabilities only |
| Apple isolation (`:600-613`) | `ios` and `ipados` reported as distinct `platform_family`; simulator runs are labelled `synthetic_conformance` |
| Artifact rung (`:283-287`) | `inert_assignment_local`, `digest_verified`, bounded resumable acquisition, atomic promotion, thermal/power pause |

## 9. Spike evidence (Android, Pixel 8 Pro, SDK 36)

Debug APK (unoptimized Rust, 124 MB universal), `am start -W` cold start
TotalTime 546-714 ms over 4 runs; first Rust `invoke` 11-16 ms; foreground service
started from the UI, `isForeground=true types=0x40000000` (specialUse); process and
`PARTIAL_WAKE_LOCK mycelium:node` survived 90 s screen-off (device charging), same pid.
Evidence files: `runs/v2/pm-app/evidence/` in the program directory.

## 10. Fallback

If Tauri stops hosting the service cleanly (for example a webview lifecycle change
that kills the process when the activity is destroyed), switch to thin native shells
(Kotlin/Compose, SwiftUI) over the same `mobile_core` with UniFFI bindings; the core API
of section 4 is designed for that. The spike found no reason to switch.

## 11. Control-plane changes this app needs (owner decisions, not in this lane)

1. Register peer class `android_native_iroh` (already emitted by
   `android_onboarding.py:86`) in `mycelium_membership/contracts.py` with the app's
   runtime backend; today a join with it fails `membership_peer_class_invalid`.
2. Make it activation-eligible only through A12 rungs; today
   `peer_runtime_is_activation_eligible` hard-codes `mac_mlx_iroh`/`linux_numpy_iroh`
   (`contracts.py:313-324`) and the seed refuses offers otherwise
   (`mycelium_seed/coordinator.py:1312-1316`).
3. `a12_evidence.py:620-621` calls `require_android_family_evidence` without
   `claim_scope`, so the bundle still demands two families even for the family-bound
   initial claim.
4. Release signing key custody (owner-held keystore outside the repository, password
   in the macOS Keychain) and publication of the pinned certificate digest.

## 12. Browser path

The pure modules (`canonical`, `identity`, `membership`, `onboarding`, `wire`,
`runtime`, `evidence`) avoid tokio/std-net and compile to `wasm32-unknown-unknown`, so
a no-install page can reuse the same onboarding and envelope code. Transport in the
browser is Iroh's relay-only browser mode (no UDP hole punching), so latency-sensitive
stage work stays native; the browser page targets the probe/evidence contributor role
and inherits the iOS-style foreground-only drain model.

## 13. Build, run and remove (the simple path)

One toolchain set builds both mobile targets from the same sources:

- Android: `cd apps/node-app && npm ci && npx tauri android build --debug --apk --target aarch64`
  (needs the Android SDK, NDK and `rustup target add aarch64-linux-android`); the
  release APK is built with `npx tauri android build --apk --target aarch64` and
  signed by `scripts/sign-release.sh`, which reads the keystore password from the
  macOS Keychain and prints the signer certificate SHA-256 that A12 evidence pins.
- iOS: `npx tauri ios build` from the same package (Xcode; signing is an owner step).
- Core only: `cd native/mobile_core && cargo test --features net`.
- Remove from a phone: `adb uninstall now.mycelium.node.debug` (debug) or
  `now.mycelium.node` (release); uninstalling deletes the node identity.

Debug builds contain labelled developer hooks (status mirror, a synthetic
stage-pack assignment standing in for the A2 artifact client, transport trace) that
release builds do not compile.
