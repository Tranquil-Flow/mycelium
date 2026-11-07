<!-- SPDX-License-Identifier: AGPL-3.0-only -->
# Reviewed candidate categories

This table describes the current continuation checkpoint, not whole-product
closure. Counts are disjoint **review claims** in `python-bounds.tsv`; predicates
may overlap, so they must not be summed by counting all reporter matches.
Unmatched candidates remain pending. A category never authorizes dropping the
underlying Python feature, Windows support, a real wait or nested enforcement.

## Non-bound / fixture categories

Every row below has a positive **real candidate** and an actual-wait negative
control in Rust test
`inventory_categories::reviewed_categories_match_real_rows_and_reject_unrelated_waits`.
The full inventory test also runs each claim's predicate against its exact
source candidate. `inventory_validation` rejects category relabeling of an
unrelated real row. Exact lists/conditions are in `src/inventory/categories.rs`.

| Category | Rows | Predicate / reviewed meaning | Real example |
|---|---:|---|---|
| string-join | 253 | Positional `.join` on a parsed string/bytes literal, not an arbitrary receiver | `model_adapters.py`: `', '.join(self.layer_count_fields)` |
| path-join | 73 | Exact qualified path or shell-token concatenators, not thread/process joins | `test_pyz_ledger_coverage.py`: `os.path.join` |
| boolean-state | 172 | Exact `True`/`False` in value slots; numeric timeout/interval/TTL/size names excluded | `demo_chain.py`: `released=False` |
| install-release-data | 297 | Lexical `install`/`release` collision with `stall`/`lease`, excluding bound words and embedded multiline programs | `mycelium_a13_linux/install_source.py`: `install_modes=INSTALL_MODES` |
| clock-provider | 22 | Callable-annotated, optional clock parameter with `None`, not a duration | `mycelium_a11_evidence_control.py`: `clock_ms` |
| clock-observation | 96 | Exact observation fields and known clock-sampling expressions | `managed_runtime.py:334`: `int(time.time() * 1000)` |
| absent-measurement | 41 | Explicit measurement-field allowlist with `None`; unknown measurement is not zero | `clock_offset.py`: `rtt_seconds=None` |
| protocol-label | 47 | Single-line string in protocol/reason/state identifier slots | `managed_control.py`: `TARGET_POLL_PROTOCOL` |
| schema-unit-label | 15 | Schema dictionaries in three named source files, with exact type/enum strings | `mycelium_product_spine/contracts.py`: `command_timeout_ms: 'count'` |
| packaged-unit-test | 33 | Three explicit packaged unittest modules, **test-only**, not a scripts-directory exemption | `test_pyz_ledger_coverage.py` |
| timestamp-context | 449 | Exact issue/start/observation/completion timestamp slots, excluding deadline/expiry fields | `compiler_inputs.py`: `now_unix_ms=0` |
| latency-observation | 211 | Exact observed/estimated/accumulated latency slots, not timeout/SLO caps or evidence qualification | `mycelium_a11_draft_runtime.py`: `draft_queue_ms=0.0` |
| planner-cost | 27 | Exact planner/model cost and propagation-speed inputs, not waits | `network_cost.py`: `propagation_km_per_ms=200.0` |
| fixture-data | 32 | Named fixture-builder scopes/path, data/keyword/default slots only; timeout/poll keywords excluded; **fixture-only** | `mycelium_replica_contracts.py`: fixture `issued_at_unix_ms=10000` |
| window-identity | 31 | Exact identifier/platform/version/Win32-flag fields, not a time-window allowance | `_win32_api.py`: `CREATE_NO_WINDOW=134217728` |
| windows-path-grammar | 5 | Exact `layer_assignment.py` path/parser slots; drive-component slicing is not history retention | `windows_path=PureWindowsPath(value)` |
| http-status-code | 7 | Pairing-window error map in `pairing.py`; exact 403/404/409/410 values | `pairing_window_unknown:404` |

These categories add 1,811 reviewed non-scalar/test/fixture sites. There are also
pre-continuation dispositions whose source-specific review remains preserved.
No embedded shell/Python program or arbitrary duration string is waived by the
string predicates. A phase-latency observation or p95-shaped field does not
satisfy measured-p99 qualification.

