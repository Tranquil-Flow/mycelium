# Astra's MacBook reviewer path (M22)

Supported reviewer target: macOS 13 or newer on Apple silicon, Python 3.14, the
digest-pinned Iroh sidecar in the onboarding bundle, and MLX from
`release/python-requirements.lock`. The Mac does not need a Git checkout, shared LAN,
private SSH access, or Tailscale. Tailscale is optional operator convenience only;
EndpointID-authenticated Iroh is the product transport.

1. Unpack the digest-verified `astras-macbook-m22-1.tar` into an owner-only directory.
2. Save the separately delivered single-use invitation as an owner-only file.
3. Run `python3 runtime/scripts/astra_reviewer_preflight.py --invite-bundle INVITE`.
   Success means coordinator identity verified, Apple-silicon/MLX/resource checks pass,
   and `activation_eligible=true`. It does not mean route qualification.
4. Save the operator-supplied `service-config.json`, then build and install its durable
   launchd package (replace `SERVICE_ID` with the config's `service_id`):

   ```sh
   python3 runtime/scripts/package_m22_service.py \
     --config service-config.json --output-root "$PWD/service-package"
   mkdir -p "$HOME/Library/LaunchAgents"
   cp "service-package/org.mycelium.SERVICE_ID.plist" \
     "$HOME/Library/LaunchAgents/org.mycelium.SERVICE_ID.plist"
   launchctl bootstrap "gui/$(id -u)" \
     "$HOME/Library/LaunchAgents/org.mycelium.SERVICE_ID.plist"
   launchctl enable "gui/$(id -u)/org.mycelium.SERVICE_ID"
   launchctl kickstart -k "gui/$(id -u)/org.mycelium.SERVICE_ID"
   ```

   First run consumes the invitation; later runs resume the same durable identity and
   advance its incarnation/generation. The generated service uses the bounded,
   persistent restart budget recorded in `service-package-manifest.json`.
5. In Device Lab/Nodes, confirm the pseudonymous reviewer is online but still distinct
   from route eligibility. The planner then reports its exact layer range, required and
   cached bytes, runtime/load proof, and any rejection reason.
6. After qualification, run an arbitrary browser prompt. Verify Inference history,
   Network path class, Plans allocation, per-stage counters, and Readiness bindings.
7. Use graceful drain before removal, then revoke the member in Settings. Re-running
   preflight or onboarding must not create a duplicate principal or seed authority.

Closed-lid operation is supported only under Apple's normal clamshell requirements
(AC power plus external display/input) and after a fresh reachability/renewal check.
Otherwise the laptop is treated as unavailable and placement fails closed.
