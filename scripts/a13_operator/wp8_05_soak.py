# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-05 soak: >=100 requests against the live route, with the failure counters the criterion names.

PLAN.md section 3 criterion 5 asks for a soak measured by absence: >=100 requests, >=10 cancellations,
restarts, one network change, and -- the numbers that matter -- ZERO stuck requests, ZERO control-loop
deaths, ZERO duplicates.

WHY THIS RUNS AT THE API LEVEL RATHER THAN THROUGH THE CHAIN: the pinned stream step asserts
`route_ready is True` at a moment when readiness trails the deployment's qualification, so the CHAIN still
fails its own assert on a route that is demonstrably serving. That assert lives in the chain, not in the
route, so it does not gate a soak conducted against the API. The measurement driver (wp8_05_measure_stream.py)
already proved the route answers once the deployment qualifies -- 82r1 read `route_ready: true` for the
first time in this lineage, and criterion 6's TTFT/TPOT numbers came from exactly this client.

THE COUNTERS, and each is defined so it cannot be quietly satisfied:
 * stuck        -- a request that neither produced a terminal event nor was cancelled within the deadline.
                   Counted from the client's own view, because that is what a user experiences.
 * duplicate    -- two completed requests sharing a request_id, or two accepted records with the same
                   event path. Compared by identity, not by counting rows.
 * loop_death   -- the serving status stops answering during the soak, or the app's surface disappears,
                   sampled between requests. A soak that "completed" while the control loop died is not a
                   soak that passed.
 * cancelled    -- cancellation was REQUESTED; whether it took effect is recorded separately as
                   cancelled_confirmed, because a cancellation that silently does nothing is the exact
                   failure a soak exists to catch.