## Literal budgets

| Category | Rows/new IDs | Canonical unit | Independent real-row test |
|---|---:|---|---|
| numeric-category:milliseconds | 154 | Integral milliseconds, exact decimal conversion from source seconds | `report_interval=10.0` → 10000; `ttl_seconds=60.0` → 60000 |
| numeric-category:entries | 54 | Entry/attempt count, never seconds | `max_attempts=3`, `poll_attempts=10`, worker replacement ceiling 3 |
| numeric-category:bytes | 49 | Bytes, not entries or time | `app.py` `max_bytes=4096` |

Rust test `numeric_categories_keep_attempts_bytes_and_duration_units_distinct`
checks the real rows and rejects `propagation_km_per_ms` as a duration.
`decimal_conversion_is_exact_and_rejects_unrepresentable_values` checks exact
conversion, fractional-millisecond refusal and overflow.
`integer_product_categories_preserve_literal_byte_and_time_arithmetic` verifies
checked integer multiplication/powers on real source expressions; no arbitrary
Python evaluator exists in Rust. `per_token_byte_ratio_is_not_a_byte_capacity`
rejects a source bytes-per-token cap as an ordinary byte amount. Python `Decimal` was
used only for a scratch independent audit, not as a repository dependency.
`final_candidate::owner_stop_receipt_uses_attempt_count_not_a_duration_or_new_attempt`
checks the newly discovered final-source owner-stop receipt and declaration.

## Derived lexical edges

| Category | Rows | Predicate and evidence |
|---|---:|---|
| same-scope-parameter | 121 | Exact bare reference to a default-bearing parameter in the same lexical scope, with no local assignment/import shadow |
| same-scope-binding | 5 | Exact bare reference to an earlier, uniquely stored same-scope assignment |
| module-constant | 17 | Exact bare reference to a unique same-file module declaration, no enclosing local shadow |
| constructor-field | 59 | Exact `self.field` reference to a uniquely stored constructor assignment in the same class |

Two additional default-normalization categories are separate from alias rules:

| Category | Rows | Predicate and evidence |
|---|---:|---|
| parameter-float-normalization | 24 | Same-scope `float(parameter)` with a finite literal default; integer defaults outside exact f64 range are refused; source builtin shadowing checked |
| parameter-integral-normalization | 2 | Same-scope `int(parameter)` with an already integral literal default; a fractional default cannot be truncated under this rule |

All 228 edges carry an exact target candidate, original reference, rule and real
BudgetId relationship. `inventory_bindings` tests every real edge, rejects casts
under the ordinary alias rules and rejects cross-file targets, checks graph acyclicity, and mutates a relationship to a
different **valid same-unit** ID to prove that existence/unit checks alone do
not establish provenance. This control failed before binding enforcement and
passes with it. No unresolved value is filled with a default: these rows have
`disposition=derived` and an empty numeric value.

Static store/shadow facts were reviewed from hash-verified ASTs. Rust validates
the checked-in edge's structural/typed contract; it does not re-execute Python
or claim a complete dynamic alias/mutation proof. Source changes invalidate
these facts and require a new source-bound review.

## Read-set continuation categories

- `diagnostic-field`: 144 additional reviewed rows over 68 exact `(path, symbol)`
  pairs in `python-diagnostic-fields.tsv`. Value slots only; comparisons and
  actual wait arguments do not qualify. Includes measured/estimated latency,
  accumulated maxima, diagnostic retained counts and heartbeat message IDs,
  not enforced storage caps, heartbeat intervals or qualified p99 evidence.
  `inventory_continuation` checks real rows and foreign-path/timeout/comparison
  controls. The allowlist does not exempt the rest of any source file.
- `deadline-category:monotonic-literal`: 24 new source-backed IDs for the exact
  positive integral-millisecond offset in `time.monotonic() + <decimal>`.
  The mapped quantity is the **relative allowance**, never the absolute result.
  Standard `time` imports and absence of variable/argument shadowing were checked
  against pinned ASTs. Wall-clock addition, subtraction, dynamic offsets,
  sub-millisecond offsets, zero, negative values and overflow remain rejected.
  Unknown operational phases remain unclassified and cannot qualify without p99.

