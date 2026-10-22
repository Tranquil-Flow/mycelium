# Troubleshooting

Format: **what you see** → **what it means** → **what to do**.

Message texts are quoted from the code. Where the app shows only a code such as `member_slice_timeout`, the code is quoted too, so you can search for it. Messages without a quoted source in this page were not checked in code.

Windows and Linux: none of this has been run on real Windows or Linux hardware. This page covers the macOS app.

## Installing

| What you see | What it means | What to do |
|---|---|---|
| macOS says **"Install Mycelium.command" Not Opened**. | The package has an ad-hoc signature only. It is not Developer ID signed and not notarized. | Click **Done**, then **System Settings → Privacy & Security → Open Anyway** (next to "Install Mycelium.command"), then **Open Anyway** in the next dialog. Never disable Gatekeeper. If macOS still refuses, stop and report its exact text. |
| A second macOS dialog names **"Mycelium A13 Demo"** as not opened, or the Terminal says `killed`. | You are running an older script that started the app without checking it first. | Download the current package. If you must continue, open **Privacy & Security → Open Anyway** for the app, then run **Install Mycelium.command** again. |
| The app says **"Mycelium is not installed yet"** and does not start. | You opened the app from the downloaded folder (or from a temporary macOS copy). It will not run a session there. | Run **Install Mycelium.command** from the downloaded folder (see the Quickstart, Step 3), then open `~/Applications/Mycelium A13.app`. |
| Terminal ends with `Mycelium is not installed: ... does not match SHA256SUMS` (or `APP-FILES.sha256`, or `is not covered by SHA256SUMS`). | A file in the extracted folder is damaged, incomplete or changed. Nothing was changed and nothing was run. | Do not install. Compare the zip's SHA-256 with the owner's value, delete the folder and extract again. |
| `"refused": "install_already_running"` | Another install, rollback or uninstall holds the install lock: a second Terminal window, or a double-click that started twice. | Wait for the other one or close its window, then run **Install Mycelium.command** again. The lock is released automatically when a process ends, even if it was killed; there is no lock file to delete. |
| `"refused": "install_io_error"` or `"installer_failed"` | The installer could not read or write a file (full disk, unwritable folder) or hit an unexpected error. Nothing half-installed is left behind. | Free some disk space, then run **Install Mycelium.command** again. If it repeats, report the code. |
| An earlier install was interrupted (macOS killed it, the Terminal was closed, two ran at once), or a traceback ending in `FileNotFoundError` under `programs/<version>`. | A stale, empty or half-copied version folder. The current installer sets it aside by itself. | Run **Install Mycelium.command** again; no manual cleanup. The old folder is kept as `programs/<version>.stale-<n>` and can be deleted once the install works. With an older installer that still crashes, quit it, rename `~/Library/Application Support/Mycelium/programs/<version>` to `<version>.old` in Finder, and run the new package. |
| `shasum` output differs from the owner's value. | The file is not the file the owner built (damaged or changed). | Do not install. Get the file again. |
| `codesign --verify` prints something or exits non-zero. | The extracted app is damaged or was modified. | Do not install. Re-extract; if it still fails, get the file again. |
| Installer prints JSON ending `"refused": "package_digest_mismatch"` | The installed bytes do not match the signed manifest. | Re-extract or re-download. Do not edit the bundle. |
| `"refused": "bundle_incomplete"` | Something in the package is missing. | Re-extract the whole zip. Run the installer from inside the extracted folder. |
| `"refused": "package_publisher_invalid"` | The package carries no usable publisher key. (On first install the installer remembers the publisher key; later packages must match it.) | Ask the owner for a correct package. |
| `"refused": "package_downgrade_rejected"` | This package is older than what is installed. | Install the newer package, or ask the owner. |
| `"refused": "installed_version_conflict"` | That version is already installed with different contents. | Ask the owner. Do not delete files by hand. |
| `"refused": "platform_unsupported"` | This Mac is not supported. Only Apple silicon (arm64) is. | Use an Apple silicon Mac. |
| `"refused": "register_target_not_ours"` | Something that is not Mycelium's link is already at `~/Applications/Mycelium A13.app`. The installer will not replace it. | Move or rename that item yourself, then install again. |
| `"refused": "not_installed"` / `"no_previous_version"` | You ran `--uninstall`, `--rollback` or status with nothing to act on. | Nothing is wrong. Check with `--install-status`. |

