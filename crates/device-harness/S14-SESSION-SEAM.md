<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# S14: candidate-bound native status/session seam

Status: **fake-qualified adapters, not native/device/package acceptance**. Round-6
integration input: preview 3 `e3773ea3`; genuine foundation evidence from
`v02/foundation-exit` `67a57963` is consumed read-only, never edited. The S18 lint
resolver correction is documented separately in `TM002-ROUND6.md`.

## Round 6 implemented boundary

- `evidence_inputs::Inputs` authenticates exact native/oracle manifest bytes
  against caller-supplied independent pins. `read_env` uses the two canonical
  environment variables, bounded regular-file reads and final-symlink refusal.
  It retains both source identities and the four declared native resources,
  including `libjaccl.dylib`. It does **not** open referenced assets or promote
  discovery to L8/L11 package/OS/runtime authority.
- `native_io::NativeIo` implements the existing session `Io` over fake-backed
  `Files` and `Wire` primitives: private metadata checks, native-control JSON,
  the pinned launch HTML form grammar, candidate-bound session encoding,
  expected-inode atomic-replacement contract and exact readback. Socket targets
  are typed IPv4 loopback plus observed port; route-specific headers/body reject
  Host/proxy/credential injection, redirects and arbitrary routes. Provider
  suboperations share a decreasing deadline. `FileLimits` requires typed caller
  caps without operational defaults; native provider admission must bind these
  to L1 resource policy, not adopt synthetic test limits.
- The existing session `Adapter` now implements member `Http`: read-only status
  plus explicit Start/capability/join actions. It authenticates under the same
  total deadline, supplies exact cookie/origin/action headers, and dispatches
  each action once. A 401/transport loss after dispatch never renews or replays
  that action. This is not an automatic member-repair capability.
- `native_staging::stage` verifies all four source fingerprints before creating
  exclusive roots, then checks each raw copy on both fake hosts. Partial roots
  stay spent. This is **pre-relocation input staging**, not a replacement for
  L11's manifest schema/VerifiedBundle or post-relocation package qualification.
- `examples/foundation_discovery.rs` was executed against the genuine manifests
  using the canonical env vars. It reports native/oracle digests, four resources,
  `referenced_assets_opened:false`, `native_execution:false`, and
  `release_qualified:false`. Neither oracle nor native artifacts were executed.


## Ownership and reconciliation

| Owner | Retained authority | L12 consumes / remaining handoff |
|---|---|---|
| L11 | `desktop::package::{Manifest,Entry,Kind,VerifiedBundle,admit_bundle}` | Preserve its schema-1 entry list and verified native/OS admission. Supply the digest of the **exact serialized manifest bytes** and a separately verified candidate/process identity. L12's `Binding` is a receipt envelope, not another bundle schema or a constructor for `VerifiedBundle`. |
| L8 | `backend-mlx/build.rs::verify_native`, pinned headers/libraries; native-input evidence owner | Supply the digest of the genuine admitted native-input manifest. The frozen build checks headers, `libmlxc.dylib`, `libmlx.dylib`, **`libjaccl.dylib`**, and `mlx.metallib`; the L11 mandatory list alone is not the full backend dependency closure. Reconcile install-name/rpath and any required third library without treating equal paths as equal provenance. |
| L12 | `native_session::{Binding,Observation,Adapter,Io,Receipt}`; existing package/release proof types | Bind source/commit/executable candidate, exact PID/birth/executable, launch epoch, desktop-manifest digest and native-input digest before and after HTTP. Reject any changed binding, stale control file, changed port or reused process. These identity observations remain injected; hexadecimal hashes alone do **not** prove resource admission. |
| L10 + L11 (S12, BACKLOG 2) | Native bootstrap and real service/session authority | Supply the concrete native re-open/file/HTTP adapter. Do not fabricate browser cookies or translate native authority into an invented browser credential. Preserve the genuine `/native-session` → private launch form → `/session` path, then re-read `/api/status`. |

The concrete `Io` must independently obtain candidate/resource identities from
trusted package/OS reads, never from the status endpoint attesting itself. Its
HTTP implementation is **loopback-only at 127.0.0.1 and the observed port**, with
no proxy, redirect following or arbitrary host input. File reads/writes require
private, owned, regular single-link files, no symlinks, exact atomic 0600 session
replacement/readback, and a fresh native-minted launch form. Round 8 adds the native
implementations below (still no physical claim).

## Bounded behavior already exercised behind recording fakes

- Successful cached status does not renew. Only exact HTTP 401 with
  `open_from_native_launcher` can renew; no renewal on timeout, other errors,
  generic 401, or a second refusal. An absent session consumes the same allowance.
- Native credentials go only to `/native-session`; the launch value goes only
  to `/session`; the resulting cookie goes only to `/api/status`. Requests never
  follow redirects. Session-bearing types intentionally have no Debug/Serialize.
- All observation, storage and HTTP phases share one remaining deadline.
  `keeper::NativeKeeper` additionally shares **one renewal allowance and one
  status deadline across the whole keeper check**, including recovery rechecks.
- Keeper Start is separately capability-injected, requires a failed rollover,
  exact `rollover_members_changed`, present null `cleanup_unproven`, same swarm,
  a fresh eligibility recheck, durable rolling-budget reservation and readback.
  An intentional stopped state is never auto-started. A rejected/ambiguous Start
  spends its reservation and is never replayed inside the check.
- Member clock age is inspected independently of owner success; the real
  `authority.members[].clock.age_seconds` shape is parsed by `member_watch`.
  Silence/unobserved clocks alert. No member restart/repair capability exists.
