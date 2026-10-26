# What Mycelium does

Mycelium runs a language model across a few computers that you own, so that no single computer has to hold the whole model. Today that means two Apple silicon Macs. It is a preview. This page lists what each part does, in plain words, and how far it has been proven.

**How to read the status column.** "Shown" means the project ran it on its two real Macs (a 48 GB Mac and a 16 GB MacBook) and a written judge verdict exists. "Pending" means the run existed but no final verdict was written when the report was drafted (2026-10-20 04:40). Nothing here is a claim about other machines, other models, or other networks.

| Feature | What it means for you | Status |
|---|---|---|
| Direct route and relay switch (A1, A14) | Your Macs talk directly; if they cannot, they switch to a relay | Shown |
| Continue after a Mac leaves (A6, A7) | A standby Mac carries on your answer | Shown |
| Honest failures | An answer that cannot finish says so | Shown for A6/A7 cases |
| Signed install (A13) | The package is checked before it installs | Automated check passed; owner hands-on install pending |
| Seed migration (A9) | The swarm's membership authority can move to a new format and keep identities | Shown on a private test seed |
| Two-stage split (A5) | The model's layers are split across two Macs | Normal and concurrent runs shown; stage-loss case fixed, re-test pending |
| Batching (A10) | Several requests share one pass | Feature observed; verdict pending |
| Speculative decoding (A11) | A small model drafts, the big model checks | Mostly observed; verdict pending |
| Adaptive sharing and Pause sharing (A16) | A Mac can step back when you need it | Partly shown; verdict pending |

All of this is macOS (Apple silicon) only. Windows and Linux packages exist in the source tree but have never been run on real hardware, and they compute on the CPU only.

## Direct route and relay switch (A1, A14)

**What it does.** When two Macs can reach each other, they exchange data directly. If that link is lost, they can move to a relay (a middle server that passes encrypted data along) and keep going. The research UI shows the change as an "Activation path transition": earlier observations are kept and a new "generation" number is recorded.

**What was shown.** Live in Chromium and Firefox, a direct connection (generation 54) and then a switch to the relay (54 to 55). Forged, stale, foreign and unknown-region route records were refused.