## Creating and joining a swarm

| What you see | What it means | What to do |
|---|---|---|
| "Action not completed: network requires fresh owner. Existing identity and evidence are retained." | You tried **Start public relay** after the swarm already exists. A swarm's identity records its relay address, so a relay cannot be added afterwards. | Start the relay **before** creating the swarm. Adding a relay to an existing swarm is not implemented. |
| "Action not completed: … Existing identity and evidence are retained." (any other code) | The app refused an action. The code is the text after "not completed", with underscores shown as spaces. Your identity was not changed. | Read the code. Retry once. If it repeats, report the exact text. |
| "Start the public relay first (Invite or join with a code → Start public relay). (swarm_relay_required)" | You tried to publish or share a swarm without a relay. | Press **Start public relay**. |
| "The swarm directory is not reachable right now. (swarm_directory_unreachable)" | The app could not reach the public swarm directory (needs internet). | Check your connection and retry. Your local swarm is unaffected. |
| "That is not a swarm link (it starts with mycelium-swarm:). (swarm_link_invalid)" | The pasted text is not a swarm link. | Paste the whole link. |
| "The directory returned a swarm with a different owner than the link. Not attached. (swarm_record_owner_mismatch)" | The directory's answer did not match the link you pasted. The app refused it. | Do not retry with the same link. Ask the sender to check it. |
| A reused or expired join code is rejected. | A code admits one Mac, once, and expires (5, 10 or 15 minutes). | Ask the owner for a new code (**Invite a Mac**). |
| You cannot reach the other Mac. | In the app, joining needs no SSH or file copying. It needs both Macs online and the public relay running on the owner Mac. | Check both Macs are awake, online and the owner's relay is on. A Mac that sleeps or loses Wi-Fi drops out. |

## Starting the run

| What you see | What it means | What to do |
|---|---|---|
| "Download the selected model on this Mac first (Inference → Download / resume source), then press Start again. (owner_model_not_prepared)" | The owner Mac does not yet have the model. | Prepare the model on the owner Mac, then press **Start demo** again. |
| "At least two Macs must have joined and reported in. Invite a Mac with a code or QR, then press Start again. (two_macs_required)" | Only one Mac is in the swarm. | Invite the second Mac and wait for it to join. |
| "The run needs two different physical Macs; two copies of the app on one Mac do not count. (two_distinct_hosts_required)" | Both members are the same computer. | Use a second physical Mac. |
| "Some joined Macs did not report in. (member_descriptors_timeout)" | A joined Mac did not answer in time (asleep, offline, app closed). | Wake it, open the app, check its network, press **Start demo** again. |
| "A joined Mac did not finish fetching its slice in time. (member_slice_timeout)" | A Mac's download of its slice of the model was too slow or stalled. | Check that Mac's network and free disk, then start again. |
| "A Mac does not have enough memory for its share of this model. Add a Mac with more memory, or choose a smaller model. (member_memory_insufficient_for_share)" | The memory check failed. A 16 GB MacBook cannot take as much as a 48 GB Mac. | Pick a smaller model, or add a Mac with more memory. |
| "A Mac does not have enough free disk for its share of this model. (member_disk_insufficient_for_share)" | Free disk on a Mac is below its share. | Free space on that Mac (the model needs many GB). Also `insufficient_disk` when a download runs out of space. |
| "A joined Mac agreed to less memory per run than its share of this model needs. (member_memory_cap_below_share)" or "…less storage than its share… (member_storage_cap_below_share)" | The limit chosen at join time is too small. | That Mac's user re-joins with a bigger limit. |
| "A Mac cannot hold even the smallest share of this model. Add a Mac with more memory, or choose a smaller model. (member_cannot_hold_share)" | No split of this model fits that Mac. | Smaller model or a bigger Mac. |
| "This Mac's storage limit is below what the selected model needs here. (owner_storage_consent_below_model)" | Your own storage limit is too low. | Raise the storage limit and consent again. |
| "A run is already active. Stop it first. (owner_run_active)" | You pressed Start twice. | Press **Stop**, then start. |
| "The route did not qualify in time. (route_qualification_timeout)" | The pre-serving check did not finish. | Retry. Check both Macs' network. |
| "The route failed its startup qualification, so nothing is served. The owner log names the Mac and reason. (route_qualification_failed)" | A start-up check failed on purpose and the app serves nothing rather than serve badly. | Read the owner log for the Mac and reason; fix that Mac; start again. |

