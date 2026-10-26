# Mycelium v0.1.4

Mycelium lets Apple silicon Macs in a swarm share the work of running a language model. This update fixes fast mode stopping after a few minutes, continues swarms across per-run time limits and adds a GUI path for the tested starter model.

## What changed

- Fast mode now renews its permission lease while the route serves. In v0.1.3 it stopped answering about five minutes after serving began. Renewal still checks membership and the run deadline; it does not bypass consent or add mid-request recovery.
- The owner's control mailbox keeps a bounded history instead of accumulating old control traffic indefinitely. This keeps old traffic from adding more status and control work over days, while retaining replay protection and unresolved work records.
- Joined Macs re-report and reconnect after the owner app restarts, once the owner is reachable at the same address. An interrupted answer is not resumed, and a changed temporary relay address still needs attention.
- Swarms continue automatically across per-run time limits. Each Mac agrees to lend compute for at most the chosen number of minutes per run, 30 by default. Automatic continuation respects the shortest time limit any Mac chose, including shorter consent on a joined Mac and any saved limit on the owner's own contributor. It stops the old run before that limit to allow cleanup, then starts a fresh run under the same saved consent limits. This automatic rollover does not extend one run's permission or keep an expired temporary relay running. In-flight answers are cancelled with `chat_switching_runs`, not resumed. Chat waits for the fresh route. Unproven cleanup or a changed model/member binding stops continuation; other startup failures have bounded retries, stopping after three consecutive failures. **Stop** ends the session and cancels continuation.
- **Qwen2.5-0.5B-Instruct**, the tested starter model, is now in the model picker and is the default for a fresh setup. Its source download is about 1 GB if not cached. Fresh setups now default to **4 GB of storage consent** to cover the starter's whole-snapshot share and preparation, not just the download. Existing installations retain their saved model choice and consent limits.
- Layer placement now fits each Mac's share within its agreed storage and memory limits, using the limits reported to the owner. It keeps the original split when it fits and tries other contiguous splits when necessary. If no split fits, the app reports the resource refusal plainly and does not widen consent. Startup also surfaces member resource refusals promptly while preserving staging cancellation and expired-offer cleanup.
- Quit from Mycelium's menu or press **Cmd-Q** with Mycelium active to stop cleanly. Native Quit and **Quit demo** share the same shutdown: stop the run and close the relay before exiting. Closing the browser tab does not quit the app. The packaged native handler is built from source with Apple's system SDK, without a runtime download.
- The owner page has a **Fast mode** switch, off by default. It applies to the next run and cannot be changed while a run is active. The GUI path does not require an environment variable.
- Startup status reads use the last complete snapshot plus current run progress rather than waiting for the startup work to finish. Error messages now explain missing capability reports, expired fast-mode permission and control sessions, with steps to reconnect or start a fresh run.
- The website shows per-swarm first-token time, tokens/s and per-stage time, plus a hop round-trip time field. Each swarm's own app measures and reports available statistics. Timings are measured in the app, not from the visitor's browser; per-stage time measures compute per token, not network latency. They are self-reported observations, not independent measurements by the directory or a promise about the next prompt. Fresh adjacent-stage transport observations provide round-trip estimates. An existing owner-to-member health probe is the fallback, labeled **Network round trip (probe)**; it includes mailbox polling and command handling and is not one-way or pure network latency. Missing or stale samples remain **Not measured**. This is implemented measurement support, not a new physical benchmark.

## What was verified

- Sustained serving: `<<TBD: soak duration, hardware, model, mode, request count and outcomes>>`.
- Run rollover and owner restart: `<<TBD: physical rollover count, consent limits, reconnect results and interrupted-request outcomes>>`.
- First-run path: `<<TBD: stranger-path installation, GUI-only setup, first-chat results and remaining failures>>`.
- Performance: `<<TBD: first-token time, tokens/s, measurement window, normal/fast mode comparison and website-stat verification>>`.

The earlier v0.1.3 two-Mac test used Qwen2.5-0.5B on developer-owned Macs over a phone hotspot. Its short-run speed observation is not a v0.1.4 soak result. The pending results above must not be read as passed tests or as evidence for larger models, larger swarms or other hardware.

## Known issues

- The temporary relay starts with a window of up to 30 minutes and renews before expiry while an owner run is active or a swarm is published, within a total granted-window cap of 24 hours. Renewal keeps the same tunnel and address; it does not extend run consent or invitation expiry. Idle relays, expired relays and relays at their cap still close. Restarting a closed tunnel changes its address, so old links can stop working. A fixed-address relay retains its configured lifetime and renewal cap. This is not an uptime guarantee.
- After restarting the app on a joined Mac, press **Start / reconnect** there. Automatic re-reporting after an owner restart is a different case.
- Fast mode still has no mid-request failover, replay recovery or continuation from saved model working memory. A stage failure can end the request. Leave it off for the normal recovery-capable path, which still needs a suitable ready replacement.
- Changing resource selectors does not replace saved enrollment consent. The new 4 GB default does not raise existing saved limits. Repeating enrollment with changed limits is rejected. Choose a model that fits the saved limits; do not delete identity files to bypass a refusal. Planning uses known reported limits; changed or previously unknown limits can still be refused when a member accepts work.
- All Macs should run the same version so their signed reports include their time limits. A missing time limit is treated conservatively as 60 seconds per run, not permission to use the owner's longer limit. This can cause short runs or leave too little time for startup. A member whose saved time limit has become smaller refuses an oversized assignment rather than silently accepting it.
- Larger-model performance and wider hardware compatibility are not established. Qwen3-14B remains available and needs roughly 30 GB for the source download alone; it is no longer the fresh-install default.
- The public demo can be intermittent. Route compilation can still take several minutes on a busy owner Mac; faster status does not mean faster model compilation.
- There is no supported, fully offline two-Mac app flow. Included LAN and offline-transfer code does not remove the app's public HTTPS and inference-transport requirements.

## Upgrade from v0.1.3

1. Stop serving and quit Mycelium on every participating Mac.
2. Download the v0.1.4 zip and checksum from this release and verify the checksum before approving the installer.
3. Extract the archive and follow [the macOS installation guide](user/QUICKSTART-MACOS.md). Keep existing application data and swarm identity; the update does not require deleting either.
4. Upgrade the participating apps together. Reopen them, reconnect the joined Macs and start a fresh run. Do not upgrade an in-flight route. Mixed-version operation is not promised.
5. Check **Model** before downloading: existing app data can retain an earlier choice. Select **Qwen2.5-0.5B-Instruct** for the tested starter setup. Choose **Fast mode** before starting the new run.
6. Check the upgraded installation before removing the v0.1.3 download.

If your launch environment already sets `MYCELIUM_TOKEN_PATH=on` or `off`, that explicit setting overrides the saved preference and locks the GUI switch. Remove it from the app's launch environment and reopen the app to use the switch. Do not change modes during a request.

## Download and verification

Asset: `mycelium-0.1.4-macos-arm64.zip`

Run `shasum -a 256 mycelium-0.1.4-macos-arm64.zip` and compare the output with the separate `.sha256` file from the release.

SHA-256: `<<TBD: final release archive SHA-256>>`.

Apple silicon macOS only. This is still early software with a limited hardware qualification scope. The app has an ad-hoc Apple signature, not a Developer ID signature, and is not notarized. macOS approval of the installer may require an administrator. Do not disable Gatekeeper; follow the installation guide.

Swarm members can see prompts, outputs or intermediate data needed for their work. This release does not widen saved consent or add fast-mode mid-request recovery.

License: AGPL-3.0-only.