All 725 source byte hashes were rechecked against final-oracle Git blobs before
this continuation review. Source data was parsed, never imported/executed.

A further 32 deadline constructions are reviewed as source-linked derived edges:
20 module constants, 9 uniquely assigned constructor fields and 3 same-scope
parameters. These use `monotonic-offset-*` rules, require a duration target and
retain both the exact clock expression and the original relative source binding.
One additional same-scope edge propagates the newly reviewed workflow deadline.
No absolute clock sample is turned into a fixed numeric allowance. Clock/scope,
operator, extra-arithmetic and wrong-target controls are exercised in Rust.

`reviewed-state-field` adds 181 exact-source value-slot reviews over 155
`(path, scope, symbol)` triples in `python-state-fields.tsv`: 49 callbacks/clock
providers, 27 message/nonce/sequence fields, 20 status/error projections,
18 platform/ABI metadata fields, 15 filesystem handles/stat results and 52
observations/diagnostic fields. Each triple records its semantic rationale.
An optional callback is not an unlimited timeout. Filesystem ownership handles
are not temporal grants. Runtime counters and measured maxima do not become
new allowed limits. Rust tests reject foreign scopes, comparisons, timeout and
deadline fields, retry-budget consumption (`stalled_windows`) and storage caps.
These controls deliberately leave budget-consumption and control-flow cases
pending instead of treating every counter or boolean as diagnostic.

Manual discovery supplement: `mycelium_router/transports/iroh.py:183 @ ef9ae61d`,
`PrefillStallPolicy.max_resends = 2`, was missed by keyword discovery. The new
`PyPrefillMaximumResends` ID is an entry/attempt count, not time. `:1931,2125`
consume it and `:225` uses `(max_resends + 1)` attempts in the watchdog formula.
Both previously spent shared-client stalls and subsequent resends consume the
same count. The candidate census grows by one without changing source hashes.
The four dynamic `stalled_windows` rows remain pending, not waived. This finding
confirms the broad discovery queue is not proof of exhaustive whole-source coverage.

Comparison bindings add 74 reviewed sites: 52 module-constant comparisons,
4 same-scope-parameter comparisons, 2 constructor-field comparisons and 16
zero-validity/positive-branch checks on same-scope default-bearing parameters.
These reference the existing typed budget without inventing a numeric result
for a predicate. Strictness, any literal payload/positivity floor, and rejection
polarity remain in the exact source expression. Parameter defaults remain
caller-overridable. The rules reject equality substitutions, changed reference
names, non-comparison kinds and wrong bindings; all real edges run through the
existing acyclic graph, typed provenance and full-inventory checks. The
multi-dynamic-bound `build_qwen_live_route` range remains pending rather than
mapping both sides to only its upper bound.

## Interrupted continuation recovery

The inherited prefill/config/import/nonscalar slice is retained, not reverted.
`prefill-category:consumed-resends` binds exact spent-window sites to
`PyPrefillMaximumResends`; dimensionless stall multipliers are counts, while the
unmeasured RTT fallback is configured duration, never a measured profile.
Class-field, parameter-bound-float-normalization, imported-constant,
imported-float-constant, remaining-monotonic-local and typed-config-field edges
carry explicit declaration targets. Import/config evidence is in the corresponding
TSVs; this is lexical review, not transitive runtime mutation/authority proof.
`reviewed-nonscalar-slot` admits only full exact source slots listed in its TSV,
with per-slot semantic reasons. Actual nested caps/waits are not waived.

Recovery adds `reviewed-observation-slot`: every source identity field, expression,
kind and source snippet must match the explicit observation TSV, with a semantic
kind and review reason. Elapsed command results do not admit deadline/consumption
slots or constitute calibrated p99. `retention-category:literal-ceiling` requires
a pinned `len(collection) > integer` comparison, exact candidate identity, entry
units and a BudgetId whose provenance cites that exact source line. Same-valued
capacities in unrelated stores are not interchangeable. Four previously omitted
FIFO comparisons are manually added to discovery/census and bound to the existing
router retention capacity; supervisor settled-authority retention has its own ID.
The original failing retention/observation tests are preserved and now execute.


## Exact observation, upper-clamp, CLI and retained-object continuation

