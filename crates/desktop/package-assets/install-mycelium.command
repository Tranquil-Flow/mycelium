#!/bin/sh
# Install Mycelium for this user. No admin, no network, nothing outside this folder is trusted blindly.
#
# You approved this script once (Privacy & Security > Open Anyway).  Before it extends that approval to
# the app next to it, it checks the downloaded folder against SHA256SUMS and the app file by file against
# APP-FILES.sha256.  Only if every check passes does it remove the download-quarantine mark from THIS
# app folder (and nothing else), so the installer can start without a second block.  The installer then
# verifies the Ed25519-signed manifest and the bundle digest again before it writes anything, and clears
# the mark on the installed copy.  Any mismatch: nothing is changed and nothing is run.
set -eu
HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
APP="$HERE/Mycelium.app"
EXE="$APP/Contents/MacOS/mycelium"
XATTR=/usr/bin/xattr
refuse() {
  echo "Mycelium is not installed: $1. Nothing was changed and nothing was run." >&2
  echo "Download the zip again and compare its SHA-256 with the one on the release page." >&2
  exit 2
}
cd "$HERE" || refuse "this folder cannot be read"
[ -f SHA256SUMS ] || refuse "SHA256SUMS is missing"
for doc in installation-package-manifest.json install-source.json "Install Mycelium.command" APP-FILES.sha256; do
  sed -n 's/^[0-9a-f]\{64\}  //p' SHA256SUMS | grep -Fxq -- "$doc" || refuse "$doc is not covered by SHA256SUMS"
done
/usr/bin/shasum -a 256 -c SHA256SUMS >/dev/null 2>&1 || refuse "a file in this folder does not match SHA256SUMS"
[ -d "$APP" ] && [ ! -L "$APP" ] && [ -f "$EXE" ] && [ ! -L "$EXE" ] || refuse "the app is missing or is not a plain folder here"
(cd "$APP" && /usr/bin/shasum -a 256 -c "$HERE/APP-FILES.sha256" >/dev/null 2>&1) || refuse "the app does not match APP-FILES.sha256"
found="$(cd "$APP" && find . ! -type d ! -type l | sed 's|^\./||' | LC_ALL=C sort)"
listed="$(sed 's/^[0-9a-f]\{64\}  //' APP-FILES.sha256 | LC_ALL=C sort)"
[ "$found" = "$listed" ] || refuse "the app holds files that APP-FILES.sha256 does not list"
if (cd "$APP" && find . -type l -exec readlink {} \;) | grep -q '^/'; then refuse "the app holds a link to an absolute path"; fi
"$XATTR" -d -r -s com.apple.quarantine "$APP" >/dev/null 2>&1 || true
if "$XATTR" -p com.apple.quarantine "$EXE" >/dev/null 2>&1; then refuse "the download mark could not be removed from the app"; fi
RUNNER="$EXE"
if "$RUNNER" --install --bundle "$HERE"; then
  echo
  echo "Mycelium is installed for this user: open 'Mycelium' in the Applications folder of your home folder."
else
  status=$?
  echo "Mycelium is not installed: the installer refused (see above)." >&2
  exit "$status"
fi