- Public `site` verification refuses every case variation of client `CF-*`,
  credentials and CRLF injection before transport; it follows no redirects.

## Budget handoff (L1)

`harness_budgets::L1` resolves exact semantic registry names; absent/wrong-unit
entries fail closed, with **no numeric operational fallback**. `drive_registered`
binds the owner-ruled minimum before executing the soak. Existing `drive` and
CLI evaluation remain supplied-policy offline replay, not release authority.

Exact requests/provenance are in program evidence
`runs/v2/v02/l12-receipts/round5-budget-requests.json`. Round 6 adds the seven
semantic declarations through the L1 table mechanism with source provenance:
`HarnessMinimumSoak` (1200 seconds), keeper recovery window/count,
rollover/startup grace, member-silence policy and owner-status check deadline.
The owner ruling is historical v0.1.4 policy, **not** a v0.2 final-soak waiver;
longer requested minima remain binding. L1b remains canonical registry owner.
Synthetic observed clock offsets are not registry policies (see S18 correction).

## Still open; no substitute evidence

Genuine discovery manifests are now consumed and the concrete decoding,
member HTTP and raw-staging adapters are fake-qualified. Round 8 supplies native
private-file, atomic-replacement, loopback-wire and process-probe implementations
(next section). S14 remains open for the native staging primitives,
independently admitted candidate resources, relocated
native/GPU load, minimum OS, install/upgrade/rollback and same-byte physical
release receipts. The supplied native manifest explicitly has no Rust packaged
executable and retains its minimum-OS/relocation blocker. `release_qualified`
stays false. The keeper's durable Start/CAS binding still needs native wiring;
observation renewal is never permission to replay an action. No live runner or
CLI has been enabled by this change.

S15 is retained unchanged: precision-safe CLI decoding and the original strict
restart-window/counter regression remain in the full default and
`serde_json/arbitrary_precision` test gates. Python is an external read-only
oracle only; no Python is imported, executed, or copied into the Rust test path.

## Round 8: native implementations (local-fixture qualified, not product acceptance)

- **Descriptor boundary.** `native::NativeTransport` (the only spawn in this crate)
  now calls L6's `node::process::confine_descriptors(&mut command)?` as the last
  registration before `spawn` (LANE-COMMON rev 3 dependency edge `device-harness -> node`;
  lock delta is that one edge). A child receives exactly fds 0/1/2, as v0.1.4's
  `close_fds=True` did. `tests/descriptor_confinement.rs`: with six gap sockets
  (TCP listener, both TCP ends, UDP, Unix pair, FD_CLOEXEC cleared) the probe saw
  `held:3..8` before the change and none after; a control proves the fixture can see
  inheritance, and stdio capture / failed-exec reporting survive. A refusal returns
  `descriptor_confinement_failed` and nothing is spawned.
- **`native_files::NativeFiles<P>` implements `Files`.** Two exclusive 0700 canonical roots
  (the app's `native-control.json` / `open-demo.html`; the harness's own session
  directory), re-identified by device+inode on every operation. Reads are
  `O_NOFOLLOW|O_NONBLOCK`, judged from `fstat` of the open fd, bounded to cap+1 bytes;
  a symlink, FIFO, wrong mode or extra hard link is *reported* and `NativeIo` refuses
  it. `replace_session` compares the expected identity, writes a 0600 `O_EXCL` temp,
  fsyncs, renames, verifies inode/mode/nlink and fsyncs the parent; a stale identity,
  planted symlink or failed write leaves the existing record untouched with no temp
  residue. Compare-then-rename is not a CAS against a same-uid hostile writer; the
  exclusive directory is the guard (stated, not hidden).
- **`native_os::HostProbe<T: Transport>` implements `Probe`.** `process`: `/bin/ps -o lstart=
  -o comm= -p PID` parsed strictly (macOS `Wed 28 Oct 05:27:08 2026`, absolute path;
  pid 0/1 never reach the OS). `listener_port`: `/usr/sbin/lsof -nP -a -p PID
  -iTCP@127.0.0.1 -sTCP:LISTEN -Fn`, exactly one distinct loopback port. These run
  through the confined `NativeTransport`. Executable identity is what `ps` reports,
  not a code-signature or package proof; candidate/package facts stay injected
  (`native_files::Admitted`, from L8/L11 evidence).
- **`native_wire::NativeWire` implements `Wire`.** One `Connection: close` HTTP/1.1
  request to `127.0.0.1:<port>` only; framing headers are owned by the module; replies
  are read under the shared deadline and a hard byte cap with strict framing
  (no chunked, duplicate or short `Content-Length`); 3xx is returned, never followed.
- **Caps.** `harness_budgets::file_limits` / `loopback_response_limit` take every cap from
  an exact registry id. Control = `PyFinalMyceliumA13MacosAppMaxBytesL306C64` (4096,
  the v0.1.4 `native-control.json` limit). Launch, session and response ids do **not**
  exist yet; they are requested in program evidence `L12-evidence/S25-registry-ids.md`
  and the functions fail closed (`harness_budget_unregistered`) until L1b registers
  them. Test-fixture caps are labelled synthetic and are not a proposal.
- **Qualification.** `tests/native_e2e.rs` drives the real `NativeIo` + `Adapter` over
  these adapters against a local loopback fixture app (real files, real sockets):
  first check renews once, the cached session is reused, a 401 expiry renews exactly
  once with an inode-changing atomic replacement, and a control port that disagrees
  with the observed listener is refused. `release_qualified` stays false; no packaged
  candidate, device or physical receipt is involved.