- `reviewed-observation-slot` now includes further event clock origins, elapsed/statistics outputs and telemetry records. Exact source identity remains mandatory; a time-looking name alone never qualifies. Deadline/allowance/threshold controls stay excluded.
- `clamp-category:literal-upper-seconds` admits only explicitly reviewed sites in `python-clamp-sites.tsv`. The Rust parser validates two-operand `min` with exactly one positive, exactly millisecond-representable literal; the ID represents the upper operand, not the dynamic result. A monotonic clock plus that clamp is a relative policy offset, never an epoch. Unsupported positive-floor nesting and malformed or negative/submillisecond caps are rejected. Each site has its own source-provenance ID.
- `binding-category:argparse-option` records an exact reviewed Namespace receiver/option/default edge in `python-argparse-bindings.tsv`. The option's declaration is unique; reviewed destinations have no writes. Parsed namespaces and explicitly forwarded helper parameters are lexical default provenance, not proof that user overrides equal a default. Five missed defaults were supplemented at the canonical AST **keyword** location, with scope cardinalities updated; existing inventory identities were not rewritten.
- Further `reviewed-nonscalar-slot` entries cover protocol labels, pairing document resets, retained maps/receipts and benchmark result records. Their numeric children/consumers remain independent. The router retired-authority FIFO guard at line 3334 has its own typed 256-entry capacity, not a mislabeled record nor an unrelated equal-valued retention ID.
- Positive/negative/mutation controls cover observation-vs-policy, exact bindings, clamp parsing/provenance, and retained-record-vs-capacity distinctions. Zero/None placeholders remain unmeasured; no gate threshold, assertion, quota or deadline was relaxed.


## Typed object/boolean and explicit-quantity continuation

This slice adds 272 reviewed sites: 176 exact nonscalar/callback/absent-result slots,
45 exact boolean-state comparisons, 45 source-provenance quantities, and six lexical
budget edges. It does not make the inventory complete.

`reviewed-state-comparison` requires a registered exact source identity plus boolean
`is`/`is not` semantics. It cannot admit attempt equality, retained-size ceilings,
zero resource-count requirements or an elapsed-time comparison. A positive state test
does not waive its enclosing qualification gate.

`quantity-category:literal` and `quantity-category:clock-offset` use
`python-quantity-sites.tsv` to bind units to reviewed semantics instead of inferring
from ambiguous names such as `window` or `maximum_size`. Clock rows map only the
relative offset in seconds/milliseconds, never the dynamic absolute timestamp.
Examples: limiter `window=60.0` is seconds, benchmark `--windows=3` is entries,
file-key `maximum_size=32` is bytes, and a maximum RTT field is a configured validation
ceiling rather than measured RTT. Submillisecond/dynamic offsets and unregistered
rows do not qualify. Source-specific BudgetId provenance is checked, including a
mutation to a different ID with the **same unit and value**.

Reviewed callable inputs to wait helpers and first asyncio operands are not duration
arguments; their independently inventoried timeout/poll values remain enforceable.
Object/registry wrappers do not waive nested scalar policies. Optional diagnostic
results carry no freshness/measurement claim; actual optional caller deadlines,
unbounded waits and numeric consumption remain pending. A dead-looking `Bench.window`
field with no established unit was deliberately left unresolved rather than guessed.


## Optional/default, clamp-reference and range-policy continuation

This slice adds **233 reviewed sites**: 76 source-linked bindings, 92 direct
quantity mappings, 35 nonscalar slots and 30 observations. It also supplements
79 previously missed candidates; unresolved dynamic operands remain pending.

- `validated-parameter-default` records an exact normalization helper/witness
  and default-bearing parameter. The reviewed helper preserves that default;
  it does not establish that an arbitrary caller value equals the default.
  `optional-parameter-default` records an explicit `None` fallback and its
  declaration. `resolved-optional-value` follows the exact selection expression,
  retaining dynamic caller input and refusing unrelated/default-free bindings.
- `upper-cap-reference` and `lower-floor-reference` use balanced two-argument
  `min`/`max` parsing and exact operand evidence. A lower floor is not an upper
  deadline. A `time.monotonic() + constant` upper operand maps its relative
  allowance, never a fixed absolute timestamp. Import/re-export edges and
  propagated aliases preserve exact declaration identities and graph provenance.
