# Scrubbed history and sealed digests

The public history is the project's development history with personal home-folder paths and one private
host name replaced by same-length placeholders (`/Users/mycelium-dev`, `/Users/joinmac`, `member-mac-host`).
No product source file changed (nothing under `crates/*/src`, no `build.rs`, no Cargo manifest or lockfile).

Digests inside the sealed records in `evidence/` and `runs/` (for example `evidence/foundation-exit/seal.json`
and the `runs/v2/v02/FOUNDATION-EXIT-*-evidence-manifest.json` files) were computed over the private,
pre-scrub bytes. They are kept unchanged as historical records, so they no longer match the scrubbed files
they name. Only digests that tests assert were re-pinned, in one commit that lists each old and new value.
