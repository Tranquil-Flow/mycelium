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
import json
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
        'text': ''.join(e.get('text') or '' for e, _t in tokens),
    }
    result['tpot_s'] = (round((last_t - first_t) / (len(tokens) - 1), 6)
                        if len(tokens) >= 2 else None)
    return result


def stream(base, prompt, max_new_tokens, *, timeout=600, clock=time.monotonic):
    """Submit one request and time every event as it ARRIVES. Returns (samples, summary)."""
    def _json(path, body=None):
        request = urllib.request.Request(
            base.rstrip('/') + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Origin': base.rstrip('/'), 'Content-Type': 'application/json',
                     'Accept': 'application/json'})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    session = _json('/api/v1/bootstrap')['session']
    qualification = _json('/api/v1/qualification/current')
    submission = {'prompt': prompt, 'max_new_tokens': int(max_new_tokens),
                  'qualification': qualification['binding']}
    headers = {'Origin': base.rstrip('/'), 'Accept': 'text/event-stream',
               session['csrf_header']: session['csrf_token']}
    accepted = _json('/api/v1/inference', submission)
    samples = []
    event_request = urllib.request.Request(base.rstrip('/') + accepted['event_path'], headers=headers)
    with urllib.request.urlopen(event_request, timeout=timeout) as response:
        for raw in response:
            if not raw.startswith(b'data: '):
                continue
            arrived = clock()
            event = json.loads(raw[6:])
            samples.append((event, arrived))
            if event.get('type') in ('completed', 'cancelled', 'failed'):
                break
    return samples, summarize(samples)


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