- `quantity-category:literal-floor` registers the positive literal operand of
  an exact `max` expression. `retention-tail` registers a `[-N:]` entry count,
  not a duration. IDs cite each exact source site; equal values elsewhere are
  not interchangeable policies.
- `upper-guard` registers the literal upper acceptance/retention threshold of an
  exact comparison; polarity, strictness and the whole original expression stay
  pinned. Chained positive lower literals have separate `comparison-lower`
  candidates and `range-floor` IDs. Chained reference floors are independently
  supplemented and either source-bound or still pending. Zero positivity/domain
  checks remain in the original expression, not fabricated positive allowances.
- Five `select.select` descriptor-list operands are records, not timeouts. All
  five fourth operands were newly inventoried: zero is explicitly nonblocking
  (`nonblocking-seconds`, exactly zero seconds); twenty seconds has its own ID;
  the detach constant is source-linked; two dynamic values remain pending.
  `None`/unbounded waiting is not admitted as zero/nonblocking.
- Three expiry-list comprehensions exposed previously missed enforcement
  comparisons in relay, scheduler and terminal-KV retention. Their collection
  rows are now nonscalar, but each scalar expiry comparison is newly inventoried
  and remains pending. Tests require those guards to exist and reject their
  classification as either observations or records. Tuple policies such as
  `AppDemoChain.budgets` are deliberately **not** blanket-exempted. A subsequent
  read-only audit of all 725 pinned files found 43 further heuristic comprehension
  candidates, including expiry, retention, reservation capacity and RTT-size
  guards alongside schema false positives. All 43 are now explicitly queued as
  pending, not silently admitted or claimed as 43 confirmed true bounds.
- Additional observations are exact diagnostic/event/estimated-cost slots,
  including the maximum frame actually seen, not transport capacity. Initial
  zero and reported/predicted values are not measured p99. Consumed deadline
  deductions and planned resource capacities remain excluded.

All new rules have exercised positive and mutation/negative controls, including
same-valued wrong-provenance IDs, changed operands/scopes/kinds, floor/ceiling
confusion, payload/timeout separation and hidden expiry enforcement. These are
source-review claims, not transitive runtime authority, whole-source discovery,
consumer-port integration, physical qualification or release closure.

## Retention, thresholds and exact result/state witnesses (continuation)

- Relay and scheduler expiry guards are linked to their local retention operand,
  with explicit `self.config` receiver/default provenance. `max(0.0, policy)` is
  a configurable clamp, not permission to treat the whole retained lifetime as a
  fixed scalar. Wrong receivers, `min`, and a changed floor are rejected.
- Terminal-KV expiry reads the exact deadline stored by `hold`; equality expires.
  The stored deadline is `min(now + active seconds, kv_lease_deadline(state))`.
  Its configured-seconds edge is **only one operand**, not authority to discard
  the lease ceiling. The A7→runtime→retention composition records selected default
  provenance, not a universal default on required arguments or a caller-mutation
  proof. Constructor zeros mean disabled; `enable` supplies active policy.
  Capacity eviction and positive eligibility retain independent reviewed edges.
- Exact zero and positive lower-domain comparisons have separate provenance IDs.
  Zero is only a validation/eligibility boundary, never an invented runtime wait;
  lower floors are not upper caps. Predicate direction and strictness are retained.
- Structural witnesses match every source-identity field and one reviewed role.
  Schema/identity/evidence containers cannot exempt adjacent expiry, freshness or
  capacity enforcement. Inactive sentinels likewise require an exact reviewed
  slot; `timeout=None`, unbounded waits and empty runtime collections do not pass.
- Exact read-only diagnostic/result projections and explicitly modeled costs do
  not create execution allowances or measured p99. Their underlying policies,
  signed authority encodings, budget consumption and enforcement stay separate.
  A nested quarantine-staleness comparison discovered during projection review
  is independently inventoried and linked to its own module threshold.
- The census delta and relocation tables retain the old source identities while
  pinning changed file hashes. A newly introduced route snapshot offset has its
  own current-source witness; it cannot be backdated. Historic rows cannot claim
  the newer review pin. A later census refresh is distinct from semantic review
  of new operands and is not a claim of exhaustive current-source discovery.

