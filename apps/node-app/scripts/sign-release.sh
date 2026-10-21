#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-only
# Sign the release APK with the project release key. The keystore lives outside
# the repository; its password is read from the macOS Keychain at build time and
# handed to apksigner through the environment only (never argv, logs or disk).
# Prints the signer certificate SHA-256, the public signer identity the A12
# `signed_native_application` evidence pins.
set -euo pipefail

KEYSTORE="${MYCELIUM_ANDROID_KEYSTORE:-$HOME/.hermes/secrets/mycelium-android-release.jks}"
ALIAS="${MYCELIUM_ANDROID_KEY_ALIAS:-mycelium-node}"
KEYCHAIN_SERVICE="${MYCELIUM_ANDROID_KEYCHAIN_SERVICE:-mycelium-android-release}"
KEYCHAIN_ACCOUNT="${MYCELIUM_ANDROID_KEYCHAIN_ACCOUNT:-mycelium}"
SDK="${ANDROID_HOME:-$HOME/Library/Android/sdk}"
APKSIGNER="$(ls -d "$SDK"/build-tools/*/ | sort -V | tail -1)apksigner"
IN="${1:-src-tauri/gen/android/app/build/outputs/apk/universal/release/app-universal-release-unsigned.apk}"
OUT="${2:-${IN%-unsigned.apk}-signed.apk}"

fail() { echo "sign-release: $1" >&2; exit 2; }
[ -f "$KEYSTORE" ] || fail "release_keystore_missing"
[ -f "$IN" ] || fail "unsigned_release_apk_missing"
command -v security >/dev/null || fail "keychain_tool_missing"

MYCELIUM_KS_PASS="$(security find-generic-password -s "$KEYCHAIN_SERVICE" -a "$KEYCHAIN_ACCOUNT" -w 2>/dev/null)" \
  || fail "release_keystore_password_missing_from_keychain"
export MYCELIUM_KS_PASS
"$APKSIGNER" sign --ks "$KEYSTORE" --ks-key-alias "$ALIAS" \
  --ks-pass env:MYCELIUM_KS_PASS --key-pass env:MYCELIUM_KS_PASS \
  --out "$OUT" "$IN"
unset MYCELIUM_KS_PASS
"$APKSIGNER" verify --verbose --print-certs "$OUT" \
  | grep -E "Verified using v[23] scheme|Signer #1 certificate SHA-256 digest"
echo "signed: $OUT"
