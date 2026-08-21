# A8 public HTTPS bootstrap — deployment template

Status: **design_only**. This directory is deployment scaffolding for the A8
physical gates. Nothing here has been executed against live infrastructure,
and nothing here grants A8 qualification.

## What this is

The reverse proxy template terminates the publicly trusted certificate for
the one canonical `https://host[:port]` origin and forwards ONLY the five
closed routes to the loopback seed listener:

- `GET /seed/identity`
- `GET /seed/rotation`
- `POST /seed/join`
- `POST /seed/resume`
- `POST /seed/message`

Everything else — operator administration, invite minting, revocation,
backups, raw audit records, static files, directory listings, websocket
upgrades — is rejected. Cleartext HTTP on port 80 is refused with 444
(connection closed), never redirected: Mycelium clients never follow
redirects and the boundary never offers one.

## Certificate metadata

- Certificate: publicly trusted, currently valid, issued for
  `__A8_PUBLIC_HOSTNAME__` via an automated ACME renewal path
  (e.g. certbot/lego) or an operator-managed equivalent.
- Renewal grants **no seed authority**. The seed is authenticated only by
  its signed identity and the invitation-bound verification-key pin
  (spec §10.1 `seed_key_pinning`). A fresh certificate must never repin the
  seed key.
- Secrets (private keys, ACME credentials, provider account identifiers)
  are excluded from this template and live only in the deployment host's
  protected configuration.

## Wire-up

1. Substitute `__A8_PUBLIC_HOSTNAME__` and `__A8_SEED_LOOPBACK_PORT__`
   (default 8876 per `release/service-configs/seed.json`).
2. Start the seed listener bound to loopback with the public origin:
   `SeedHTTPServer(..., host="127.0.0.1", port=<loopback port>,
   public_seed_url="https://__A8_PUBLIC_HOSTNAME__",
   policy=PublicBootstrapPolicy(canonical_origin="https://__A8_PUBLIC_HOSTNAME__"))`.
3. Mint one owner-delivered invite (owner-private administration plane);
   its `seed_url` must equal the canonical public origin.

## Verification before any physical gate

- `GET /seed/identity` over the public origin returns the signed envelope
  with `seed_url == canonical origin`; verify the signature against the
  invitation pin.
- `http://__A8_PUBLIC_HOSTNAME__/...` is refused (444), no redirect.
- Any non-allowlisted path/method returns a bounded error; the seed DB
  shows no new member.
- Responses carry `Cache-Control: no-store` and `Content-Type:
  application/json`.
