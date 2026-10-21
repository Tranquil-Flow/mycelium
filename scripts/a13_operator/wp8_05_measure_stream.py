# SPDX-License-Identifier: AGPL-3.0-only
"""wp8-05/criterion 6: measure TTFT and TPOT client-side, from real SSE arrival times.

Why a new driver instead of the existing stream step: `round14_stream.py` is one of the five PINNED
operator files, so it cannot be extended, and the round's events carry NO timestamps --
`{protocol, publisher_generation, request_id, sequence, type, text, token_index}` and nothing else.
The only truthful way to time a stream is from the receiving end, which is what this does.

Why the existing runs cannot answer criterion 6: the round's request is
`prompt='The capital of Germany is', max_new_tokens=8`, and the model emits EOS after one token
("Berlin"). With a single token event there is no inter-token interval at all, so TPOT is undefined
and TTFT degenerates into the whole request. A claim sheet needs a prompt that actually generates
tokens, which is why this driver takes them as parameters.

Timing definitions used, stated so the published numbers mean something specific:
  ttft_s  = first token arrival - request accept      (time to first token)
  tpot_s  = (last token arrival - first token arrival) / (tokens - 1)   (per-token, after the first)
Both need tokens >= 2; a single-token stream reports tpot_s as None rather than 0.0, because 0.0 would
read as an impossibly fast measurement.
"""
import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


def summarize(samples):
    """TTFT/TPOT from (event, receive_time) pairs. Pure, so the arithmetic is testable."""
    accepted = next((t for e, t in samples if e.get('type') == 'accepted'), None)
    tokens = [(e, t) for e, t in samples if e.get('type') == 'token']
    terminal = next((e for e, _t in samples if e.get('type') in ('completed', 'cancelled', 'failed')),
                    None)
    if accepted is None or not tokens:
        return {'ok': False, 'error': 'stream_incomplete',
                'events': len(samples), 'tokens': len(tokens)}
    first_t, last_t = tokens[0][1], tokens[-1][1]
    result = {
        'ok': True,
        'tokens': len(tokens),
        'ttft_s': round(first_t - accepted, 6),
        'total_s': round(last_t - accepted, 6),
        'terminal': (terminal or {}).get('type'),
        # WHICH terminal, not just whether there was one: a `failed` terminal that carries
        # code=request_cleanup_unproven is the route saying it could not prove owner-scoped cleanup,
        # and criterion 5 must count that as a failure rather than a settled request.
        'terminal_code': (terminal or {}).get('code'),
        'has_terminal': terminal is not None,
        # ok=True means "the stream produced tokens and reached a terminal"; succeeded means the
        # terminal was the SUCCESSFUL one. The two differ, and criterion 5 needs the second.
        'succeeded': (terminal or {}).get('type') == 'completed',
        'text': ''.join(e.get('text') or '' for e, _t in tokens),
    }
    result['tpot_s'] = (round((last_t - first_t) / (len(tokens) - 1), 6)
                        if len(tokens) >= 2 else None)
    return result


