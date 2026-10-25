# Mycelium v0.1.3

Mycelium lets Apple silicon Macs in a swarm share the work of running a language model. This update fixes joined Macs giving up on work after a brief control-connection failure and makes those failures easier to diagnose.

## What changed

- A joined Mac now retries the same work offer after a transient control error until that offer expires, instead of giving up after one failure. This fixes self-hosted two-Mac swarms stopping at “Every Mac accepts and starts its slice” with `member_work_not_running`.
- Read-only staging requests now get the same bounded transient HTTP retry as other control requests. Existing attempt and time budgets still apply; authority refusals are not retried.
- Relay errors use structured codes, and clients understand them.
- Joined Macs show the HTTP status and route of a control failure in their status. Credentials and response bodies are not included.

## What was verified

A two-Mac run started from the app, using fast mode and Qwen2.5-0.5B, reached serving and completed two chats over a phone hotspot. Observed decode speed was about 12.5–15.6 tokens per second in that run. This is an observation, not a speed guarantee or a claim about larger models.

## Known issues

- The temporary relay address changes when the relay restarts. Old links may stop working; share the new address or use your own fixed relay address.
- After restarting the app on a joined Mac, press **Start / reconnect** to reconnect it.
- The default Qwen3-14B model needs about 30 GB of storage. Its speed has not been tested in this release's two-Mac run.
- Fast mode is still environment-only, with no GUI switch. It is OFF by default and has no mid-request failover, replay recovery or KV continuation. Set `MYCELIUM_TOKEN_PATH=on` in the owner/application process environment before starting the app and creating a fresh route to opt in. Leave it unset or set it to `off` for the normal recovery-capable path. Stop serving and restart before changing modes; do not change modes during a request.
- The public demo can be intermittent.
- Route compilation can take several minutes on a busy owner Mac.

## Upgrade from v0.1.2

1. Stop serving and quit Mycelium on every participating Mac.
2. Download the v0.1.3 zip and checksum from this release and verify the checksum before approving the installer.
3. Extract the archive and follow [the macOS installation guide](user/QUICKSTART-MACOS.md). Keep existing application data and swarm identity; the update does not require deleting either.
4. Upgrade the participating apps together, reopen them, rejoin the swarm and create a fresh route. Do not upgrade an in-flight route. Mixed-version operation is not promised.
5. Check the upgraded installation before removing the v0.1.2 download.

Fast mode stays OFF unless the launching environment already exports `MYCELIUM_TOKEN_PATH=on`. Remove that setting or set it to `off` to retain normal recovery behavior.

## Download and verification

Asset: `mycelium-0.1.3-macos-arm64.zip`

Run `shasum -a 256 mycelium-0.1.3-macos-arm64.zip` and compare the output with the separate `.sha256` file from the release.

Apple silicon macOS only. This is still early software with a limited hardware qualification scope. The app has an ad-hoc Apple signature, not a Developer ID signature, and is not notarized. macOS approval of the installer may require an administrator. Do not disable Gatekeeper; follow the installation guide.

Swarm members can see prompts, outputs or intermediate data needed for their work. This release does not change consent policy. Token-path mid-request failover and consent-policy changes are not included.

License: AGPL-3.0-only.
