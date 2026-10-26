# Privacy: what stays on your Macs and what does not

Mycelium's aim is that you run models on your own machines. This page says what that does and does not protect. It is deliberately blunt. Where something could not be checked, it says so.

## The short version

- **Your Macs, your keys.** Your swarm identity and settings live on your Mac. There is no account, no sign-up and no organizer credential.
- **Mycelium does not hide your prompt from the Macs that serve it.** Every Mac that runs part of your model can see what it needs for its part (prompts, outputs or intermediate data, plus sizes and timing). The app tells you this before you enrol. Only add Macs you trust.
- **Joining Macs uses a public HTTPS relay.** The default is a Cloudflare tunnel; an owner-supplied HTTPS address is also supported. Whoever terminates HTTPS can see that traffic (see below).
- **There is no supported, fully offline two-Mac app flow.** LAN membership and offline model-transfer code are included, but do not provide offline inference between Macs.
- **The [public demo](https://mycelium.now/swarms/) is available but can be intermittent.** Only use swarms whose operators you trust.

## What stays on your Mac

| Item | Where | Note |
|---|---|---|
| Your identity and membership records | `~/Library/Application Support/Mycelium A13 macOS Demo` | Kept when you uninstall, unless you ask for deletion. No secure-erase claim is made. |
| Downloaded model files and your slice | Under the same state folder | Verified by hash when received from the owner. |
| Settings and consent | Same folder | |
| The app's launch page | Private, opened only on your Mac; removed when you quit | The page talks to the app on your Mac only. |
| Which apps you run | Never read | The host-protection feature reads only coarse memory pressure and overall CPU. It refuses any request that names an application. |

## Who can see what

| Who | What they can see |
|---|---|
| **Other Macs in your swarm** | The data for the layers they run. Depending on their assigned work, they can see prompts, outputs or intermediate numbers (activations). This is **not** confidential inference from those Macs. They can see request sizes and timing. |
| **The owner Mac (entry point)** | Sees every prompt sent to the swarm through its chat. |
| **Cloudflare** (public relay for join codes and for web chat) | The default relay is a Cloudflare quick tunnel that ends HTTPS at Cloudflare. An owner-supplied HTTPS front can replace it; that front sees the traffic passing through it. Cloudflare can see visitor IP addresses, timing and size, and join codes in transit. For the web chat, the prompt and answer cross the tunnel as plain HTTPS JSON inside Cloudflare, so Cloudflare can see **plaintext prompts and answers**. The app now says this in its own words: the relay status line, the confirmation shown before a tunnel starts, the swarm directory's privacy note and page footer, and the hosted-demo label all name Cloudflare and say it can see visitor IP addresses, timing and web chat prompts and replies in plaintext, and that only inference between the Macs uses Iroh. (Earlier builds said only "can observe control traffic; inference uses Iroh", which was misleading for web chat.) |
| **iroh relays and name service (n0)** | When Macs connect through the internet, the network library (iroh) uses n0's relay servers and name lookups. They can see endpoint IDs and IP addresses, and, for relayed traffic, the size and timing of encrypted packets. The traffic between Macs is encrypted end to end between those two endpoints by the library's design; this review did not re-verify it. |
| **Hugging Face** | If a Mac downloads model files from there, Hugging Face sees that Mac's IP address and the file list. On the owner's local network, the member tries the owner first. Across networks, it prefers Hugging Face over the owner's relay. |
| **The public swarm directory** (only if you press **Publish this run as a swarm**) | A list entry: owner key, owner fingerprint, entry address, member count and model name, and any access-request notes people type. A published swarm also reports performance statistics measured by its own app, not independently by the directory. Chat prompts go to the swarm entry, not through the directory API. Listings are public. |
| **Anyone running a swarm you visit** | If you chat in someone else's swarm from a browser, **their entry origin gets your prompt and your IP address directly**. A listing can point at any address. Only chat with swarms you trust. |
| **Your internet provider** | That you use Cloudflare, n0 or Hugging Face, and traffic volume. |

## On-disk prompt copies

While a request runs, the request gateway can save a **plain, unencrypted copy of your prompt (as token numbers, which are easy to turn back into text) and the answer so far**, so it can recover after a failure. The folder is private to your user and the copy is deleted when the request ends. Start-up now **sweeps the temporary files a crash can leave behind** (`.checkpoint-*.tmp`, written just before a copy is saved, and they hold the prompt as token numbers); a test covers it. Orphaned finished copies were already discarded when the gateway starts. Both cleanup fixes are included in the released source. Whatever the build, a state folder can still end up in Time Machine or other backups, so keep that in mind if you handle sensitive text.

## The public demo

The [public directory](https://mycelium.now/swarms/) and browser chat are available. The developer's demo depends on participating Macs staying awake and can be intermittent. Availability is not a privacy guarantee or a promise that every listed swarm is serving.

The source includes owner-controlled access modes, quotas and a kill switch. Web chat passes through the swarm's HTTPS front. The default front is Cloudflare, which can see prompts, replies, visitor addresses and timing. These controls do not make a swarm private from its operator or workers.

A browser keeps one stable key, so entry operators and the HTTPS front can link activity across swarms. **Forget this key (and its grants and swarms)** removes the browser's access and management key; it does not delete copies of data held elsewhere.

## Local-first scope

The released source includes network modes (`internet`, `lan`, `local`), an offline model source, an owner-served model manifest and a folder import. These pieces do not provide a fully offline two-Mac app flow. The [local-first runbook](../LOCAL-FIRST.md) separates source and unit-test coverage from hardware qualification:

- It is covered by unit tests, **not run on two machines**.
- The LAN transport helper for inference between Macs is **design only**. In LAN mode it starts the seed and membership, not inference.
- The desktop app's join flow still needs a public HTTPS relay, either the default Cloudflare tunnel or an owner-supplied address.
- The seed endpoint on a LAN is plain `http`, with no TLS yet.
- Offline model transfer trusts digests from the owner rather than from Hugging Face.

So, today: **nothing in this version gives you a fully offline two-Mac run.** If this page ever says "released", check it against the code.

## Adaptive sharing

Mycelium watches this Mac's memory, CPU, heat and battery. When other apps need resources, it automatically shares less, finishes or hands off work in progress, and pauses sharing. It resumes once the Mac has been healthy for a while. Your own apps always come first. It reads only coarse memory pressure and overall CPU load, never which apps you run, and nothing is sampled before you consent. It is on by default at your first consent and you can switch it off. Off means the Mac keeps sharing under pressure until you press **Pause sharing** or **Stop**; your memory limit, disk-space floor and power, heat and network limits stay enforced either way. Turning it off never widens what an owner can do to your Mac.

## Choices you control

- Tick consent only for Macs you trust.
- Choose AC-only, unmetered-only and storage limits.
- Keep **Withdraw standing consent** and **Revoke this Mac** in mind. Both are in the app.
- Do not publish a swarm unless you want it listed.
- Use **Stop** and **Quit launcher** when you are done.

## Sources

- `mycelium_a13_macos/web/index.html` (consent wording; "This is not confidential inference from the assigned worker"; publish card text)
- `mycelium_a13_macos/public_relay.py` (relay privacy string)
- `mycelium_a13_macos/adaptive_owner.py` (no application identity; exact key sets)
- `docs/handover/A13-MACOS-QUICKSTART.md` (pairing and privacy notes; cooperative-worker visibility)
- [Local-first runbook](../LOCAL-FIRST.md) (included code and remaining offline limits)
- `mycelium_swarm_directory/server.py`, `mycelium_swarm_directory/entry.py` (public directory and entry access checks)
- `mycelium_a13_macos/chat.py`, `mycelium_a13_macos/public_relay.py` (chat access and HTTPS fronts)
