# Quickstart: Mycelium on two Macs

This page takes you from a downloaded package to a first chat answer served by two Macs. It is written for people who do not read code.

**Read this first.**

- This is a **preview** (`0.1.0-preview.2`). It has been tested on the project's own two Macs, not on a wide range of machines.
- The package is **not** signed with an Apple Developer ID and is **not** notarized. macOS will warn you. See "Step 3".
- There is **no public download**. The package is handed to you by the owner (a file plus a SHA-256 checksum).
- **Apple silicon only** (M1 or newer, macOS arm64). Intel Macs are refused.
- **Windows and Linux:** installers exist in the source tree, but they have never been run on real hardware. Treat this guide as macOS-only for now.

## What you need

- Two Apple silicon Macs on the same Wi-Fi (the project's own runs used a phone hotspot).
- The package `mycelium-0.1.0-preview.2-macos-arm64.zip` and the SHA-256 value the owner sent you.
- Enough free disk space for the model. The app checks disk and memory and will refuse with a message if a Mac cannot hold its share (see [TROUBLESHOOTING](TROUBLESHOOTING.md)).
- An internet connection. A join between two Macs goes through a temporary Cloudflare tunnel (see [PRIVACY](PRIVACY.md)). A fully offline mode is **not released yet**.
- A standard (non-admin) user account is enough. The installer does not need administrator rights.

## Step 1. Check the file

In Terminal:

```sh
shasum -a 256 ~/Downloads/mycelium-0.1.0-preview.2-macos-arm64.zip
```

The output must match the value the owner sent you. If it does not, stop and ask for the file again. A matching checksum only proves the file is the same bytes; it does not prove the software is safe.

## Step 2. Unzip and check the signature

Double-click the zip in Finder. Then in Terminal:

```sh
/usr/bin/codesign --verify --deep --strict "$HOME/Downloads/mycelium-0.1.0-preview.2-macos-arm64/Mycelium A13 Demo.app"
```

It should print nothing and exit with status 0.

## Step 3. Install

Right-click **Install Mycelium.command** and choose **Open**. If macOS blocks it, open **System Settings → Privacy & Security** and choose **Open Anyway**.

- Do **not** turn Gatekeeper off. Do **not** remove the quarantine flag. The installer keeps it on purpose.
- If macOS refuses completely, stop and tell the owner what it said. That is a finding, not something to work around.

The installer first checks an Ed25519-signed manifest and the app's digest, then writes only to your user folders:

- program: `~/Library/Application Support/Mycelium/programs/<version>`
- app link: `~/Applications/Mycelium A13.app`
- your identity and settings: `~/Library/Application Support/Mycelium A13 macOS Demo`

If it fails, it prints a short JSON line ending in `"refused": "<code>"`. See [TROUBLESHOOTING](TROUBLESHOOTING.md).

## Step 4. First run on the owner Mac

Open **~/Applications/Mycelium A13.app**. It opens a private page in your default browser. The page only talks to the app on your own Mac. Click **Open private demo** if it asks.

1. **Read and tick the consent box.** It says other Macs in your swarm can see the prompts, outputs or intermediate data they need for their work. Mycelium does not hide your prompt from the Macs that serve it.
2. **Choose your limits:** power (AC only, or battery allowed), network (unmetered only, or metered allowed), and storage (1, 2, 4 or 23 GB). The defaults are AC only and unmetered only.
3. **Start the public relay first.** In the card "Invite or join with a code", press **Start public relay (before creating the swarm)**. This starts a temporary Cloudflare tunnel. The order matters: if you create the swarm first, the app refuses to add a relay afterwards (`network_requires_fresh_owner`).
4. Press **Create authority & enroll this Mac**. This creates your swarm identity on this Mac. No account and no organizer credentials are needed.
5. **Get the model ready on this Mac.** If you press Start without a prepared model, the app says: "Download the selected model on this Mac first (Inference → Download / resume source), then press Start again."

## Step 5. Add the second Mac

On the owner Mac:

1. Pick a code lifetime (5, 10 or 15 minutes) and press **Invite a Mac**. A code and a QR code appear. Each code admits **one** Mac and expires.

On the second Mac:

1. Install the same package (Steps 1 to 3) and open the app.
2. Tick the consent box and choose its limits.
3. Paste the join link, or type the relay name and code (for example `quiet-river-apple-stone ABCD-EFGH-JKLM`) into **Join code or link**.
4. Choose a memory limit and a time limit per run. Tick the **standing consent** box. It means this Mac accepts this owner's work automatically within those limits until you withdraw.
5. Press **Join with code**.

Back on the owner Mac you see the second Mac's fingerprint, chip, memory and measured link. Approve it with one click.

## Step 6. Run the model

On the owner Mac, in "Run the model across the joined Macs", press **Start demo**. The app runs these steps for you and shows each one:

1. Both Macs report in.
2. The route is built: layers of the model are split between the Macs by memory.
3. Each Mac fetches only its own slice.
4. The route is checked ("qualified") before anything is served.
5. The chat opens.

At least **two different physical Macs** are required. Two copies of the app on one Mac do not count.

Nothing is served until the route passes its start-up check. While it is checking, the chat says: "Warming up: the swarm is qualifying its route. Chat opens when it is ready." When it is ready the chat shows `Serving <model>` and, after each answer, a line like `Served by: ... → ...` naming the Macs that did the work.

## What you will see

- Step list with each step marked as it completes.
- On the owner page, the "Adaptive sharing" card (on by default once you consent). See [WHAT-MYCELIUM-DOES](WHAT-MYCELIUM-DOES.md).
- If a Mac leaves during an answer, either the answer continues or it ends with an explicit failure. It never ends with a fake success.

## How to stop

From gentlest to most final:

| What you want | What to press | What happens |
|---|---|---|
| Stop the run | **Stop** (in the run card) | The run stops. Joined Macs stop when the owner stops. |
| Stop this Mac serving, keep everything | **Stop serving now** (Adaptive sharing card) | This Mac takes no new work and frees model memory. **Allow serving again** resumes. |
| Pause, keep identity | **Stop & retain private state** | Membership renewal pauses. **Start / reconnect** resumes with the same identity. |
| Close the app | **Quit launcher** | The app exits and removes its temporary launch page. Reopening makes a new browser authorization. |
| Leave a swarm | **Withdraw standing consent** (joined Mac) or **Revoke this Mac** (needs its checkbox) | Revoke ends membership. It is different from deleting local files. |
| Uninstall | Quit, then run the app's installer with `--uninstall` | Removes the app link and installed version. Identity and state **stay** unless you add `--delete-identity-and-state`. |

Uninstall example (the executable inside the app link):

```sh
"$HOME/Applications/Mycelium A13.app/Contents/MacOS/Mycelium A13 Demo" --uninstall
```

`--install-status` shows what is installed and `--rollback` returns to the previous installed version. Deleting your state folder starts you again as a new member; it does not recover the old identity. No secure-erase claim is made.

## Sources

- `docs/handover/A13-MACOS-QUICKSTART.md` (earlier preview notes: Gatekeeper, state folder, pairing)
- `mycelium_a13_macos/web/index.html` (button names, consent text, policy options, "Start public relay (before creating the swarm)")
- `mycelium_a13_macos/web/run.js` (run steps; `owner_model_not_prepared` message)
- `mycelium_a13_macos/web/chat.js` (chat state messages, "Served by")
- `mycelium_a13_macos/app.py` (`network_requires_fresh_owner`), `mycelium_a13_macos/adaptive_owner.py`
- `mycelium_a13_macos/__main__.py` (`--install`, `--uninstall`, `--rollback`, `--install-status`), `mycelium_a13_packaging/installer_core.py` (`--delete-identity-and-state`, `refused` output), `mycelium_a13_macos/installer.py` (quarantine kept, paths)
- `scripts/finalize_a13_macos.py` (what the bundle contains; ad-hoc signature, not notarized)
- `/Users/evinova-self/Documents/mycelium-core-program/status/A13-OWNER-CHECKLIST-20261020.md` (install, verify, restart, uninstall order; package name and version)
- `/Users/evinova-self/Documents/mycelium-core-program/status/FINAL-REPORT-A-TASKS-TWO-MAC-DRAFT.md` (two-Mac test setup)