## Chatting

| What you see | What it means | What to do |
|---|---|---|
| "The swarm is not serving right now. The host starts it from the Mycelium app." (`chat_not_ready`) | No run is active. | Ask the host to press **Start demo**. |
| "Warming up: the swarm is qualifying its route. Chat opens when it is ready." | The route check is running. | Wait. |
| "The route is not qualified yet (warming up). Try again in a moment." | Same. | Wait and retry. |
| "Someone else's prompt is running. Try again in a few seconds." (`chat_busy`) | The chat handles one prompt at a time. | Retry shortly. |
| "You sent several prompts this minute. Wait a little." / "The room sent many prompts this minute. Wait a little." | Per-person and per-room rate limits. | Wait about a minute. |
| "That prompt is too long (2,000 characters at most)." | Chat prompts are capped. | Shorten the prompt. |
| "Cannot reach the host right now." | The chat page cannot reach the owner Mac. | Check that the owner's app is running and online. |
| An answer is cut off with `[stopped: <code>]`. | The request ended with an explicit failure. It was **not** reported as a success. | Send the prompt again. See the next section if it repeats. |
| Research UI badge: "Stream interrupted", "Cancellation pending", "Cancellation unconfirmed", "Failed". | The connection broke, a stop is in progress, a stop could not be confirmed, or the request failed. | Wait; if it stays, retry. "Cancellation unconfirmed" means the app could not prove the stop; do not assume work ended. |
| "membership alone does not make inference available" | The Mac joined, but the route has not passed its check. Joining is not serving. | Wait for qualification, or start the run. |

## A Mac that left the swarm, or was restarted while serving

| What you see | What it means | What to do |
|---|---|---|
| "already joined" when a Mac that left tries to join again. | Leaving stops serving and withdraws consent, but the Mac is still a member until the owner revokes it. There is no in-place re-join in V1. | The owner revokes the Mac (owner screen), then opens a join window and the Mac pairs again with a **new join code**. |
| The run ends with "A joined Mac was restarted while the demo was serving" (`member_restarted`). | A joined Mac's app was restarted during a run. It comes back as a new membership generation without its model loaded, so it cannot rejoin that route. The owner ends the run instead of letting chats hang. | Press **Start** again; the restarted Mac takes part in the new run. |
| "The previous route is still closing" (`previous_run_still_closing`). | The last route is still waiting for a Mac to confirm cleanup. | Wait a minute and press **Start** again. |

## When a Mac leaves or fails mid-answer

These are designed behaviours, tested on two Macs. See [WHAT-MYCELIUM-DOES](WHAT-MYCELIUM-DOES.md).

| What you see | What it means | What to do |
|---|---|---|
| The answer pauses, then continues. | A standby Mac took over from a saved copy of the conversation's working memory (A7), or the work was redone on a replica (A6). | Nothing. |
| The answer ends with a failure. | No qualified standby was available, or the standby's copy was incompatible or behind. The request failed honestly. | Start again once both Macs are back. |
| After a remote stage Mac is killed, the request fails after a few seconds rather than hanging. | Product defect found on hardware on 2026-10-20 and fixed in the final candidate: before the fix, no failure arrived for about 90 seconds and the service then crashed. After the fix, a stage whose process has exited is declared lost within a liveness tick. A slow or missed health check alone does not cancel a request; only an exited process does. | If it hangs for more than about 15 seconds, report it. The two-Mac re-test of this fix was still pending when the report was written. |
| The path changes from direct to relay in the Incidents view ("Activation path transition"). | Your Macs lost a direct link and moved to the relay. The prior observations are kept and a new generation is recorded. | Nothing. Expect slower answers on a relay. |
| "Membership lease stale" (incident). | A Mac's membership lease expired; control is blocked until renewal. | Reconnect the Mac (**Start / reconnect**). |

