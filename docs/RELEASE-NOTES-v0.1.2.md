# Mycelium v0.1.2

Mycelium lets Apple silicon Macs in a swarm share the work of running a language model. This update adds optional fast mode and improves startup and member-control reliability.

## What changed

- When an answer has been produced but node cleanup is not yet confirmed, the chat page shows a note after the answer rather than marking it stopped. Backend terminal status is unchanged.
- Control-only startup no longer provisions fast-mode work authority or pins the later serving runtime to a bootstrap lifetime.
- Optional fast mode uses a native token path and prepares each stage for serving. It remains OFF by default. No speed guarantee is made.
- Installed workers receive the owner's authenticated token-path trust inputs. Startup refusals are reported instead of being mistaken for a successful start.
- Host-pin leases refresh from authenticated current membership during installation. Identity and process limits remain pinned; expired or revoked authority is still refused.
- Supported mailbox operations retry brief, unstructured HTTP 502, 503, 504 and 530 responses within the same request identity and bounded attempt/time budgets. Sustained failures still end explicitly. Authority refusals are not retried, and native work is not replayed.
- Quiet-host regression tests now check the complete structured process census rather than a shortened display message. This is a test-only repair.

## Fast mode: opt-in and limits

Leave `MYCELIUM_TOKEN_PATH` unset or set it to `off` for the normal recovery-capable path. To opt in, set `MYCELIUM_TOKEN_PATH=on` in the owner/application process environment before starting the app and creating a new serving route. There is no GUI switch.

To change modes, stop serving, restart the owner/application with the chosen environment, and create a fresh route. Do not change modes during a request.

Fast mode has no mid-request failover. Replay recovery and KV continuation are not supported on the token path. If a participating stage is lost, the request can fail. Restarting and sending a new request is not continuation of that request. Keep fast mode OFF when recovery of an in-flight request is needed. Opting in never bypasses membership, trust, signatures, leases or capability checks.

## Upgrade from v0.1.1

1. Stop serving and quit Mycelium on every participating Mac.
2. Download the v0.1.2 zip and checksum from this release and verify the checksum before approving the installer.
3. Extract the archive and follow [the macOS installation guide](user/QUICKSTART-MACOS.md). Keep existing application data and swarm identity; the update does not require deleting either.
4. Upgrade the participating apps together, reopen them, rejoin the swarm and create a fresh route. Do not upgrade an in-flight route. Mixed-version operation is not promised.
5. Check the upgraded installation before removing the v0.1.1 download.

Fast mode stays OFF unless the launching environment already exports `MYCELIUM_TOKEN_PATH=on`. Remove that setting or set it to `off` to retain normal recovery behavior.

## Download and verification

Asset: `mycelium-0.1.2-macos-arm64.zip`

SHA-256: `01542cfc7f6e5f6927c4770f246fce98c5ad702ef1fb2745728af1ef25c4dc76`

Run `shasum -a 256 mycelium-0.1.2-macos-arm64.zip` and compare the output with the value above and the separate `.sha256` file.

Apple silicon macOS only. This is still early software with a limited hardware qualification scope. The app has an ad-hoc Apple signature, not a Developer ID signature, and is not notarized. macOS approval of the installer may require an administrator. Do not disable Gatekeeper; follow the installation guide.

Swarm members can see prompts, outputs or intermediate data needed for their work. This release does not change consent policy. Token-path mid-request failover and consent-policy changes are not included.

License: AGPL-3.0-only.
