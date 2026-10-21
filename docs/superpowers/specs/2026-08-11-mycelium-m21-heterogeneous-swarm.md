# M21 Spec — Signed Heterogeneous Internet-Native Swarm

## Authority and scope

M21 extends the durable M12 membership authority; it does not introduce a second
identity plane. Mac/MLX, Linux/NumPy, Android/Termux, browser and evidence-only mobile
classes use the same signed node-ID, endpoint-ID, incarnation, generation, lease,
revocation and rotation namespace. Membership never grants activation eligibility.

The production activation allow-list remains `mac_mlx_iroh` and
`linux_numpy_iroh`. `browser_http`, `pixel_http`, `android_termux_iroh` and
`linux_tbd` remain ineligible until their class-specific parity, lifecycle, thermal,
power, network-loss and runtime gates are separately qualified. In particular,
backend acceptance of an Android process is not a production qualification.

## External-participant policy

Participation is invite-only and operator-approved. Invitations are owner-minted,
short-lived, unique and single-use. A member has request and byte quotas, bounded
audit retention, explicit revocation, credential rotation and abuse-response policy.
Credentials are principal-specific and cannot be reused across users or devices.

An assigned peer can observe its assigned model tensors, incoming/outgoing
activations, stage timing and network metadata. Mycelium does not claim Byzantine
resistance, permissionless participation, or confidentiality from a malicious
assigned worker. Those properties require a separate threat model and gate.

## Product transport

EndpointID-authenticated Iroh is the activation transport. A path is reported as
`direct`, `relay` or `unknown`; relay region may be reported but private addresses and
raw endpoint IDs are excluded. Cold/warm RTT, goodput, jitter/loss, reconnect,
connection reuse and path changes are evidence, not route authority by themselves.

Tailscale and SSH may be used as explicitly labelled operator staging conveniences
for installation and process control. They are not a product requirement, peer
identity, activation transport, planner input or success criterion.

## Closed evidence and UI

`mycelium.m21_heterogeneous_swarm.v1` is a closed, canonically digested,
privacy-reduced projection. It contains the seed/deployment binding, frozen external
policy, pseudonymous members, redacted direct/relay observations, qualified route
summary, exclusions and claim boundary. It never contains invite tokens, keys,
cookies, prompts/output, tensors, activations, raw endpoint IDs, private addresses,
filesystem paths or usernames.

Device Lab shows bounded onboarding and class qualification; Nodes shows trust,
class, lease/freshness, eligibility and connectivity; Network shows redacted path
class/relay evidence; Readiness distinguishes membership from activation; Settings
shows safe bootstrap/relay policy. The gate requires a physical heterogeneous route
using at least two eligible runtime classes and one signed browser/mobile member that
is visibly rejected from activation.
