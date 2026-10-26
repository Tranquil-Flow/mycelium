# Friend swarm across different networks

A private swarm for you and a few friends, each on their own network (home Wi-Fi,
a phone hotspot, an office), without `mycelium.now`. Nothing here needs a hosted
Mycelium service: the owner's Mac publishes a temporary Cloudflare quick tunnel,
friends join over outbound HTTPS, and the Macs then talk to each other over Iroh.

Status: the cross-network path has been traced in source and covered by tests, but it
has not been run across two different physical networks yet. Every recorded two-Mac
session so far had both Macs on one network. Treat the first real run as a test.

## Who sees what

| Party | What it sees | When |
| --- | --- | --- |
| **Cloudflare** (quick tunnel `*.trycloudflare.com`, and its HTTPS DNS fallback) | The owner relay terminates HTTPS, so it can observe join/control traffic, visitor IP addresses and timing, and web chat prompts and replies in plaintext. It is a trusted-for-availability, not a private, hop. | While the relay runs |
| **n0** (Iroh relays and `iroh.link` DNS/pkarr lookup) | Which endpoints talk to each other, when, and how much. The inference payloads between the Macs are encrypted end to end. | While a route is up |
| **Hugging Face** | A friend's Mac downloads the pinned model from Hugging Face when it is not on the owner's local network, so Hugging Face sees that Mac's IP address and the model name. Every file is checked against the owner's pinned digests whichever source supplied the bytes. | During the model download |

The private friend-swarm flow does not need the public directory. If you choose
**Publish this run as a swarm**, the app contacts the directory and publishes
listing metadata and self-reported performance statistics. There is no claim that
a friend swarm is private from its participating Macs or its HTTPS front.

## Before the session (owner and friends)

1. **Firewall.** Start the Mycelium app once on each Mac before the timed session and
   click **Allow** when macOS asks whether to accept incoming network connections
   (the app, its Iroh sidecar and the bundled `cloudflared` can each trigger it). An
   unanswered prompt can stall a join or a route.
2. **Clocks.** Both Macs should have *Set time and date automatically* on
   (System Settings, General, Date & Time). Managed control tolerates 30 seconds of
   difference. The join page warns above 20 seconds (see below).
3. **Owner Mac awake.** The owner keeps the Mac awake and on power for the whole
   session. If it sleeps, the tunnel drops and the swarm is unreachable until the
   owner's relay is started again.
4. **Model.** The owner selects the pinned model. Friends that do not have it
   download it (see "Model download" below).

## Owner steps

1. Create the swarm and tick **Let other Macs join**, or press **Start public relay**
   afterwards. Choose the relay time limit you need, up to 30 minutes. This is
   separate from each Mac's **Time limit per run**.
2. **The quick tunnel has a time limit and a new hostname each start.** After the limit
   the tunnel closes and the swarm is unreachable. Starting the relay again gives a
   different `*.trycloudflare.com` hostname, so any open invitation code or link from
   the earlier hostname stops working (`pairing_window_origin_stale`) and you have to
   open a new invitation window and send the new link.
3. Open an invitation window (5, 10 or 15 minutes). The app shows two forms:
   a typed form (`quiet-river-apple-stone ABCD-EFGH-JKLM`) and a **join link**.
4. Send the **join link** to the friend through a message app. Compare the fingerprint
   shown on both Macs, then approve the request.

## Use your own address (stable hostname)

Instead of a random quick-tunnel hostname you can serve the swarm on a fixed `https://` address you
own, for example `https://demo.mycelium.now`. Mycelium does not create it: **you must point the
hostname at this Mac yourself**. On the owner page open **Use my own address**, enter the address and
a local port; the app listens on `127.0.0.1:<port>` and your front forwards to it.

### The Cloudflare named-tunnel example (`demo.mycelium.now`)

This is what you run on the owner Mac. It is your own Cloudflare setup, separate from
Mycelium; Mycelium never reads or writes it, and no secret belongs in this document or in a
message to anyone. It needs a free Cloudflare account that manages the `mycelium.now` zone.

```sh
cloudflared tunnel login                          # opens your browser; stores a cert in ~/.cloudflared
cloudflared tunnel create mycelium-demo           # prints the tunnel UUID and writes <UUID>.json (a secret)
cloudflared tunnel route dns mycelium-demo demo.mycelium.now
```

`~/.cloudflared/config.yml` (replace `<TUNNEL-UUID>` and `<you>`; pick the same port you type in
the app):

```yaml
tunnel: <TUNNEL-UUID>
credentials-file: /Users/<you>/.cloudflared/<TUNNEL-UUID>.json
protocol: http2
ingress:
  - hostname: demo.mycelium.now
    service: http://127.0.0.1:8787
  - service: http_status:404
```

Then `cloudflared tunnel run mycelium-demo` and keep it running while the swarm is up. In
Mycelium press **Use my own address**, enter `https://demo.mycelium.now`, port `8787`, tick
"served by a Cloudflare tunnel", and start the relay.

Rules the app enforces: `https://` plus a real DNS name only (no port, path, login details,
IP number or `.local` name), and a local port from 1024 to 65535. The typed short code
(`quiet-river-apple-stone ABCD-EFGH-JKLM`) still works only for `*.trycloudflare.com`; for your
own address send the **join link**, which carries the address and the owner's key digest.

When the relay stops and restarts on the same configured address, the swarm identity,
members and listing are retained. Unexpired join links still use the same address;
expired or used invitations still need replacing. Press
**Use my own address** again, or restart the relay with the same address (the app remembers it),
and nothing has to be republished. The app only opens the local port; if the tunnel is not
running, visitors see a Cloudflare error page and the app still works locally.

