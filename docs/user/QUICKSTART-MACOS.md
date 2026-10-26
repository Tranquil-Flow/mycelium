# Quickstart: Mycelium on two Macs

This page takes you from a downloaded package to a first chat answer served by two Macs. It is written for people who do not read code.

**Read this first.**

- This is **Mycelium 0.1.4**, an early macOS release with fast-mode lease renewal, automatic run rollover and a tested starter model in the GUI. Hardware support is limited; broad compatibility is not claimed. See the [release notes](../RELEASE-NOTES-v0.1.4.md), including upgrade steps and the fast-mode failure limits.
- The package is **not** signed with an Apple Developer ID and is **not** notarized. macOS will block the installer script once. See "Step 3".
- Download the zip and checksum from the GitHub release page. Check them before approving the installer.
- **Apple silicon only** (M1 or newer, macOS arm64). Intel Macs are refused.
- **Windows and Linux:** installers exist in the source tree, but they have never been run on real hardware. Treat this guide as macOS-only for now.

## What you need

- Two Apple silicon Macs on the same Wi-Fi (the project's own runs used a phone hotspot).
- The package `mycelium-0.1.4-macos-arm64.zip` and its SHA-256 value from the release page (or the one the owner sent you).
- Enough free disk space for the model. The app checks disk and memory and will refuse with a message if a Mac cannot hold its share (see [TROUBLESHOOTING](TROUBLESHOOTING.md)).
- An internet connection. Joining uses a public HTTPS relay, by default a temporary Cloudflare tunnel (see [PRIVACY](PRIVACY.md)). An owner-supplied HTTPS address is also supported. There is no supported, fully offline two-Mac app flow.
- The installer itself does not need administrator rights. A standard (non-admin) user needs an administrator to approve the first open of this un-notarized download.

## Step 1. Check the file

In Terminal:

```sh
shasum -a 256 ~/Downloads/mycelium-0.1.4-macos-arm64.zip
```

The output must match the value the owner sent you. If it does not, stop and ask for the file again. A matching checksum only proves the file is the same bytes; it does not prove the software is safe.

## Step 2. Unzip and check the signature

Double-click the zip in Finder. Then in Terminal:

```sh
/usr/bin/codesign --verify --deep --strict "$HOME/Downloads/mycelium-0.1.4-macos-arm64/Mycelium A13 Demo.app"
```

It should print nothing and exit with status 0.

## Step 3. Install

macOS 26 blocks the installer script once and does not offer **Open** on a right-click. One **Open Anyway** is enough; follow these steps exactly:

1. In the extracted folder, double-click **Install Mycelium.command**. macOS says **"Install Mycelium.command" Not Opened**. Click **Done**.
2. Open **System Settings → Privacy & Security** and scroll down to **Security**. Next to *"Install Mycelium.command" was blocked to protect your Mac* click **Open Anyway** and confirm with your password or Touch ID.
3. macOS asks **Open "Install Mycelium.command"?** Click **Open Anyway**. Terminal opens and runs the script.
4. Wait for the last line: `Mycelium is installed for this user`. If the last line says `Mycelium is not installed`, nothing was changed; read the reason above it and see [TROUBLESHOOTING](TROUBLESHOOTING.md).

That is all: there is **no second block**. Before it lets the app start, the script checks every file in the folder against `SHA256SUMS` and every file of the app against `APP-FILES.sha256`, then removes the download mark from that app only. The installer then checks the Ed25519-signed manifest and the app's digest again, and installs.

- Do **not** turn Gatekeeper off. Do **not** run `xattr` or `spctl` yourself. The script does the one narrow, checked removal; nothing else is touched.
- Do **not** open the app inside the downloaded folder. If you do, it shows "Mycelium is not installed yet" and starts nothing; run **Install Mycelium.command** (steps 1 to 3).
- On macOS 15 and earlier, **Right-click → Open** on the script also works.
- If macOS refuses completely, stop and tell the owner what it said. That is a finding, not something to work around.

The installer needs no administrator rights and no network. It writes only to your user folders:

- program: `~/Library/Application Support/Mycelium/programs/<version>`
- app link: `~/Applications/Mycelium A13.app`
- your identity and settings: `~/Library/Application Support/Mycelium A13 macOS Demo`

If it fails, it prints a short JSON line ending in `"refused": "<code>"`, never a stack trace. See [TROUBLESHOOTING](TROUBLESHOOTING.md). The reasoning behind the steps is in [MACOS-INSTALL-FLOW](../design/MACOS-INSTALL-FLOW.md).

## Step 4. First run on the owner Mac

Open **~/Applications/Mycelium A13.app**. It opens a private page in your default browser. The page only talks to the app on your own Mac. Click **Open private demo** if it asks.

1. **Read and tick the consent box.** It says other Macs in your swarm can see the prompts, outputs or intermediate data they need for their work. Mycelium does not hide your prompt from the Macs that serve it.
2. **Choose your limits before creating or joining:** power (AC only, or battery allowed), network (unmetered only, or metered allowed), and storage. Read the selected model's displayed storage requirement; the download size alone is not enough. The defaults are AC only and unmetered only. Changing a selector later does not replace saved consent.
3. **Start the public relay when you want other Macs to join.** In the card "Invite or join with a code", press **Start public relay**. This starts a temporary Cloudflare tunnel. You can do it before or after creating the swarm. You can ask for join codes at any time: a code asked for before the relay starts shows "ready to share once the relay starts" and becomes redeemable the moment the relay is up. Joined Macs can only serve while the relay is running ("N members joined, waiting for relay to serve").
4. Press **Create authority & enroll this Mac**. This creates your swarm identity on this Mac. No account and no organizer credentials are needed.
5. **Get the model ready on this Mac.** Keep **Qwen2.5-0.5B-Instruct** (the tested starter) selected in **Model**. It is the default for a fresh setup and downloads about 1 GB if not cached. Existing installations may retain their previous model. Under **Inference**, authorize the source fetch, check **Maximum bytes to fetch this attempt**, press **Download / resume source**, and wait for `prepared`. A download does not start inference.

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

On the owner Mac, in "Run the model across the joined Macs", choose **Fast mode** on or off, then press **Start demo**. The switch is off by default and applies to the next run; stop an active run before changing it. Fast mode has no mid-request recovery if a stage fails. No environment variable is needed for this GUI setup. The app runs these steps for you and shows each one:

1. Both Macs report in.
2. The route is built: layers of the model are split between the Macs by memory.
3. Each Mac fetches the files it needs. Small models, including the starter model, use a whole source snapshot on each Mac; larger models can use assigned slices.
4. The route is checked ("qualified") before anything is served.
5. The chat opens.

At least **two different physical Macs** are required. Two copies of the app on one Mac do not count.

**Time limit per run** means each Mac agrees to lend compute for at most the chosen number of minutes per run, 30 by default. In v0.1.4 the app starts a fresh run automatically at the limit, under the same saved consent. This rollover does not keep an expired temporary relay running. See [relay and run limits](../FRIEND-SWARM.md#run-time-limits-and-automatic-rollover).

Keep both apps open. Joined Macs reconnect and re-report automatically after the owner app restarts, once the owner is reachable at the same address. If a temporary relay address changes, old links can stop working. After restarting the joined Mac's own app, press **Start / reconnect**. An interrupted answer is not resumed.

Nothing is served until the route passes its start-up check. While it is checking, the chat says: "Warming up: the swarm is qualifying its route. Chat opens when it is ready." When it is ready the chat shows `Serving <model>` and, after each answer, a line like `Served by: ... → ...` naming the Macs that did the work.

## What you will see

- Step list with each step marked as it completes.
- On the owner page, the "Adaptive sharing" card (on by default once you consent). See [WHAT-MYCELIUM-DOES](WHAT-MYCELIUM-DOES.md).
- If a Mac leaves during an answer, either the answer continues or it ends with an explicit failure. It never ends with a fake success.

## Use your own address (advanced, optional)

By default, letting another Mac join uses a temporary Cloudflare address that changes every
time the relay starts. If you own a domain you can use a fixed address instead, for example
`https://demo.mycelium.now`, so join links and the swarm listing keep working after a restart.

1. Point the hostname at this Mac yourself. Mycelium does not do this for you. A Cloudflare
   named tunnel is the simplest way (example below).
2. In the app, under **Invite a Mac**, open **Use my own address**. Enter the address
   (`https://` and a domain name, no port or path) and a local port such as `8787`. Tick the
   Cloudflare box only if a Cloudflare tunnel serves the address. Start the relay.
3. Open an invitation and send the **join link** (the short typed code works only for the
   temporary addresses).

**The Cloudflare named-tunnel example (`demo.mycelium.now`)**

This is what you run on the owner Mac. It is your own Cloudflare setup, separate from
Mycelium; Mycelium never reads or writes it, and no secret belongs in this document or in a
message to anyone. It needs a free Cloudflare account that manages the `mycelium.now` zone.

```sh
cloudflared tunnel login                          # opens your browser; stores a cert in ~/.cloudflared
cloudflared tunnel create mycelium-demo           # prints the tunnel UUID and writes <UUID>.json (a secret)
cloudflared tunnel route dns mycelium-demo demo.mycelium.now
```

`~/.cloudflared/config.yml` (replace `<TUNNEL-UUID>` and `<you>`; pick the same port you type in
the app):

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /Users/<you>/.cloudflared/<TUNNEL-UUID>.json
protocol: http2
ingress:
  - hostname: demo.mycelium.now
    service: http://127.0.0.1:8787
  - service: http_status:404
```

Then `cloudflared tunnel run mycelium-demo` and keep it running while the swarm is up. In
Mycelium press **Use my own address**, enter `https://demo.mycelium.now`, port `8787`, tick
"served by a Cloudflare tunnel", and start the relay.

Rules the app enforces: `https://` plus a real DNS name only (no port, path, login details,
IP number or `.local` name), and a local port from 1024 to 65535. The typed short code
(`quiet-river-apple-stone ABCD-EFGH-JKLM`) still works only for `*.trycloudflare.com`; for your
own address send the **join link**, which carries the address and the owner's key digest.

When the relay stops and restarts on the same configured address, the swarm identity,
members and listing are retained. Unexpired join links still use the same address;
expired or used invitations still need replacing. Press
**Use my own address** again, or restart the relay with the same address (the app remembers it),
and nothing has to be republished. The app only opens the local port; if the tunnel is not
running, visitors see a Cloudflare error page and the app still works locally.

Who can see what: whichever front serves your hostname terminates HTTPS and sees control
traffic, visitor IP addresses and timing, and web chat prompts and replies. With a Cloudflare
tunnel that is Cloudflare; with your own server it is you. Only inference between the Macs
uses Iroh. The public chat page and swarm entry follow exactly the same checks on your address
as on a quick tunnel (kill switch, access mode, daily caps, per-visitor rate limit). The
per-visitor rate limit reads Cloudflare's visitor-address header only when you tick the
Cloudflare box; for any other front all visitors share one limit.

## How to stop

From gentlest to most final:

| What you want | What to press | What happens |
|---|---|---|
| Stop the run | **Stop** (in the run card) | The run stops. Joined Macs stop when the owner stops. |
| Stop this Mac serving, keep everything | **Stop serving now** (Adaptive sharing card) | This Mac takes no new work and frees model memory. **Allow serving again** resumes. |
| Pause, keep identity | **Stop & retain private state** | Membership renewal pauses. **Start / reconnect** resumes with the same identity. |
| Close the app | **Quit launcher** | The app exits and removes its temporary launch page. Reopening makes a new browser authorization. |
| Leave a swarm | **Leave smoothly** or **Instant leave** (joined Mac), or **Revoke this Mac** (owner, needs its checkbox) | A joined Mac that leaves stops serving and withdraws its consent; the owner then sees it drop out. Leaving does **not** let the Mac walk back in: the owner revokes it, and the Mac pairs again with a **new join code**. Revoke ends membership. It is different from deleting local files. |
| Uninstall | Quit, then run the app's installer with `--uninstall` | Removes the app link and installed version. Identity and state **stay** unless you add `--delete-identity-and-state`. |

Uninstall example (the executable inside the app link):

```sh
"$HOME/Applications/Mycelium A13.app/Contents/MacOS/Mycelium A13 Demo" --uninstall
```

`--install-status` shows what is installed and `--rollback` returns to the previous installed version. Deleting your state folder starts you again as a new member; it does not recover the old identity. No secure-erase claim is made.

## Sources

- `docs/handover/A13-MACOS-QUICKSTART.md` (earlier preview notes: Gatekeeper, state folder, pairing)
- `mycelium_a13_macos/web/index.html` (button names, consent text, policy options, "Start public relay")
- `mycelium_a13_macos/web/run.js` (run steps; `owner_model_not_prepared` message)
- `mycelium_a13_macos/web/chat.js` (chat state messages, "Served by")
- `mycelium_a13_macos/app.py` (`relay_required`, `network_requires_fresh_owner`), `mycelium_a13_macos/adaptive_owner.py`
- `mycelium_a13_macos/__main__.py` (`--install`, `--uninstall`, `--rollback`, `--install-status`), `mycelium_a13_packaging/installer_core.py` (`--delete-identity-and-state`, `refused` output), `mycelium_a13_macos/installer.py` (quarantine cleared on the verified copy, paths), `mycelium_a13_macos/launch_location.py`
- `scripts/finalize_a13_macos.py` (what the bundle contains and what `Install Mycelium.command` checks; ad-hoc signature, not notarized)