EVIDENCE: every request's arrival offsets, tokens, terminal type and timing, plus the counters, written to
one JSON per soak label -- the same round-specific-path discipline the measurement driver learned the hard
way (a constant output path makes a stale file look like a fresh result).
"""

import argparse
import json
import os
import threading
import time
import urllib.error

# the two soak/measurement drivers live side by side in scripts/a13_operator, which is not on sys.path
# when this module is imported by a test or run from another cwd -- so the sibling is resolved explicitly
# rather than relying on the caller's PYTHONPATH.
import pathlib as _pathlib
import sys as _sys
_sys.path.insert(0, str(_pathlib.Path(__file__).resolve().parent))
from wp8_05_measure_stream import stream as _stream

# PLAN §3 criterion 5, verbatim: ">=100 requests (sequential and concurrent), >=10 cancellations, one
# restart of each Mac's worker, one network change. Zero stuck requests, zero control-loop deaths, zero
# duplicate native executions." The numbers live here so a verdict cannot drift from the criterion.
CRITERION_MIN_REQUESTS = 100
CRITERION_MIN_CANCELLATIONS = 10
# The driver mode that can actually CONFIRM a cancellation: ask via the product's own cancel endpoint
# (DELETE <cancel_path>) and keep reading for its terminal. 'disconnect' closes the stream early, which
# exercises the product's infer_cancel_wait path but cannot produce a confirmation.
CANCEL_MODE = 'endpoint'


def cancellation_confirmed(summary):
    """A cancellation counts ONLY when the product's own terminal says `cancelled`.

    Nothing else confirms it: not the client's request, not the endpoint's 202, not the absence of
    tokens. A cancel that silently does nothing must stay visible as unconfirmed -- that is what
    criterion 5's >=10 cancellations is measuring.
    """
    return summary.get('terminal') == 'cancelled'


def summarize(records, *, expected=None, loop_deaths=0, max_in_flight=0):
    """Fold per-request records into criterion 5's counters. Pure function: same records, same verdict.

    THE COUNTERS MUST NOT BE SATISFIABLE BY ACCIDENT. `ok` means "the stream produced tokens", which is
    NOT the same as "the request succeeded": a request that emits one token and then terminalises as
    `failed` -- including the product's own `failed/request_cleanup_unproven`, where the route says it
    could not prove owner-scoped cleanup -- used to count as *completed* here. That is a whitewash in
    the instrument that measures criterion 5, so `succeeded` (a `completed` terminal) is counted
    separately from `ok`, an `ok` request with no terminal at all is counted as `unterminated`, and
    `stuck_total` adds those to the stuck count. `met_criterion_5` requires all of them to be zero.
    """
    total = len(records)
    completed = [r for r in records if r.get('ok')]
    succeeded = [r for r in records if r.get('succeeded')]
    failed_terminals = [r for r in records if r.get('terminal') == 'failed']
    cleanup_unproven = [
        r for r in records if r.get('terminal_code') == 'request_cleanup_unproven'
    ]
    # tokens were emitted but the stream never terminalised: a hang the old counter could not see.
    unterminated = [r for r in records if r.get('ok') and not r.get('terminal')]
    stuck = [r for r in records if not r.get('ok') and not r.get('cancelled_requested')]
    cancelled = [r for r in records if r.get('cancelled_requested')]
    confirmed = [r for r in cancelled if r.get('cancelled_confirmed')]
    seen, duplicates = {}, []
    for r in succeeded:
        rid = r.get('request_id')
        if rid in seen:
            duplicates.append(rid)
        else:
            seen[rid] = True
    latencies = sorted(r['total_s'] for r in succeeded if r.get('total_s'))
    def pct(p):
        if not latencies:
            return None
        return latencies[min(len(latencies) - 1, int(p / 100.0 * len(latencies)))]
    failing = stuck + unterminated + failed_terminals
    summary = {
        'protocol': 'mycelium.a13.soak.v1',
        'requests': total,
        'completed': len(completed),
        'succeeded': len(succeeded),
        'failed_terminals': len(failed_terminals),
        'cleanup_unproven': len(cleanup_unproven),
        'unterminated': len(unterminated),
        'stuck': len(stuck),
        'stuck_total': len(stuck) + len(unterminated),
        'cancelled_requested': len(cancelled),
        'cancelled_confirmed': len(confirmed),
        'duplicates': len(duplicates),
        'stuck_request_ids': [r.get('request_id') for r in stuck][:5],
        'failure_request_ids': [r.get('request_id') for r in failing][:5],
        'cleanup_unproven_request_ids': [r.get('request_id') for r in cleanup_unproven][:5],
        'duplicate_request_ids': duplicates[:5],
        'p50_total_s': pct(50), 'p95_total_s': pct(95),
        'expected': expected,
        'met_floor': (total >= expected) if expected else None,
        'loop_deaths': loop_deaths,
    }
    summary['criterion_5'] = {
        'zero_stuck_requests': summary['stuck_total'] == 0,
        'zero_cleanup_unproven': summary['cleanup_unproven'] == 0,
        'zero_failed_terminals': summary['failed_terminals'] == 0,
        'zero_duplicates': summary['duplicates'] == 0,
        'zero_loop_deaths': loop_deaths == 0,
        'requests_met': total >= CRITERION_MIN_REQUESTS,
        'cancellations_met': len(confirmed) >= CRITERION_MIN_CANCELLATIONS,
        # MEASURED in-flight high-water mark, never the configured number: the criterion asks for
        # requests "sequential and concurrent", so a run with one request in flight at a time has not
        # exercised the concurrent half however it was configured.
        'concurrent_requests_met': max_in_flight >= 2,
        # NOT measurable by this driver, and named here rather than left implied:
        'unmeasured_by_this_driver': [
            'one restart of each Mac worker',
            'one network change',
        ],
    }
    summary['met_criterion_5'] = all(
        value for key, value in summary['criterion_5'].items()
        if key != 'unmeasured_by_this_driver'
    )
    return summary


ADMITTED_MAX_CONCURRENT_REQUESTS = 4
"""The concurrency the product ADMITS: the A4 qualification installs
``maximum_concurrent_requests: 4`` (mycelium_live/a4_install.py:160). Criterion 5 asks for requests
"sequential and concurrent", so the soak runs both phases -- and refuses a concurrency the product never
agreed to serve, because measuring a load the fleet did not admit is not a measurement of this pilot."""


class _Inflight:
    """High-water mark of requests ACTUALLY in flight, so the concurrent claim is measured rather than
    configured. A configured number is a statement about the harness; this is a statement about the run."""

    def __init__(self):
        self._lock = threading.Lock()
        self.current = 0
        self.maximum = 0

    def enter(self):
        with self._lock:
            self.current += 1
            self.maximum = max(self.maximum, self.current)

    def leave(self):
        with self._lock:
            self.current -= 1


def _attempt(base, index, *, phase, prompt, max_new_tokens, timeout, cancel_every, inflight,
             sample_status):
    """One request's record. SHARED by both phases so the sequential and concurrent halves of the
    criterion cannot drift apart in what they measure."""
    record: dict = {'attempt': index, 'phase': phase}
    if sample_status is not None and not sample_status():
        record['loop_death_observed'] = True
    do_cancel = bool(cancel_every and index % cancel_every == 0)
    record['cancelled_requested'] = do_cancel
    inflight.enter()
    try:
        # CANCELLATIONS ARE WIRED, AND CONFIRMED ONLY BY THE PRODUCT. The driver asks through the
        # product's own cancel endpoint (DELETE <cancel_path>, asgi.py:260/:336) and then KEEPS
        # READING, because the product's `cancelled` terminal is the only thing that confirms one
        # (see cancellation_confirmed above). Criterion 5 wants >=10 confirmations, and a soak that
        # counted its own request as the confirmation would hide exactly the failure it measures.
        # THE DRIVER RETURNS (samples, summary) -- A TUPLE, and `record.update(tuple)` raises
        # ValueError("dictionary update sequence element #0 has length ..."). That exception was
        # swallowed by this block's own `except Exception` handler, so EVERY request was recorded as
        # ok=False with a ValueError detail and the soak could not measure anything at all. Unpack.
        _samples, summary = _stream(base, prompt, max_new_tokens, timeout=timeout,
                                    cancel_after_s=2.0 if do_cancel else None,
                                    cancel_mode=CANCEL_MODE if do_cancel else 'disconnect')
        record.update(summary)
        if do_cancel:
            record['cancelled_confirmed'] = cancellation_confirmed(summary)
            if not record['cancelled_confirmed']:
                record['cancelled_note'] = (
                    'unconfirmed by the product: cancel_status=%s terminal=%s'
                    % (summary.get('cancel_status'), summary.get('terminal')))
    except urllib.error.HTTPError as exc:
        record.update({'ok': False, 'error': 'HTTPError', 'detail': 'HTTP %s' % exc.code})
    except Exception as exc:
        record.update({'ok': False, 'error': type(exc).__name__, 'detail': str(exc)[:160]})
    finally:
        inflight.leave()
    return record


def run_soak(base, *, requests=110, cancel_every=10, prompt='Describe the city of Berlin in three sentences.',
             max_new_tokens=32, timeout=180, label='soak', sample_status=None, out_dir='.',
             concurrency=ADMITTED_MAX_CONCURRENT_REQUESTS):
    """Issue `requests` requests: the first half SEQUENTIALLY, the second half with `concurrency` in
    flight. Criterion 5 asks for requests "sequential and concurrent", so both phases run in one
    invocation and their records fold into one summary; `max_in_flight` reports what was measured."""
    if concurrency < 1 or concurrency > ADMITTED_MAX_CONCURRENT_REQUESTS:
        raise ValueError('concurrency_not_admitted:%r (the A4 qualification admits at most %d)'
                         % (concurrency, ADMITTED_MAX_CONCURRENT_REQUESTS))
    records, loop_deaths = [], []
    lock = threading.Lock()
    inflight = _Inflight()
    tail = requests // 2
    sequential_end = requests - tail
    first_concurrent = sequential_end + 1

    def work(indices, phase):
        for i in indices:
            record = _attempt(base, i, phase=phase, prompt=prompt, max_new_tokens=max_new_tokens,
                              timeout=timeout, cancel_every=cancel_every, inflight=inflight,
                              sample_status=sample_status)
            with lock:
                records.append(record)
                if record.get('loop_death_observed'):
                    loop_deaths.append(i)
                print('soak %d/%d %s ok=%s total_s=%s'
                      % (i, requests, phase, record.get('ok'), record.get('total_s')), flush=True)

    work(range(1, sequential_end + 1), 'sequential')
    if tail:
        if concurrency > 1:
            workers = [threading.Thread(
                target=work,
                args=(range(first_concurrent + offset, requests + 1, concurrency), 'concurrent'),
                daemon=True)
                for offset in range(min(concurrency, tail))]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
        else:
            work(range(first_concurrent, requests + 1), 'concurrent')

    summary = summarize(records, expected=requests, loop_deaths=len(loop_deaths),
                        max_in_flight=inflight.maximum)
    summary['loop_death_at'] = loop_deaths[:5]
    summary['label'] = label
    summary['concurrency'] = concurrency
    summary['max_in_flight'] = inflight.maximum
    summary['phases'] = {'sequential': sequential_end, 'concurrent': requests - sequential_end}
    path = os.path.join(out_dir, 'soak-%s.json' % label)
    with open(path, 'w') as fh:
        json.dump({'summary': summary, 'records': records}, fh, indent=1)
    return summary, path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True, help='the live registry origin, e.g. http://127.0.0.1:54191')
    ap.add_argument('--requests', type=int, default=110)
    ap.add_argument('--cancel-every', type=int, default=10)
    ap.add_argument('--max-new-tokens', type=int, default=32)
    ap.add_argument('--out-dir', default='.')
    ap.add_argument('--label', default=os.environ.get('A13_LOG_LABEL', 'soak'))
    ap.add_argument('--concurrency', type=int, default=ADMITTED_MAX_CONCURRENT_REQUESTS,
                    help='in-flight requests for the concurrent half; the product admits at most %d'
                         % ADMITTED_MAX_CONCURRENT_REQUESTS)
    args = ap.parse_args()
    summary, path = run_soak(args.base, requests=args.requests, cancel_every=args.cancel_every,
                             max_new_tokens=args.max_new_tokens, label=args.label,
                             out_dir=args.out_dir, concurrency=args.concurrency)
    print(json.dumps(summary, indent=1))
    print('SOAK_WRITTEN', path)


if __name__ == '__main__':
    main()
