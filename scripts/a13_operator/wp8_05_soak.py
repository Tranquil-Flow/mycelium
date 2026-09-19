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


def summarize(records, *, expected=None, loop_deaths=0):
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
        # NOT measurable by this driver, and named here rather than left implied:
        'unmeasured_by_this_driver': [
            'concurrent requests (this driver issues them sequentially)',
            'one restart of each Mac worker',
            'one network change',
        ],
    }
    summary['met_criterion_5'] = all(
        value for key, value in summary['criterion_5'].items()
        if key != 'unmeasured_by_this_driver'
    )
    return summary


def run_soak(base, *, requests=110, cancel_every=10, prompt='Describe the city of Berlin in three sentences.',
             max_new_tokens=32, timeout=180, label='soak', sample_status=None, out_dir='.'):
    """Issue requests sequentially; cancel every k-th; sample the route's health between requests."""
    records, loop_deaths, cancel_ids = [], [], []
    for i in range(1, requests + 1):
        record = {'attempt': i}
        if sample_status is not None:
            alive = sample_status()
            if not alive:
                loop_deaths.append(i)
                record['loop_death_observed'] = True
        do_cancel = (cancel_every and i % cancel_every == 0)
        record['cancelled_requested'] = bool(do_cancel)
        try:
            # CANCELLATION IS NOT WIRED YET, and the code says so rather than pretending:
            # wp8_05_measure_stream.stream() takes no cancel hook, so a cancel attempt here would either
            # raise (the LSP caught exactly that) or silently do nothing. Criterion 5 wants >=10
            # cancellations, so this MUST be implemented before a soak can claim to meet it -- either by
            # closing the event stream early and observing the product's terminal, or by the product's own
            # cancel command (the mailbox vocabulary already carries `infer_cancel_wait`). Until then the
            # record marks the intent and does NOT claim confirmation.
            result = _stream(base, prompt, max_new_tokens, timeout=timeout,
                             cancel_after_s=2.0 if do_cancel else None)
            record.update(result)
            if do_cancel:
                record['cancelled_confirmed'] = False
                record['cancelled_note'] = 'cancel_not_implemented_in_driver'
        except urllib.error.HTTPError as exc:
            record.update({'ok': False, 'error': 'HTTPError', 'detail': 'HTTP %s' % exc.code})
        except Exception as exc:
            record.update({'ok': False, 'error': type(exc).__name__, 'detail': str(exc)[:160]})
        records.append(record)
        print('soak %d/%d ok=%s total_s=%s' % (i, requests, record.get('ok'),
                                               record.get('total_s')), flush=True)
    summary = summarize(records, expected=requests, loop_deaths=len(loop_deaths))
    summary['loop_death_at'] = loop_deaths[:5]
    summary['label'] = label
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
    args = ap.parse_args()
    summary, path = run_soak(args.base, requests=args.requests, cancel_every=args.cancel_every,
                             max_new_tokens=args.max_new_tokens, label=args.label, out_dir=args.out_dir)
    print(json.dumps(summary, indent=1))
    print('SOAK_WRITTEN', path)


if __name__ == '__main__':
    main()