## Adaptive sharing and Pause sharing

| What you see | What it means | What to do |
|---|---|---|
| "Off. Nothing is reduced or paused automatically; ..." | Adaptive sharing is off. Your memory limit, disk floor, power/heat/network limits, Pause sharing and Stop still apply. | Switch Adaptive sharing on if you want this Mac to protect itself. You must tick the acknowledgement. |
| "On · reduced. Taking fewer new requests while this Mac is busy." | The Mac is under some pressure. | Nothing. |
| "No new work; finishing, handing off or ending running work." | Pressure is high or you pressed Stop or turned on Pause sharing. | Wait. |
| "Stopped. Model memory released; no work from this Mac." Reason shown: "you paused sharing", "you pressed Stop", "memory is low", or "the CPU is busy". | The Mac has withdrawn. | **Allow serving again**, or release Pause sharing. Re-entry waits for about two minutes of calm plus a fresh check with the swarm, so it is not instant. |
| "Running work has not finished within the expected time." | A running request did not end in time after withdrawal started. | Wait; if it persists, quit the app. |
| "The sharing policy could not be read. New work is refused until you turn it off or on again." | The saved setting is unreadable. The Mac fails closed. | Switch Adaptive sharing off and on again. |
| After restarting the app the Mac does **not** take work immediately. | Fixed in the final candidate: a stop or withdraw now survives an app restart and the Mac starts "fenced". | Wait for the calm period, or press **Allow serving again**. |

## Developer test harness only

These messages come from the project's own two-Mac test scripts, not from the app. You will not see them as an ordinary user. They are included because they happened.

| Message | Meaning |
|---|---|
| `PreconditionUnmet:packaged_preflight:ssh_failed` | The harness reaches the second Mac over SSH. It could not find that Mac's staged source copy (`~/mycelium-pm-<sha8>`). The harness stopped before any device work. |
| `laptop_sidecar_equals_mac_sidecar` | The harness requires the same network helper binary (the "sidecar") on both Macs; they differed. |
| Disk below the 20 GiB floor | A harness safety rule: it will not start a stage with less than 20 GiB free on a Mac. |
| Second Mac on battery | The harness warned that the laptop was discharging during a long run. |

## Still stuck?

Write down the exact text on screen and what you pressed before it appeared. The app keeps your identity and evidence when it refuses, so retrying is safe.

## Sources

- `mycelium_a13_macos/web/app.js` ("Action not completed: …" notice; adaptive status and reason texts)
- `mycelium_a13_macos/web/run.js` (run error texts and codes)
- `mycelium_a13_macos/web/swarm.js` (swarm error texts and codes)
- `mycelium_a13_macos/web/chat.js` (chat messages)
- `mycelium_a13_macos/web/index.html` (consent and adaptive texts)
- `mycelium_a13_macos/app.py` (`relay_required`, `network_requires_fresh_owner`), `mycelium_a13_macos/acquisition.py` (`insufficient_disk`)
- `mycelium_a13_packaging/installer_core.py`, `mycelium_a13_macos/installer.py` (`refused` codes, install lock, `.stale-<n>`), `scripts/finalize_a13_macos.py` (`Install Mycelium.command` messages), `mycelium_a13_macos/launch_location.py` ("not installed yet")
- `mycelium_a13_macos/adaptive_host.py` (`recovery_s` = 120, `stale_after_s` = 6), `mycelium_a13_macos/adaptive_owner.py`
- `ui/web/src/features/inference/InferenceWorkspace.tsx` (phase labels), `ui/web/src/features/internetNative/InferencePathBadge.tsx`, `InternetIncidentVocabulary.ts` (incident labels)
