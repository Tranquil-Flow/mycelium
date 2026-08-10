# External tester and new-device boundary

**Status:** trusted-invite boundary for the current MVP. This is not a claim of
permissionless, anonymous, Byzantine-resistant, or multi-tenant operation.

## Who may join

The current durable seed admits only a person or device that receives a unique,
short-lived, Ed25519-signed, single-use invitation from the operator. One invite is
issued per device identity. An invite must never be reused, posted publicly, placed in
shell arguments, or copied into product telemetry.

The operator must know who controls the device, use a separate authenticated channel
to deliver its owner-only invitation file, and be able to revoke or stop that member.
The current trust model assumes invited peers are cooperative. It does not protect a
prompt or model artifact from a malicious admitted peer that intentionally retains
data it was assigned to process.

## What joining proves

A successful join proves that the device holds its durable private membership key,
redeemed one authorized invite, and is speaking to the pinned durable seed identity.
Membership generation and leases fence replaced, revoked, stopped, and stale device
sessions.

Joining does **not** prove measured capacity, runtime correctness, model compatibility,
artifact load, reachability for activation traffic, route placement, or inference
qualification. A member starts with `route_ready=false`. Before an
activation-eligible native device may alter a live route, the operator must collect
fresh capability and link evidence, produce an assignment, stage only its assigned
artifacts, verify load/runtime parity, rebuild the complete topology, and pass the
physical qualifier. The existing qualified route remains immutable until that gate
passes.

## Device classes

| Peer class | Current use | Activation boundary |
| --- | --- | --- |
| `mac_mlx_iroh` | Native macOS stage worker | Eligible only after separate physical qualification |
| `browser_http` | Browser Device Lab worker | Never activation eligible |
| `pixel_http` | Android evidence/probe worker | Never activation eligible in the current build |
| `linux_tbd` | Reserved membership identity | No activation runtime is approved |

An invite batch may contain at most 64 independent credentials. That limit is an
operator safety bound, not a promise that a model can or should be split across 64
stages.

## Network and data visibility

For the current deployment, cross-network operators use the same private Tailscale
tailnet for seed reachability and staging. Tailscale is not intrinsic to the signed
membership protocol, but removing it as an operational dependency and qualifying
ordinary Internet direct/relay paths remains M20 work. Bind the current seed only to
its private Tailscale address; do not expose its HTTP control endpoint to the public
Internet.

An assigned native stage receives its authorized model tensors and observes the
activations and request timing that traverse that stage. The final stage observes
decoded-token computation; the entry stage processes input token state. Operators
must use non-sensitive test prompts and assume an admitted device can retain anything
delivered to it. Observatory and status projections must continue to omit credentials,
prompts, outputs, tensors, activations, KV content, private paths, and private network
addresses.

## Not yet supported

- self-service public signup or anonymous peers;
- mutually untrusted users or devices;
- per-user model/prompt confidentiality from assigned workers;
- automatic placement immediately after enrollment;
- an Android or browser inference stage;
- Tailscale-independent direct/relay qualification;
- unattended closed-lid Mac operation without a supported clamshell setup or an
  explicit administrator power-management decision.

These are product-security and qualification boundaries, not UI limitations to work
around.
