# Mycelium v0.1.0 — first public preview

Mycelium splits one language model across several computers, so machines that
could not run the model alone can serve it together. This is a preview: it works
on the project's own Macs and has not been tested on a wide range of machines.

Licence: **AGPL-3.0-only** (see `LICENSE`).

## What works

- **Two-Mac swarm.** One Mac creates a swarm and a second Mac joins it with a
  short-lived, single-use code. The layers of the model are split between the two
  Macs by available memory.
- **Join.** The owner shows a join link and QR code; the second Mac pastes it.
  The owner approves the new Mac by its fingerprint.
- **Model download.** Each Mac fetches only the slice of the model it serves.
  Downloads are checked against pinned hashes and can resume.
- **Serve and chat.** The route is checked before anything is served. Chat
  answers stream token by token in the app.
- **Public directory.** A swarm can be listed on <https://mycelium.now> so
  others can find it and chat with it through the swarm's public entry.
- **Your own relay origin.** The owner can put the swarm's public entry on a
  hostname they control instead of a temporary tunnel address.
- **Pause and leave.** A Mac can pause sharing, leave, and re-enter. Its identity
  is kept across restarts and uninstall unless you ask for deletion.

## Known limits

- **Not notarized.** The app is ad-hoc signed, not signed with an Apple
  Developer ID. macOS Gatekeeper warns on first open. Use right-click → **Open**,
  then **System Settings → Privacy & Security → Open Anyway** if needed. Do not
  turn Gatekeeper off.
- **Standard users need an admin once.** On a standard (non-admin) account, an
  administrator has to approve the first open of the un-notarized app. The app
  itself never asks for `sudo` and writes only inside your home folder.
- **Small public demo model.** The public demo serves **Qwen2.5-0.5B**. It is
  small on purpose; answers are short and often wrong in the way small models
  are.
- **Clocks should be synced.** Turn on *Set time and date automatically* on both
  Macs. This release accepts up to 30 seconds of difference between Macs; a
  larger gap makes joins and some commands fail.
- **macOS on Apple Silicon only** (M1 or newer). Intel Macs are refused. Linux
  and Windows are planned; their installers exist in the source tree but have not
  been run on real hardware.
- **Prompts are visible to the Macs that serve them.** Mycelium does not hide
  your prompt or the intermediate data from the machines doing the work. Read
  `docs/user/PRIVACY.md`.
- Two-Mac swarms only have been run end to end. Larger swarms and models that
  need more memory than two Macs have are not part of this release.

## Install

1. Download `mycelium-0.1.0-macos-arm64.zip` and its `.sha256` file from the release page.
2. Check the file:

   ```sh
   shasum -a 256 ~/Downloads/mycelium-0.1.0-macos-arm64.zip
   ```

   The result must equal the value in the `.sha256` file and the one below.
3. Double-click the zip. It unpacks to the folder
   `mycelium-0.1.0-macos-arm64`. Check the app's signature:

   ```sh
   /usr/bin/codesign --verify --deep --strict ~/Downloads/mycelium-0.1.0-macos-arm64/"Mycelium A13 Demo.app"
   ```

   No output and exit status 0 is correct.
4. Right-click **Install Mycelium.command** → **Open**. If macOS blocks it, use
   **System Settings → Privacy & Security → Open Anyway**.
5. Open `~/Applications/Mycelium A13.app` and follow
   `docs/user/QUICKSTART-MACOS.md`.

Uninstall removes the program and the app link but keeps your identity and
settings in `~/Library/Application Support/Mycelium A13 macOS Demo`.

## Checksum

```text
SHA-256 (mycelium-0.1.0-macos-arm64.zip): 6920cae8cc5827695cbb903d81097c6cbc111781483f0d492762ef06e069dc59
```

## Source

Build and test instructions are in the repository `README.md`. Please report
problems as GitHub issues and include the app's short error code, not any
secrets.
