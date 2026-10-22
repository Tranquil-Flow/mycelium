# macOS install flow and trust chain

Applies to the ad-hoc signed, not notarized macOS package
(`mycelium-<version>-macos-arm64.zip`). If Developer ID signing and notarization are ever ruled in, the
Gatekeeper rounds below disappear; the checks stay.

## Constraints

- No network, no administrator rights, no `spctl`/`csrutil`/Gatekeeper changes.
- Verify before trust: nothing is un-quarantined, installed or run before the document and file checks pass.
- Refuse on any mismatch, with nothing changed and a one-line reason (never a traceback).

## What macOS 26 does (observed 2026-10-21, admin user, Safari download)

1. Right-click Open on `Install Mycelium.command` gives "Not Opened"; only Privacy & Security, Open Anyway runs it.
2. The old script then `exec`ed the still-quarantined ad-hoc app binary: a second block and a killed shell.
3. Open Anyway for the app launched the bundle itself, translocated, with no `--install`: a session in a
   throw-away read-only copy and nothing installed.
4. The installed copy kept `com.apple.quarantine`, so `~/Applications/Mycelium A13.app` ran translocated too.

## The flow now

One Gatekeeper decision, for the script. Then:

| Step | Who | Check | On failure |
|---|---|---|---|
| 1 | User | Approves `Install Mycelium.command` (Done, Privacy & Security, Open Anyway, Open Anyway). | Nothing runs. |
| 2 | Script (`/bin/sh`, system `shasum`) | `SHA256SUMS` exists and lists the manifest, install source, the script itself and `APP-FILES.sha256`; `shasum -a 256 -c SHA256SUMS` passes. | `Mycelium is not installed: ...`, exit 2. No app code runs. |
| 3 | Script | The app is a plain folder (not a link) with a plain executable; every regular file of the app matches `APP-FILES.sha256`; the app holds no file that list omits and no link to an absolute path. | Same refusal. |
| 4 | Script | Removes `com.apple.quarantine` from **that app folder only** (`xattr -d -r -s`, links not followed), then reads the attribute back on the executable. The documents, the zip and everything else keep their mark. | Refusal if the mark is still there. |
| 5 | App `--install` | Verifies the Ed25519-signed `installation-package-manifest.json`, the `install-source.json`, the publisher pin, platform, floor and the link-aware bundle digest (`bundle_digest`), before any write. | `refused` JSON, nothing written. |
| 6 | Installer | Copies the app with `ditto` into `programs/<version>.partial`, re-computes the digest of that staged copy, and only if it equals the signed digest clears `com.apple.quarantine` on the staged copy, then renames the whole folder to `programs/<version>` atomically. | Staging removed; nothing installed. |

`~/Applications/Mycelium A13.app` links to the installed copy. It carries no mark, so macOS opens it
normally and does not translocate it.

### Why the script clears the mark in step 4

The script is what the user approved. Without step 4 the next line starts a quarantined binary and macOS
blocks it again. Step 4 happens only after steps 2 and 3, so a damaged, partial or edited download never
gets the mark removed. The signed-manifest check in step 5 is the real authentication; steps 2 and 3 are
integrity checks in plain shell that run before any bundled code does.

### What this does and does not prove

- `SHA256SUMS` and `APP-FILES.sha256` ship inside the zip, so they prove the folder is complete and
  self-consistent, not who built it. Who built it is proved by the zip's SHA-256 the owner sends out of band
  (Quickstart, Step 1) and by the Ed25519 signature the installer checks in step 5.
- A forger who replaces the whole zip and both lists would get step 4 and one run of their binary. The
  out-of-band checksum (Step 1) is what stops that; it is the same trust the user already extends by
  approving an unsigned script. Developer ID plus notarization would replace it (owner decision).
- No step weakens Gatekeeper for anything else: no `spctl`, no global setting, no recursive removal outside
  the app folder.

## Launching from the wrong place

The packaged app started with no arguments from anywhere but `programs/<version>` (a translocated copy, or
the downloaded folder) refuses: it starts no session, writes no state, prints one `launcher_refused` line
with code `not_installed`, and shows the alert "Mycelium is not installed yet ... double-click 'Install
Mycelium.command'". Any explicit flag (`--install`, `--headless`, `--state-root`, `--node-process-launcher`,
`-m ...`) keeps its meaning, so node processes, the service runner and the gates are unaffected.

## Install integrity (same change)

- The payload is copied to `programs/<version>.partial`, verified, then the whole directory is renamed to
  `programs/<version>`. A kill leaves at most a `.partial` directory, which the next run removes.
- A `programs/<version>` directory without a valid payload is stale: it is renamed to
  `<version>.stale-<n>` (kept for inspection, never deleted) and the install continues. A directory with a
  valid payload but different bytes is `installed_version_conflict`.
- `programs/.install.lock` is an exclusive `flock` around install, rollback and uninstall. A second runner
  refuses with `install_already_running`. The kernel releases the lock when the process ends, so a killed
  installer never leaves a stale lock.
- Unexpected `OSError` is `install_io_error`; anything else is `installer_failed`. Both are the structured
  `refused` line.

## Tests

`tests/a13_macos/test_install_command.py`, `test_installer_atomic.py`, `test_macos_package.py`,
`test_launch_location.py`, and `test_quarantine_guard.py` exercise the desk-only path.

No automated test sets the real quarantine attribute, even on directories, links or data files.
`tests/a13_macos/conftest.py` has a session-autouse guard for OS/xattr setters and subprocess writers
(including shell/script forms), plus an audit-hook backstop and read-only checks for marked launch inputs
and their ancestors. A forbidden write raises `REAL QUARANTINE WRITE FORBIDDEN` before it reaches macOS.

`fake_xattrs.py` stores attributes in memory, models copy propagation, and preserves marks across atomic
rename by inode. The installer accepts an injected xattr command runner. The shell test replaces only its
fixed `XATTR` dependency and `RUNNER` in a test copy: xattr calls reach the memory fake through a private
loopback adapter; the runner is a stub outside the data-only app. Real shell checks and refusal ordering
are still exercised, including denied removal and marked symlinks.

These tests do **not** prove macOS metadata preservation, Finder launch, or the one-Gatekeeper-round
recipient journey. The next admin-user run must verify those on a newly built, checksum-verified release
in an isolated account, following Quickstart Step 3. Do not run this qualification against an active demo
installation or manufacture quarantine as part of pytest.