**What was not shown.** In WebKit (Safari's engine) the page timed out waiting for the route table. That recheck is still open.

**What it costs.** A relay path is usually slower. The relay can see the size and timing of traffic. The data between the Macs travels over an encrypted connection (QUIC through the iroh library), so by that design the relay does not see its content; that design property was not independently verified for this page. See [PRIVACY](PRIVACY.md).

## Continuing after a Mac leaves (A6, A7)

**What it does.** While one Mac is answering, a second Mac can be kept ready as a standby. If the first Mac goes away mid-answer:

- **A7 (KV continuation).** The KV cache is the model's short-term memory of the conversation so far. A standby Mac that holds a copy continues from that point. In the test, it needed **no re-reading of the prompt** (0 recovery prefill), produced all 32 of 32 tokens, and the text was identical to a run that was never interrupted. A fencing number stops the old Mac from writing after the switch.
- **A6 (recovery).** Without a saved memory copy, a replica Mac redoes the work and carries on. It is slower.

**Failures are honest.** In the tests, three cases ended with an explicit failure instead of a made-up answer: the standby's copy was incompatible, the standby was lagging, or there was no standby. Cancelling after a handover also worked: the request ended as cancelled.

**What was not shown.** Only the two project Macs and the test setups were used. A7 is recorded as a combined result across two candidate builds, because some cases were not rerun after later small changes (the project calls this a composite verdict).

## Honest failures

Across the product, a request ends in one of three ways: completed, cancelled, or failed, with a code. The goal is that a failure is never shown as success. Examples from real runs:

- A request with no standby available (the A7 "no successor" case) ended with a failed state, not a made-up answer.
- A killed remote stage (A5) used to hang for about 90 seconds and then crash the service. That was a real defect found on hardware on 2026-10-20. It is fixed in the final build: the failure now arrives within seconds. The hardware re-test was pending at the time of writing.
- A stopped or draining Mac refuses new work with a stated reason rather than silently dropping it.

See [TROUBLESHOOTING](TROUBLESHOOTING.md) for the exact messages.

## Signed install (A13)

**What it does.** The macOS package has a manifest signed with an Ed25519 key. The installer checks the signature and the app's digest before it writes anything. It installs for your user only, without administrator rights, and installs from a folder you approved once in macOS (Privacy & Security, Open Anyway). It removes the macOS download mark only from the copy it has just verified, so the installed app opens normally. Uninstall keeps your identity and state unless you add a flag.

**Limits.** The app has an ad-hoc Apple signature only. It is **not** Developer ID signed and **not** notarized, so macOS shows a warning. The automated check (signature verified on both Macs) passed on the final build. The hands-on install, restart and uninstall by a person were still pending at the time of writing.

## Seed migration (A9)

**What it does.** The "seed" is the part of a swarm that vouches for who belongs. A9 lets the seed move from its first format to a second one without anyone getting a new identity. Members keep their identity and are adopted by the new seed, then restart.

**What was shown, on a private test seed only.** A committed migration where both members were adopted and restarted; nine tampered or out-of-date inputs were refused; a rejected migration left the old seed in charge; and a Mac's capacity could be withdrawn live in two browser sessions. The production seed and keys were never used. One more confirmation run needs a fresh test seed and is planned.

## Two-stage split (A5)

**What it does.** The model's layers are divided between two Macs according to how much memory each has. One Mac runs the first layers, the other the rest, and data passes between them for every token.

**What was shown.** Ordinary and concurrent requests matched a one-at-a-time run exactly. The stage-loss case failed once on hardware (see Honest failures), was fixed, and awaits its re-test.

## Batching (A10)

**What it does.** When several requests are in flight, a stage can process them together instead of one after another. Requests that arrive late can join. One slow or cancelled request must not hurt the others.

**What was shown.** On the 7B two-Mac route all ten feature checks were observed: two peers, real batches, late arrival, output identical to one-at-a-time, queue overflow refused, cancel and slow-reader isolation, and two browser sessions. These checks have no final judge verdict yet. The gateway queues at most 64 waiting requests by default.

## Speculative decoding (A11)

**What it does.** A small, quick "draft" model guesses several next words. The large "target" model checks them in one pass. Correct guesses are kept; wrong ones are rolled back and the target's word is used. The final text must equal what the target model alone would produce.

**What was shown.** Speculation, output parity with the target alone, rollback, a browser response, and loss of the draft model (the system fell back to target-only) were observed. Loss of the target model was not properly tested yet: the test stopped the wrong process, so it is being rerun.

## Adaptive sharing and Pause sharing (A16)

**What it does.** There are two separate controls. **Adaptive sharing** is automatic and is on by default when you give consent (you can switch it off). Mycelium watches this Mac's memory, CPU, heat and battery. When other apps need resources, it automatically shares less, finishes or hands off work in progress, and pauses sharing. It resumes once the Mac has been healthy for a while. Your own apps always come first. When it acts, the page shows a live line such as "Adaptive sharing: reduced - memory is low" or "Adaptive sharing: paused - the CPU is busy". **Pause sharing** is your manual "I need this Mac" switch: the Mac stops accepting **new** work and frees model memory until you release it. Work already running finishes, is handed to another qualified Mac, or ends with an explicit failure. Neither looks at which apps you run; Adaptive sharing reads only coarse memory pressure and overall CPU.

**Adaptive sharing off.** Nothing is reduced or paused automatically. Your memory limit, the disk-space floor, your power, heat and network limits, **Pause sharing** and **Stop** stay enforced. Pause sharing works with Adaptive sharing on or off.

**Sharing, Rejoining, Paused.** The page shows "Sharing" only once the route to this Mac is ready. After a pause it shows "Rejoining - connecting route" first. A chat request that arrives during Rejoining waits up to 15 seconds for the route instead of being refused.

**Coming back.** The Mac returns only after about two minutes of calm and a fresh check with the swarm. A stop survives an app restart: the Mac restarts "fenced" and does not take work at once.

**What was shown.** On a pre-final build: CPU saturation, loss of the monitoring probe (the app fails closed and recovers), and the manual pause switch passed. The memory-pressure case released model memory and came back, but its check still had a harness problem. The final-build counted run was pending.

**Cost.** A short background check every couple of seconds. This Mac may stop serving while you use other apps.

## Public demo

[Try the demo in your browser](https://mycelium.now/swarms/). The developer's **Mycelium demo** listing can be intermittent. A listing is not proof that a swarm is serving. Prompts go to the swarm operator's entry, and the participating Macs can see the data they need for their work. See [PRIVACY](PRIVACY.md).

## Not available yet

- A fully offline or LAN-only mode (see [PRIVACY](PRIVACY.md)).
- Windows and Linux hardware support.
- Developer ID signing and notarization.
- Using one Mac for the whole job with no peers is a different path and not covered here.

## Sources

- `~/Documents/mycelium-core-program/status/FINAL-REPORT-A-TASKS-TWO-MAC-DRAFT.md` (per-task table, defects 9 and 10, hosts, test seed)
- `~/Documents/mycelium-core-program/status/decisions.log` (2026-10-19 to 2026-10-20 entries for A7, A14, A9, A10, A11, A5, A16 results)
- `docs/router-phase-aware-batching.md` (batching behaviour); `mycelium_request_gateway/service.py` (`max_pending_requests` default 64)
- `mycelium_speculative_decode.py` (target-authoritative speculation, three commit modes)
- `mycelium_a13_macos/web/index.html`, `app.js`, `adaptive_owner.py`, `adaptive_host.py` (A16 texts, `recovery_s` 120)
- `mycelium_a13_macos/web/run.js` ("layers split by memory")
- `mycelium_a13_packaging/installer_core.py`, `mycelium_a13_macos/installer.py`, `scripts/finalize_a13_macos.py` (A13)
- `ui/web/src/features/internetNative/InternetIncidentVocabulary.ts` (path transition wording)