def stream(base, prompt, max_new_tokens, *, timeout=600, clock=time.monotonic, cancel_after_s=None,
           cancel_mode='disconnect'):
    """Submit one request and time every event as it ARRIVES. Returns (samples, summary).

    cancel_mode:
      'disconnect' -- close the stream early. The product treats a requestor going away as its own
                      cancel trigger (route.py:2520, the inline `infer_cancel_wait`), so this
                      exercises that real path. This client records only that it ASKED.
      'endpoint'   -- call the product's explicit cancel (`cancel_path` from the submit response,
                      DELETE /v1/inference/{request_id}) AND KEEP READING for the product's own
                      terminal. Criterion 5 asks for >=10 CONFIRMED cancellations, and only the
                      product's `cancelled` terminal can confirm one: a cancel that silently does
                      nothing is exactly what the soak exists to catch, and a client that assumed
                      its own request succeeded would hide it.
    """
    if cancel_mode not in ('disconnect', 'endpoint'):
        # Fail closed on a mistyped mode rather than silently measuring the other one.
        raise ValueError('cancel_mode_unknown:%s' % cancel_mode)
    # A COOKIE JAR IS REQUIRED, and 76r1 is the evidence: with plain urlopen the session cookie from
    # /api/v1/bootstrap is dropped, so every later call came back `HTTP Error 401: Unauthorized` -- all
    # three attempts. The pinned stream step (round14_stream.py:23) builds its opener with
    # HTTPCookieProcessor for exactly this reason, and round76r1-measurements recorded the 401s.
    origin = base.rstrip('/')
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPCookieProcessor(jar))

    def _json(path, body=None, extra=None):
        request = urllib.request.Request(
            origin + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Origin': origin, 'Content-Type': 'application/json',
                     'Accept': 'application/json', **(extra or {})})
        with opener.open(request, timeout=timeout) as response:
            return json.load(response)

    session = _json('/api/v1/bootstrap')['session']
    csrf = {session['csrf_header']: session['csrf_token']}

    def _delete(path, extra=None):
        """The product's cancel verb. Returns (status, body|None); an error status is a fact, not a
        crash -- the caller reports it rather than dying inside a measurement."""
        request = urllib.request.Request(
            origin + path, method='DELETE',
            headers={'Origin': origin, 'Accept': 'application/json', **(extra or {})})
        try:
            with opener.open(request, timeout=timeout) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as exc:
            return exc.code, None
    qualification = _json('/api/v1/qualification/current')
    # USE THE PRODUCT'S OWN CONTRACT. 77r1 sent a hand-rolled dict and the gateway answered 400: the
    # contract carries `protocol` (and validates it), and the pinned step builds the same object --
    # `InferenceSubmission(prompt, max_new_tokens, QualificationBinding.from_dict(binding)).to_dict()`.
    # Hand-rolling a payload that a validator defines is how a measurement harness stops measuring.
    from mycelium_request_gateway.contracts import InferenceSubmission, QualificationBinding
    submission = InferenceSubmission(
        prompt=prompt, max_new_tokens=int(max_new_tokens),
        qualification=QualificationBinding.from_dict(qualification['binding'])).to_dict()
    headers = {'Origin': origin, 'Accept': 'text/event-stream', **csrf}
    accepted = _json('/api/v1/inference', submission, extra=csrf)
    samples = []
    cancel_requested = False
    cancel_status = None
    event_request = urllib.request.Request(base.rstrip('/') + accepted['event_path'], headers=headers)
    with opener.open(event_request, timeout=timeout) as response:
        _loop_start = clock()
        for raw in response:
            if not raw.startswith(b'data: '):
                continue
            arrived = clock()
            event = json.loads(raw[6:])
            samples.append((event, arrived))
            if (cancel_after_s is not None and not cancel_requested
                    and (arrived - _loop_start) >= cancel_after_s):
                cancel_requested = True
                if cancel_mode == 'endpoint':
                    # ASK THE PRODUCT, THEN KEEP READING. Its own terminal is the only confirmation:
                    # a cancellation that silently does nothing is exactly what criterion 5's soak
                    # exists to catch, and a client that assumed its own request succeeded would hide
                    # it. If no terminal ever arrives, the record carries no terminal and the soak
                    # counts the request as a failure rather than a confirmation.
                    cancel_status, _body = _delete(accepted['cancel_path'], extra=csrf)
                    continue
                # 'disconnect': closing here IS the product's own cancel trigger. route.py:2520
                # refers to the INLINE infer_cancel_wait -- the owner cancels the inference when the
                # requestor goes away -- so this exercises that real path. The client records only
                # that it ASKED; confirmation is the product's own evidence.
                break
            if event.get('type') in ('completed', 'cancelled', 'failed'):
                break
    summary = summarize(samples)
    if cancel_after_s is not None:
        summary['cancelled_requested'] = bool(cancel_requested)
        summary['cancel_mode'] = cancel_mode
        summary['cancel_status'] = cancel_status
    return samples, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description='time TTFT/TPOT client-side (criterion 6)')
    parser.add_argument('--base', required=True, help='the product API origin')
    parser.add_argument('--prompt', default='Describe the city of Berlin in three sentences.')
    parser.add_argument('--max-new-tokens', type=int, default=64)
    parser.add_argument('--out', required=True, help='where to write the measurement JSON')
    args = parser.parse_args(argv)
    samples, summary = stream(args.base, args.prompt, args.max_new_tokens)
    record = {'protocol': 'mycelium.a13.stream_timing.v1',
              'prompt': args.prompt, 'max_new_tokens': args.max_new_tokens,
              'summary': summary,
              'event_arrival_offsets_s': [round(t - samples[0][1], 6) for _e, t in samples]}
    Path(args.out).write_text(json.dumps(record, indent=1, sort_keys=True))
    print(json.dumps(record['summary']))
    return 0 if summary.get('ok') else 1


if __name__ == '__main__':
    sys.exit(main())
