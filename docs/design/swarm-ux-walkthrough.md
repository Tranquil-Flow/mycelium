<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Swarm-creation UX walkthrough (macOS app)

Walk of create → invite → join → first model → leave, read as a first-time user from
`mycelium_a13_macos/web/{index.html,app.js,run.js,swarm.js}` and the handlers they call
(`app.py` `/api/create`, `/api/join/*`, `/api/pair/*`, `/api/network/start`, `/api/model/*`;
`acquisition.py`; `demo_chain.py`). Line numbers refer to base commit `a0b868d4`.
Status: **FIXED** (this lane, tests in `tests/a13_macos/test_swarm_ux_copy.py`) or **OPEN**
(needs a behaviour/protocol decision, not a text change).

## What the code actually does for the first model (ground truth)

* `POST /api/model/download` (`app.py:1188`) starts `SelectedModelAcquisition.start`
  (`acquisition.py:1038` `_execute`). Its byte source is the pinned Hugging Face origin
  (`_HF_ORIGIN`, `acquisition.py:83`); a peer byte source is attached only on joined Macs
  (`demo_target.py:525`).
* The request's `max_download_bytes` bounds what would be fetched *over the network*
  (`acquisition.py:1058-1069`): files already present and digest-verified (a retained or
  LAN-seeded snapshot) count as downloaded. If the rest does not fit the limit the attempt
  stops with `download_limit_exceeded` before requesting a byte.
* So the app **does** download model bytes from Hugging Face whenever the user authorises a
  limit that covers them. It does not "only ever fetch metadata". The default limit in the
  UI is 11,535,382 bytes only for the pre-seeded 7B model; every other model defaults to the
  full snapshot (`app.js:315`).

## Friction list

### First model download
| # | Where | Problem | Status |
|---|---|---|---|
| 1 | `index.html:35`, `app.js:309` | Card says only small metadata files are fetched, model bytes come from a local seed, and the app "refuses instead of downloading"; `run.js:19` then tells the user to press *Download / resume source*. Contradicts `acquisition.py:1058-1069`: raising the limit downloads from Hugging Face. Static HTML also claimed the seed story for every model before JS replaced it. | FIXED |
| 2 | `run.js:19` | `owner_model_not_prepared` says "Download … first" but not that the authorisation box must be ticked first or that the user waits for "prepared". | FIXED |
| 3 | `app.js:95` | *Download / resume source* disabled with no reason (needs running + enrolled + consent box). | FIXED (`download-hint`) |
| 4 | `app.js:100` | Failure shows `Source: failed · download limit exceeded` / `insufficient disk` / `network error`: raw code, no next action. | FIXED |
| 5 | `index.html:35` | Download limit is a raw byte count, `max` computed by script; user cannot tell what number to type. | OPEN (needs a GB-based control) |
| 6 | `demo_chain.py:418` | *Start demo* with no prepared model waits out the whole model budget before reporting `owner_model_not_prepared`; the button could refuse at once. | OPEN (behaviour) |
| 7 | `app.js:94-95`, `index.html:35` | Section "Inference" is below Devices/Join; first-time owner has no ordered checklist (create → relay → invite → download → start). | OPEN |

### Create
| # | Where | Problem | Status |
|---|---|---|---|
| 8 | `index.html:4,8`, `app.js:47` | Jargon: "authority", "seed", "enroll", "Create authority & enroll this Mac", "Target role". | OPEN (copy decision; browser tests select `#create`) |
| 9 | `app.js:70-71`, `index.html:8` | After creating, *Create* and *Join* stay disabled with no sentence (installation-role line only). | OPEN |
| 10 | `index.html:15`, `app.js:323` | "Start public relay (before creating the swarm)": the button is hidden once the swarm exists and `/api/network/start` then refuses (`app.py:1093-1095`, `network_requires_fresh_owner`). An owner who skipped it has no way to get join codes. | Dead end OPEN (needs protocol change); explained FIXED (#11) |
| 11 | `app.js:324` | *Invite a Mac* disabled with no explanation (no swarm, or no relay). | FIXED (`invite-hint`, points at manual pairing) |
| 12 | `app.js:362` | Relay confirm dialog: "Cloudflare quick tunnel … inference stays on Iroh": jargon. | OPEN |

### Invite / join
| # | Where | Problem | Status |
|---|---|---|---|
| 13 | `app.js:328` | *Join with code* disabled with no reason (needs visibility box, standing-consent box); the visibility box had no change listener so the button lagged up to 5 s. | FIXED (`join-hint`, listener) |
| 14 | `index.html:10-12,13-25` | Two parallel join paths (manual request/encrypted-invitation JSON paste; join code/QR) with no guidance on which to use. | OPEN |
| 15 | `app.js:141,227,385` | Any refused action shows the code in words ("Action not completed: membership required."): no sentence, no next step. | FIXED for the codes a person can hit; unknown codes still show the code in words |
| 16 | `run.js:35`, `swarm.js:23` | Unknown error fell back to the bare code (`return code`). | FIXED (prefixed sentence) |
| 17 | `app.js:330` | Waiting state says "Check that the owner sees this fingerprint" with no timeout or what to do if nothing arrives (Cancel button appears but is unexplained). | OPEN |
| 18 | `run.js:110-112` | Target states "native hello", "Preparing" are jargon for the user watching. | OPEN |

### Leave
| # | Where | Problem | Status |
|---|---|---|---|
| 19 | `app.js:61,76`, `index.html:44` | A joined Mac has no "Revoke this Mac" (button disabled, checkbox hidden) yet the Lifecycle card is titled "Pause, restart, or leave" and speaks of revoking. No visible way to leave. | Wording FIXED (`leave-hint` explains withdraw + stop, owner revokes); a real self-leave is OPEN (protocol) |
| 20 | `app.js:115` | Owner revoke of a member uses `window.confirm`; fine, but a revoked member sees no state sentence on its own side. | OPEN |

### Other
| # | Where | Problem | Status |
|---|---|---|---|
| 21 | `index.html:9` ladder (`app.js:78-88`) | "Generation", "Lease", "Load & challenge" shown to a new user unexplained. | OPEN |
| 22 | `index.html:3` | Header says "macOS A13 hackathon demo" (internal gate id). | OPEN |
| 23 | `swarm.js:75` | Owner Publish card appears only after the swarm exists, but the directory needs the relay from #10. | covered by #11 |

## Fixes in this lane
1. #1/#2: acquisition text now matches the code path (Hugging Face download, seed counted
   as already present, limit semantics) in `index.html` and `app.js`; `run.js` hint names the
   authorisation step.
2. #3, #11, #13, #19: `download-hint`, `invite-hint`, `join-hint`, `leave-hint` say why a control
   is unavailable and what to do.
3. #4, #15, #16: `HUMAN_ERRORS` in `app.js` gives sentences for the codes a first-time user can
   hit; `run.js`/`swarm.js` fallbacks no longer print a bare code.

No protocol, authorization or security behaviour changed; only text and enabled-state
explanations.
