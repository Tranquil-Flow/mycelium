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

- Sustained serving: measured on 2026-10-27, 16:40:53–17:00:55 UTC, on two developer-owned Apple silicon Macs: the owner Mac (macOS 26.5.1) and a joined Mac (macOS 15.6.1). The 20-minute Qwen/Qwen2.5-0.5B-Instruct Fast mode soak passed on the stable demo relay. Six of eight scheduled chat attempts completed; one was refused with `chat_switching_runs` during rollover and one with `chat_not_ready` during owner restart. This was bounded recovery testing, not uninterrupted availability.
- Run rollover and owner restart: one physical rollover was observed from approximately 422 to 481 seconds into the soak. The observed unavailable-to-serving gap was 60 s; the sampling-based upper bound was approximately 120 s. After an owner restart, explicit Start network / Start run actions recovered service in 361 s, without member action or renewed consent. The joined Mac's saved consent was retained; these observations do not establish every possible consent-limit combination. An interrupted answer is not resumed.
- First-run path: the same day's scripted fresh-identity installation/join test passed and completed three chats on these Macs. Model files were seeded from cache, and setup used the app's API actions. This is not an independent human's GUI-only test, a fresh model-download test, or installation of the eventual published release ZIP.
- Performance: six completed soak chats measured 2.41–2.92 s to first token and 8.8–11.7 tokens/s at the client token-chunk polling boundary (250 ms polling), not native decode throughput. Normal mode was not measured for this candidate, so no matched normal/Fast comparison is claimed. This soak did not publish a swarm or verify the website's displayed statistics.
- In-flight Stop: a separate same-day streaming-chat probe observed chat cancellation 0.52 s after Stop, then the owner reached `stopped` at 189.79 s with no unproven cleanup. Cancellation is prompt; full cleanup can still take about three minutes. A subsequent native Quit check left no app processes, exiting in 1.25 s on the owner and 5.9 s on the joined Mac.
- Mailbox observations during the soak: the owner grew from 46,174,208 to 74,485,760 bytes (about 46 to 74 MB); the joined Mac had no local mailbox database and reported zero. This short window is not a multi-day retention measurement.

These observations used the device-test package's executable SHA-256 `8fff41823d357949ffa55c00b3fb8dfb0b4cc541f66343a72f21e3d8dd89186f`, which is also retained in the finalized release archive. Device-test ZIP SHA-256: `5baa365cdda4d107f604c22c1d90bb13a817c273258d1c9ce06a3615544fa07c`. The release archive has separately finalized installer/signature metadata and its own checksum below; published-ZIP installation remains a separate release-promotion gate.

The earlier v0.1.3 two-Mac test used Qwen2.5-0.5B over a phone hotspot. Neither that short-run result nor this 20-minute window establishes performance for larger models, larger swarms or other hardware. Raw device records are retained privately, not shipped in this repository.

## Known issues

- Temporary quick-relay owner restarts require a fresh invitation when the address changes. The recovery result above used the stable demo relay and must not be generalized to a changed quick-relay address. Relay renewal beyond 30 minutes remains physically unproven for this candidate: the owner selected a 20-minute soak. Stop during a chat can take about three minutes to reach fully stopped even after the chat cancels.
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

SHA-256: `fce4743a7f3a2453f6621753ed3892a6cefaddf52e7a3e98fd79ba51f8930025`.

Apple silicon macOS only. This is still early software with a limited hardware qualification scope. The app has an ad-hoc Apple signature, not a Developer ID signature, and is not notarized. macOS approval of the installer may require an administrator. Do not disable Gatekeeper; follow the installation guide.

Swarm members can see prompts, outputs or intermediate data needed for their work. This release does not widen saved consent or add fast-mode mid-request recovery.

License: AGPL-3.0-only.
