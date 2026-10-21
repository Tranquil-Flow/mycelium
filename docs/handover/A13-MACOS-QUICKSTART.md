# Mycelium macOS A13 — 0.1.0-preview.1

**Unsigned development preview, not notarized. Not yet serving.**
This package demonstrates installation-local owner/target onboarding and lifecycle.
It is not the completed judge-owned two-Mac inference demo.

## Platform actually exercised

- Apple Silicon arm64, macOS **26.5.1 (25F80)**.
- macOS 15.6.1 is an admitted compatibility target, not an exercised installation.
- No continuous macOS version range is qualified. Intel and other operating systems
  are not supported by this artifact.
- Package validation uses the actual bundled executable from a clean working
  directory with no source-mode fallback. It is not a Finder/Gatekeeper approval
  test or a second-Mac test.

## Install and start

1. Download `Mycelium-A13-0.1.0-preview.1-arm64.zip` and its adjacent SHA-256 file.
   Verify the ZIP digest before extracting. The digest establishes byte identity,
   not publisher authenticity or software safety.
2. Extract the ZIP. Move `Mycelium A13 Demo.app` to your Applications folder or a
   private folder of your choice. No Python installation or source edits are needed
   for the packaged onboarding path.
3. Open the app. It opens a private local launch page in your default browser.
   Select **Open private demo**. This does not open a public listener or tunnel.
4. Review cooperative-worker visibility and power/network/storage policies.
   Select **Create authority & enroll this Mac** to own a fresh swarm, or
   **Join an existing swarm** for a target-owned identity. A target does not create
   a swarm seed. No organizer credentials are bundled or required.
5. Membership/capability are not serving. **Report this Mac capability** performs a
   tiny local arithmetic self-check, not inference or independent qualification.

### Gatekeeper and firewall

This artifact has ad-hoc code signing only, no trusted publisher certificate and
no notarization. macOS may refuse to open it. Do not disable Gatekeeper, remove
quarantine attributes, or run security-bypass commands. If blocked, inspect the
macOS explanation and contact the organizer for a trusted distribution path;
any OS trust decision belongs to you, not the application or assistant.

No incoming firewall rule is needed for installation-local onboarding. Do not
open firewall ports or disable TLS checks to make this preview pair across the
Internet. Public HTTPS reachability and unrelated-network pairing are **not
implemented/qualified as a normal-user path** in this package. Existing local
TLS pairing tests do not establish cross-network operation.

## Exact selected model — not loaded

- Model: `Qwen/Qwen2.5-0.5B-Instruct`
- Revision: `7ae557604adf67be50417f59c2c2f167def9a775`
- Source format: original Safetensors; runtime: MLX, float32 arithmetic,
  `int8-weight-only` representation.
- Decoding: `quantized_greedy_token_id`, sampling seed **0**.

These are the owner-selected contract, not reuse of hosted W6 credentials,
artifacts, signatures or physical evidence. The model is not included in the ZIP.
Pinned upstream metadata reports **988,097,824 bytes of weights** and
**999,604,126 bytes for the complete repository**. Compiled packs and runtime
storage require additional space, not yet measured for this preview.

Automatic first-run download, byte progress and interruption/resume are still
unimplemented. The download button is disabled, no download is started, and
no model is executed. Do not interpret a selected model as a loaded model.

## Optional verifier

The proposed endpoint is `https://verifier.mycelium.now`. A lookup from the build
machine failed to resolve it during this checkpoint. This is a local observation,
not proof of global outage. Authentication/audit schemas have not been verified.

The preview makes **no verifier connection**. The control is visibly disabled;
configurable URL, consent-gated outbound authentication and audit-task execution
remain unfinished. Future connectivity must be off by default and require
explicit consent describing transmitted audit data. Connecting must not authorize
paid inference. No payment or public publication occurs in this preview.

## Pairing and privacy

The target creates its public pairing request before the owner mints an invitation.
The owner reviews target identity, Mac class, expiry and quotas. Return only the
recipient-encrypted invitation. Never paste a plaintext invite into a URL or log.
The underlying application pairing path is tested through real local HTTPS and
SQLite using independent browser contexts, not two physical devices.

Cooperative workers can observe the prompts, outputs or activations needed for
assigned work, including traffic size/timing. No confidentiality-from-worker
claim is made. A clean second browser session cannot redeem private state without
its own native one-use launch authorization.

## Stop, restart and uninstall

- **Stop & retain private state** pauses local membership renewal and retains the
  installation's identity/recovery data. **Start / reconnect** resumes through
  the canonical membership path; it does not regenerate identity silently.
- The owner can revoke a reviewed member. Revocation differs from local deletion.
- **Quit launcher** exits the owned local application and removes its transient
  launch page. Reopening the app creates a fresh browser authorization.
- Quit before moving the app to Trash. Keep
  `~/Library/Application Support/Mycelium A13 macOS Demo` for recovery.
- To deliberately reset, quit and move that exact folder to a private backup
  before starting again. Starting with an absent folder creates a new installation;
  it does not recover the prior identity. Review any backup before deleting it.
  No secure-erasure claim is made, and the app never deletes that state implicitly.

## Still required for the full demo

Managed assignment → authorized provisioning → load/challenge → fresh two-host
qualification → registry selection → ordinary browser streamed inference with
request receipts and placement counters; resource enforcement; cross-network
pairing; download/progress/recovery; optional verifier integration; second-Mac
package execution; exact approved model/resource/network execution budget.

V2 gateway source now carries actual `token_id` alongside unchanged text and
rejects inconsistent replay; this local protocol result is not model execution.
Original cross-platform A13 release qualification remains unclaimed.


## Core API activation sequence (round 13)

No frontend build or verifier is required. Use each installation's authenticated
local application session. Never put its credentials or decrypted pairing state
in URLs, command arguments, public logs, or handoffs.

After supported pairing, model acquisition, bootstrap observation and compilation:
1. Read the exact `execution_proposal` from `POST /api/serving/status` with `{}`.
2. Owner: `POST /api/serving/start` with that proposal and
   `model_execution_acknowledged: true`. This publishes staging offers; it does
   not assert readiness or immediately dispatch model commands.
3. Each contributor: preview and explicitly accept its own exact work offer using
   `/api/work/preview` and `/api/work/accept`, with the approved resource limits.
4. Wait for both acceptance calls to succeed. Owner state must be
   `awaiting_activation`. Call `POST /api/serving/activate` with the same exact
   `execution_proposal` and `activation_acknowledged: true`.
5. Observe qualification through the registry's supported APIs. An
   `api_running` application or accepted activation is NOT a qualified route.
   A consuming client must not submit until current physical qualification and
   selection are valid. The existing request/SSE APIs own terminal outcomes.

The activation wait stops on cancellation or staging-offer expiry. It never
extends a signed offer or manufactures contributor acceptance/readiness.
On failure, retain run journals and inspect the stable live-status reason;
private serving exception details are retained under the run's private state.

Current physical acceptance is still incomplete; these are implemented callable
steps, not a claim that every C1–C8 gate has passed.
