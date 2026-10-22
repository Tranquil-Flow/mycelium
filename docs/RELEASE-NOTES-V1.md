# Mycelium v0.1.1 — macOS installation and reliability update

Mycelium splits one language model across several computers, so machines that
could not run the model alone can serve it together. This preview updates the
macOS installer and fixes reliability problems in longer-running swarms. It has
not been tested on a wide range of machines.

Licence: **AGPL-3.0-only** (see `LICENSE`).

## What changed since v0.1.0

- Fixed a crash when a MacBook joins a swarm (duplicate MLX libraries in the app)

- **Installation on macOS 26.** Approve `Install Mycelium.command` in Privacy &
  Security. The script checks the downloaded files before allowing the bundled
  app to run. The installer verifies a complete staged copy before moving it into
  place, and prevents two installs from running at once. An interrupted install
  no longer leaves an empty version folder that blocks the next attempt.
- **Safer first launch.** Open the installed app in your home Applications folder,
  not the app inside Downloads. Launches from an uninstalled or translocated copy
  are refused with an explanation instead of starting a temporary session.
- **Less health-check traffic during chat.** Token progress counts as a sign that
  a worker is alive; health probes no longer pile up while it is answering.
- **Clearer stops.** A blocked worker stops the swarm from claiming it is serving.
  When a run reaches its execution time limit, waiting chats receive an explicit
  end reason instead of hanging until the workers disappear.
- **Bounded status replies.** Recent request histories are kept within a fixed
  response budget, so status reports do not grow indefinitely across chats.
- **More reliable worker startup and tests.** Workers retain a compatible Python
  minor version after Homebrew changes its `python3` target. Health-probe deadlines
  use the central registry, and pump-recovery tests wait for native dispatch
  rather than relying on thread timing.

## What works

One Apple Silicon Mac creates a swarm and a second joins with a short-lived,
single-use code. The owner approves the joining Mac by fingerprint. Each Mac
fetches its share of the model with hash verification and resumable downloads.
Once the route is ready, answers stream token by token. A swarm can optionally
be listed on <https://mycelium.now> for other people to find and chat with it.

## Known limits

- **Unsigned by Apple and not notarized.** The app has an ad-hoc signature and an
  Ed25519-signed installation manifest, but no Apple Developer ID. Gatekeeper
  blocks the downloaded installer until you approve it. Do not disable Gatekeeper.
- **Standard users need an admin for first-open approval.** The installer itself
  does not use `sudo` and writes only inside your home folder; macOS approval of
  an un-notarized download can require an administrator.
- **Apple Silicon Macs only** (M1 or newer; macOS 15.6.1 or later). Intel Macs are
  refused. Linux and Windows installers in the source tree are not qualified for
  this release. Only two-Mac swarms have been exercised end to end.
- **Demo speed is still limited.** The small Qwen2.5-0.5B demo can pause between
  tokens. These reliability fixes are not a promise of fast responses; speed
  depends on the Macs, network and other work running on them. The small model's
  answers can be wrong.
- **Keep clocks synchronized.** Enable automatic time on both Macs. More than
  30 seconds of difference can make joins or commands fail.
- **Serving peers can see prompts.** Mycelium does not hide your prompt or
  intermediate data from the machines doing the work. Read `docs/user/PRIVACY.md`.

## Install on macOS 26

1. Download `mycelium-0.1.1-macos-arm64.zip` and its `.sha256` file from this release.
2. In Terminal, check the downloaded zip:

   ```sh
   cd ~/Downloads
   shasum -a 256 -c mycelium-0.1.1-macos-arm64.zip.sha256
   ```

   It must say `OK`, and the checksum must match the value below. A checksum
   establishes byte identity, not a security audit.
3. Double-click the zip. It unpacks to `mycelium-0.1.1-macos-arm64`. Check the app:

   ```sh
   /usr/bin/codesign --verify --deep --strict ~/Downloads/mycelium-0.1.1-macos-arm64/"Mycelium A13 Demo.app"
   ```

   Exit status 0 means the ad-hoc signature is intact; it is not Apple approval.
4. In the extracted folder, double-click **Install Mycelium.command**. If macOS
   says it was not opened, click **Done**, then go to **System Settings → Privacy
   & Security → Security → Open Anyway**. Confirm with your password or Touch ID,
   then click **Open Anyway** in the confirmation dialog.
5. Wait for `Mycelium is installed for this user`. The script verifies file
   checksums before clearing the download mark on the bundled app; the installer
   verifies the signed manifest and staged copy before clearing its mark. Do not
   run `xattr` yourself or turn Gatekeeper off. On older macOS, right-click →
   **Open** on the script may also be available.
6. Open `~/Applications/Mycelium A13.app` and follow `docs/user/QUICKSTART-MACOS.md`.
   Do not open the app directly inside Downloads.

If installation is refused, retain the short error code and consult
`docs/user/TROUBLESHOOTING.md`. Uninstall keeps your identity and settings unless
you explicitly ask to remove them.

## Checksum

```text
SHA-256 (mycelium-0.1.1-macos-arm64.zip): 11f289b3b271521d8f296bbada281adbf8db630b49a1578fe49e606b1fafe4d5
```

## Source and previous release

Build and test instructions are in `README.md`. The previous preview and its
notes remain at tag `v0.1.0`; that tag is not moved. Please report problems as
GitHub issues and include the short error code, not secrets or private logs.
