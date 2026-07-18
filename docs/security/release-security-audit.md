# Release security audit

Run the bounded, read-only release check from a repository root:

```bash
python3.14 scripts/release_security_audit.py --repo-root . --json
```

The audit inventories only Git-tracked working-tree files with `GIT_OPTIONAL_LOCKS=0`. It rejects unsafe index modes, tracked symlinks, credential-like tracked paths, private-key material, common high-confidence access-token prefixes, files larger than 16 MiB, and Python CLI declarations that accept secret values directly. Secret references ending in `-file`, `-path`, `-env`, `-fd`, or `-ref` remain allowed. Findings identify path, code, and line when available; they never echo matched secret bytes.

The command is deterministic and does not write product state, stage files, change the index, start peers, provision weights, or contact a network. It does not run inference. It does not evaluate authenticated transport, runtime hardening, untracked files, dependency vulnerabilities, history, ignored files, physical qualification, or semantic route evidence. Those remain separate gates.

Claim boundary is fixed even when the bounded audit passes:

```text
authenticated_transport_evaluated=false
dependency_vulnerabilities_evaluated=false
history_evaluated=false
physical_qualification_evaluated=false
runtime_security_evaluated=false
untracked_files_evaluated=false
route_ready=false
release_ready=false
```

`ok=true` means only that the tracked working-tree files passed this static policy. It cannot authorize integration, release, request serving, or a readiness transition.

Exit code is `0` only when no bounded static finding exists; otherwise it is `1`. Canonical JSON includes protocol `mycelium.release_security_audit.v1`, scan counts, sorted redacted findings, and the fixed claim boundary.
