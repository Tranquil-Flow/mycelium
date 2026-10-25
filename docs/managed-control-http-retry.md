# Managed-control proxy retry boundary

The managed HTTPS client retries an unstructured HTTP 502, 503, 504 or 530 only
on `/managed-control/enqueue`, `/managed-control/poll`, `/managed-control/ack`,
`/managed-control/result`, `/managed-control/token-progress`,
`/managed-control/staging/read` and `/managed-control/staging/cleanup/read`. Those routes
have signed-request deduplication or read-only lookup semantics. Other POST
routes do not gain retry authority.

The staging acknowledgement routes remain excluded: their matching-ack checks
are not a request-ID response replay cache. Staging reads recheck current
membership, offer expiry and cleanup state on each attempt; a later refusal
still stops the retry.

Bounds in `managed_control.py`:

- `HTTP_RETRY_ATTEMPTS = 8` total attempts, not eight additional retries.
- `HTTP_RETRY_WINDOW_SECONDS = 10.0`: one monotonic HTTP/body/backoff budget,
  intersected with an earlier enclosing owner-command deadline.
- Backoff starts at `HTTP_RETRY_INITIAL_SECONDS = 0.1`, doubles after each retry,
  and caps at `HTTP_RETRY_MAX_SECONDS = 1.0`. For immediate errors the waits are
  0.1, 0.2, 0.4, 0.8, 1, 1, 1 seconds. Slow I/O consumes the same budget.
- Socket timeout is no greater than the remaining budget and the client timeout;
  body reads recheck the absolute deadline. Owner cancellation interrupts waits.

All retries keep the same signed envelope, request ID, sequence, nonce and TTL.
No native command is re-executed by the retry layer. A lost reply after a mailbox
commit is recovered by that mailbox's existing exact-request replay. The durable
caller still owns the prepared envelope and native outcome. This is not an
exactly-once guarantee across arbitrary server storage loss or process crashes.

Every 4xx remains fatal at this boundary. Structured JSON refusals are never
retried, including an explicit `managed_control_remote_error` code, a rewritten
Content-Type, or noncanonical JSON carrying an error. The existing canonical
rules still decide which authority error code is exposed. Unknown statuses,
incomplete error bodies and unreviewed routes remain fail-closed.

The public relay's own errors now use canonical `{"error":{"code":"..."}}`
JSON with `Content-Type: application/json`, preserving their HTTP status.
In particular, `relay_backend_unavailable` is exposed by name but is **not**
retryable. Any exception to the JSON-refusal policy needs a separate decision.

Exhaustion raises the existing `managed_control_remote_error`, retaining HTTP
status and route. The pump consequently enters its existing bounded
`control_<operation>_reconciliation_required` terminal; it does not continuously
re-sign after expiry. Existing non-HTTP transport recovery policies are unchanged.
The private runtime diagnostic retains an integer HTTP status and one of the
retry-allowlisted constant route names through at most eight cause-chain nodes. Query strings,
fragments, origins, arbitrary paths, response bodies and headers are excluded.

Demo-target status also exposes `error_http` (or null after recovery): a typed
HTTP status, an exact allowlisted managed-control route, and a bounded code.
It rejects URLs, queries and fragments rather than displaying transport input.
Both joined-Mac status and the owner's local contributor status carry this
field; remote member reports retain their existing closed wire schema.

The owner managed-control client, local contributor and gateway managed sessions
share this boundary. Token progress delivery uses the same exact replay path.
A keeper must distinguish transient request retry from an exhausted/blocked
runtime: waiting for new commands cannot revive a closed pump. Recovery remains
an explicit supported stop/cleanup/new-run operation, subject to consent and
resource policy. Never replay uncertain native work or relax a refusal to make
the keeper green. A chat health check requires a completed terminal, not merely
some tokens or an HTTP 200. Operator keeper changes are separate from this patch.
