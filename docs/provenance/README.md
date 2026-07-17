# Provenance

`baseline-manifest.sha256` seals the source/doc/test tree in the first repository commit. It excludes itself to avoid a recursive digest, plus all paths ignored by `.gitignore`.

Imported source from other Mycelium worktrees must enter through an explicit import manifest containing source host/path, source SHA-256, destination path, review status, and verification command. Broad directory overlays are not accepted.

Generated caches, internal planning files, handovers, local run artifacts, model weights, credentials, and device tokens remain outside version control. Curated qualification evidence may be added later only after privacy review and immutable manifesting.
