# Quick start — Mycelium for macOS

This bundle is ad-hoc signed, not signed with an Apple Developer ID and not notarized. macOS will
block **`Install Mycelium.command`** the first time you open it. That is expected: approving that one
script is the only security step, and it is safer than disabling Gatekeeper.

**Do not disable Gatekeeper.** Do not move the app out of this folder before installing.
Nothing here needs an administrator password or a network connection.

---

## 1. Check which Macs this build runs on

The app admits **Apple silicon** (`aarch64`) only, and only these macOS versions:

| macOS version | Admitted |
|---|---|
| **15.6.1** | **yes — this exact version only** |
| 15.7, 15.7.x and any other 15.x | **no** |
| **26 or later** | **yes** |
| anything older (14.x, etc.) | **no** |

15.6.1 is an exact exception, not a "15.6.1 or newer" promise. To check yours:

```sh
sw_vers -productVersion
uname -m
```

If your version is not admitted, the app refuses to start with `unsupported_platform` or
`unsupported_os` and exits with status 2. Nothing is changed on your Mac when it refuses.

## 2. Verify the download before you open anything

Compare the zip against the SHA-256 published beside it:

```sh
shasum -a 256 mycelium-v0.2.0-macos-arm64.zip
```

Do this **before** step 3. If it does not match, delete the zip and download it again; do not open
the folder.

## 3. Approve the install script once

1. Open the unzipped folder in Finder.
2. **Right-click** (or Control-click) `Install Mycelium.command` → **Open**.
3. macOS asks whether you are sure. Choose **Open**.

If macOS only offers *Move to Trash* — or you already double-clicked it once and it was blocked — go to
**System Settings → Privacy & Security**, scroll to the bottom, find the message about
`Install Mycelium.command`, and choose **Open Anyway**. Then run the script again.

Why this is safe: the script's only job is to check this folder, and it refuses rather than guessing.
Before it extends your approval to the app, it verifies the folder against `SHA256SUMS`, verifies every
app file against `APP-FILES.sha256`, confirms that `APP-FILES.sha256` covers every file in the app
nothing more and nothing less, and refuses if the app contains a symlink to an absolute path. Only if
every check passes does it remove the download mark from *this app folder and nothing else*.

## 4. Install

The script you approved now runs the app's own installer:

```sh
./Install\ Mycelium.command
```

The installer re-verifies the Ed25519-signed `installation-package-manifest.json` and the bundle
digest against those same bytes before it writes anything, then clears the download mark on the
installed copy only. It installs **for the current user only** — no administrator rights, no changes
outside your home folder — and links `~/Applications/Mycelium.app` to the installed copy.

On success it prints:

```
Mycelium is installed for this user: open 'Mycelium' in the Applications folder of your home folder.
```

If it refuses, it prints `Mycelium is not installed: …` with the reason. **A refusal changes nothing.**
The most common causes are a modified file in the folder (re-download) and running the script from
somewhere other than the folder you unzipped.

## 5. First launch

Open **Mycelium** from the **Applications** folder inside your home folder (not the system
`/Applications`). The first launch is model-free: it checks the installed files and reports its status.
The model is downloaded separately when you start a run, and it is pinned by revision and digest.

If macOS asks whether Mycelium may accept incoming network connections, or may connect to devices on
your local network, choose **Allow** — a swarm needs both. If you deny them, joining another Mac fails
with `pairing_window_unreachable` or `control_connect_timeout`; fix it in
**System Settings → Privacy & Security → Local Network**.

## Uninstalling

```sh
~/Applications/Mycelium.app/Contents/MacOS/mycelium --uninstall
```

This keeps your identity and your membership state. To remove those too:

```sh
~/Applications/Mycelium.app/Contents/MacOS/mycelium --uninstall --delete-identity-and-state
```

`--delete-identity-and-state` is not reversible: your node identity and any swarm membership are
destroyed, and other members will treat you as a new node if you reinstall.

## What to have ready

- **AC power** on both Macs, lids open, Low Power Mode off. On battery the resource policy cancels work.
- **The same private local network** for both Macs. No VPN and no overlay network (Tailscale, etc.):
  those change the addresses the swarm admits and are not qualified, and a member reached over one is
  refused.
- **Auto-updates off** during a run, so an OS update cannot move the machine off an admitted version.
- Disk: about **10 GiB** free on a member, about **35 GiB** on the Mac running the model.

## If something goes wrong

| Message | What it means |
|---|---|
| `unsupported_os` / `unsupported_platform` | Your macOS version or CPU is not admitted (see step 1). |
| `a file in this folder does not match SHA256SUMS` | The download is damaged or was modified. Delete it and re-download. |
| `the app holds files that APP-FILES.sha256 does not list` | The folder was altered after download. Re-download; do not "fix" it. |
| `the download mark could not be removed from the app` | Report this. Do not disable Gatekeeper to work around it. |
| `pairing_window_unreachable` / `control_connect_timeout` | Local-network permission was denied, or the two Macs are not on the same private network. |

Every refusal exits non-zero and leaves your machine unchanged. If a message is not on this list,
copy it exactly — it is the refusal code the developers search for.
