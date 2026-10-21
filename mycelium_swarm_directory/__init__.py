# SPDX-License-Identifier: AGPL-3.0-only
"""Swarm directory (phonebook), owner-signed access grants and entry enforcement.

* ``wire``      canonical JSON + Ed25519 signature rules (portable to JS).
* ``docs``      the closed set of signed document types: build + validate.
* ``directory`` the phonebook service core (pure; storage and crypto injected).
* ``store``     ``MemoryStore`` / ``SqliteStore`` behind a 4-method KV interface.
* ``entry``     the swarm-entry guard: grant signature, expiry, quota, rates.
* ``server``    stdlib HTTP server for the directory API and ``web/`` assets.

Trust model: the directory is only a phonebook.  Access is enforced by the
swarm's own entry node (``entry.EntryGuard``), which trusts only grants signed
by the swarm owner's key (or, for open swarms, by the owner-listed entry key).
"""