## Compound quantities, provenance and bounded selections (continuation)

- Checked integer products and single left shifts preserve exact scalar quantities;
  overflow, mixed operators, dynamic operands and fractional operands are refused.
  Chained zero floors and upper operands remain distinct source sites. Byte-rate
  eligibility is bytes/second, never bytes or milliseconds.
- Imported comparison/cap operands bind their exact declaration and import witness.
  Constructor/config references retain receiver ownership and caller overrides.
  `monotonic-upper-cap-reference` maps only the relative `min` allowance beneath
  the exact outer monotonic clock; changing the clock, multiplier or operand fails.
  Conditional same-scope tightening is reviewed across both reaching writers;
  unrelated reassignment is not an alias proof.
- `reviewed-nonwaiting-operation` uses an exact caller plus callee source witness:
  native event polling uses `NSDate distantPast`, and the exclusive runner lock
  uses `LOCK_NB`. Neither proves zero execution time, bounded filesystem latency,
  bounded callbacks, physical lifecycle completion or absence of other waits.
- `modeled-cost-input` marks exact injected/fallback cost arguments, not durations
  granted to execution. Adjacent capacity limits remain independently inventoried.
  Named migration checkpoint selectors are schema values, not timeout defaults.
- `resolved-poll-default` preserves the explicit sidecar selection
  `self._timeout if single_poll or timeout is None else timeout`. The selected
  local allowance can feed a monotonic offset, but has no fixed absolute scalar.
  Nullable `_request` deadlines and its unbounded-lock branch remain excluded.
- All nineteen AppDemoChain scalar defaults were independently checked, including
  the differently dimensioned verification rate. The policy-record candidate
  remains pending: the inherited refusal of a generic nonscalar exemption is
  preserved. Caller override expansion, dynamic-key consumers and progress-renewed
  windows must not be converted into a whole-operation grant.
- The Windows replace backoff tuple has six independently typed delay members
  and a separately tracked retry-attempt count. `schedule-maximum` verifies the
  exact saturating-index expression, tuple identity and checked decimal maximum;
  it gives a per-sleep argument ceiling, not an actual wakeup, native-I/O or total
  operation duration. Wrong members, index changes and `max` substitution fail.

These are static source/default/provenance reviews. They do not close the remaining
inventory gate, calibrate p99, port consumers or qualify the product/release.

## Partition-A final-blob continuation

- Newly reviewed rows retain an exact `python-final-review-sites.tsv` crosswalk to
  `f4e366e98a1f705dfaf182897044500d290dbd7f`. Historical records/pins remain unchanged;
  the final pin cannot backdate a new review or relabel a historic row. This is a
  semantic witness manifest, not another corpus/census replacement.
- Swarm peer/job inputs have independent defaults, inclusive 1-count floors,
  validation ceilings and successful-path values. A history floor that references
  one of those inputs is not the history ceiling and is not the fixed default.
  Required runner inputs similarly inherit only their actual rejecting validation.
- Exact constructed policy readouts admit keyword/assignment serialization slots,
  not the adjacent enforcement paths or entire policy-record constructors.
- `reviewed-composed-ceiling` records both exact source identities plus the min
  operation in `python-composed-ceilings.tsv`. Independent decimal cap operands
  remain integral milliseconds; min caps, nested floors and absolute clock results
  are distinct. Supported relative-offset/backoff compositions preserve successful
  scalar argument ceilings, never scheduler, callback, syscall or whole-job timing.
  Other source identities, operands, min placement and same-unit wrong IDs fail.
- Newly discovered source operands increment only per-path candidate counts; all
  725 census hashes and all old candidate records are retained. The managed-runtime
  historical twenty-site census is checked separately from its two new operands.
- `reviewed-retry-state` binds exact source/target witnesses to the outbound retry
  branch, HTTP retry-loop waits, conditional deadline tightening, final-attempt
  comparison and conditional capped backoff. The full pinned AST's reaching
  writers are inspected; a cadence default cannot replace a retry ceiling. The
  attempt guard remains an entry count and does not become a duration. Clock-plus-
  backoff is only relative allowance provenance. Required and unchecked timeout
  inputs remain outside these rules; no runtime mutation or wakeup-latency bound
  is claimed.
