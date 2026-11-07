Mycelium A13 macOS pilot bundle (owner_private_media)
=====================================================

Not signed with an Apple Developer ID and not notarized (ad-hoc signature only).
macOS will block "Install Mycelium.command" once: approve that one script in
System Settings > Privacy & Security > Open Anyway (the exact steps are in
QUICKSTART-MACOS.md). Do not disable Gatekeeper.

Install for this user only (no admin, no network): run "Install Mycelium.command".
It checks this folder against SHA256SUMS and the app against APP-FILES.sha256,
and only then removes the download mark from this app so it can start. The
installer then verifies the Ed25519-signed installation-package-manifest.json
and the bundle digest before writing anything, clears the mark on the
installed copy only, and links ~/Applications/Mycelium A13.app to it.
Uninstall keeps your identity and membership state unless you ask otherwise.