Who can see what: whichever front serves your hostname terminates HTTPS and sees control
traffic, visitor IP addresses and timing, and web chat prompts and replies. With a Cloudflare
tunnel that is Cloudflare; with your own server it is you. Only inference between the Macs
uses Iroh. The public chat page and swarm entry follow exactly the same checks on your address
as on a quick tunnel (kill switch, access mode, daily caps, per-visitor rate limit). The
per-visitor rate limit reads Cloudflare's visitor-address header only when you tick the
Cloudflare box; for any other front all visitors share one limit.

## Friend steps

1. **Paste the join link rather than typing the short code.** The link carries the
   exact relay origin and pins the owner's seed key, so a wrong or look-alike host is
   refused; the typed form is built from words only and only works for
   `*.trycloudflare.com`.
2. Tick the standing consent, join, and compare the fingerprint with the owner.
3. Wait for the owner to approve. The Mac then appears as a member.

## Run time limits and automatic rollover

**Time limit per run** means each Mac agrees to lend compute for at most the chosen
number of minutes per run, 30 by default. In v0.1.4, the app starts a fresh run
automatically when the current run reaches its limit. It checks the saved consent
and available members again rather than extending the old run's permission.

Keep the owner and joined apps open. **Stop** ends the session; rollover is not a
reason to restart work you stopped. `<<TBD: final rollover behavior for in-flight
answers, cleanup and a failed restart>>`.

Run rollover and relay lifetime are separate. A temporary relay can still expire
after at most 30 minutes. Rollover does not renew that tunnel or preserve its old
address. For longer sessions, use your own fixed HTTPS address and keep its front
running; the app-side renewal limits below still apply.

After the owner app restarts at the same reachable address, joined Macs re-report
and reconnect automatically. This does not resume an interrupted answer. After
restarting the joined Mac's own app, press **Start / reconnect** there.

## Model download across networks

The owner offers its model files over three sources: its own local network, its
relay, and Hugging Face. The friend Mac picks the order:

- Same local network as the owner (same /24 as the owner's address): owner local
  network, owner relay, then Hugging Face. This is the previous behaviour.
- Different network: the owner's local network address is skipped (it would be
  unreachable or belong to an unrelated device) and Hugging Face is tried **before**
  the owner relay, so the model does not stream through the owner's phone hotspot
  and a Cloudflare quick tunnel. The owner relay remains the backup.
- Offline mode never contacts Hugging Face.

## If joining fails

The join screen translates these codes; the wording may vary slightly.

| Code | Meaning and what to do |
| --- | --- |
| `pairing_window_dns_failed` | This Mac could not resolve the owner's tunnel name, including through the Cloudflare HTTPS DNS fallback. Check this Mac's internet connection; a network that blocks DNS and DNS-over-HTTPS can cause it. |
| `pairing_window_tls_failed` | A secure connection could not be set up (certificate or handshake problem). Check the clock and any proxy or captive portal on this network, then retry. |
| `pairing_window_timeout` | The owner's tunnel did not answer in time. The owner's relay may have reached its time limit or the owner's Mac may be asleep; ask for a new link. |
| `pairing_window_unreachable` | Any other failure to reach the owner (the earlier catch-all, kept as the parent of the three codes above). |
| `pairing_window_origin_stale`, `pairing_window_expired`, `join_link_expired` | The link belongs to an earlier tunnel or window; ask the owner for a new link. |
| `clock_skew` (warning) | This Mac's clock differs from the server's by more than 20 seconds. The join continues, but turn on *Set time and date automatically*; a difference over 30 seconds makes later steps fail. |

## Leaving a swarm

A Mac that joined can leave at any time from **Leave swarm** (tick the confirmation first):

- **Leave smoothly** stops new work and lets the requests already running on this Mac
  finish, then unloads the model, withdraws the standing consent and stops renewing the
  membership. If the number of running requests has not gone down for 30 seconds (or the
  wait passes 120 seconds) it leaves instantly instead and says so. Running requests are
  not handed to a successor Mac: a member has no way to start or prove a handoff, so they
  finish here or end with the swarm's normal failure handling.
- **Instant leave** withdraws consent and disconnects now, like losing the internet. The
  owner's swarm adapts on its next request.

A member cannot sign its own removal at the owner: the owner sees a Mac that left the way it
sees any lost Mac (its lease stops renewing and the control connection closes), and can
revoke it from the owner screen.

## Limits

- Cloudflare quick tunnels have no uptime guarantee and a lifetime set by the owner
  (at most 30 minutes in the app); this is a session tool, not an always-on service.
  Your own address keeps the same hostname across restarts. Its relay in the app extends
  itself shortly before it expires **while a run is active or a swarm is published**, for up to 24 hours in total
  (`max_renewed_seconds`, at most 7 days; `auto_renew: false` turns it off). A relay that is
  neither running a model nor publishing a swarm is not extended. An owner who runs a named tunnel can pass `long_lived: true`
  to `/api/network/start` or `/api/network/restart` (external provider only) for a relay with
  no deadline; that is an explicit setting and is shown in the relay status.
- A direct (hole-punched) Iroh path is not guaranteed; carrier-grade NAT often leaves
  the n0 relay as the path. A relay path is acceptable for small models.
- There is no self-hosted relay and no zero-third-party rendezvous in this build.

See also [LOCAL-FIRST.md](LOCAL-FIRST.md) for the offline / LAN modes.
