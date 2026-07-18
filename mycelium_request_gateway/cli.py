"""CLI for authenticated prompt submission and production token streaming."""
from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence, TextIO

from .client import GatewayClient, GatewayClientError, HTTPGatewayClient
from .contracts import InferenceSubmission


def stream_prompt(
    client: GatewayClient,
    *,
    prompt: str,
    max_new_tokens: int,
    output: TextIO,
    max_reconnects: int = 3,
) -> int:
    """Submit and render token events from the production gateway session API."""

    if not isinstance(max_reconnects, int) or isinstance(max_reconnects, bool) or max_reconnects < 0:
        raise ValueError("invalid_max_reconnects")
    binding = client.current_qualification()
    submission = InferenceSubmission(
        prompt=prompt,
        max_new_tokens=max_new_tokens,
        qualification=binding,
    )
    request_id = client.submit(submission)
    last_event_id: int | None = None
    reconnects = 0
    retryable = {"stream_disconnected", "stream_transport_error", "stream_unavailable"}
    while True:
        try:
            saw_terminal = False
            for event in client.events(request_id, last_event_id=last_event_id):
                expected_sequence = 0 if last_event_id is None else last_event_id + 1
                if event.request_id != request_id or event.sequence != expected_sequence:
                    raise GatewayClientError("stream_sequence_violation")
                if event.kind == "token":
                    if event.text is None:
                        raise GatewayClientError("invalid_event_stream")
                    output.write(event.text)
                    output.flush()
                last_event_id = event.sequence
                if event.kind == "completed":
                    return 0
                if event.kind == "cancelled":
                    return 2
                if event.kind == "failed":
                    raise GatewayClientError(event.code or "request_failed")
                saw_terminal = saw_terminal or event.terminal
            if not saw_terminal:
                raise GatewayClientError("stream_disconnected")
        except GatewayClientError as exc:
            if exc.code not in retryable or reconnects >= max_reconnects:
                raise
            reconnects += 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mycelium-request")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument(
        "--token-env",
        default="MYCELIUM_REQUEST_GATEWAY_TOKEN",
        help="environment variable containing bearer credential",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    token = os.environ.get(args.token_env)
    if not token:
        print("missing request-gateway credential", file=sys.stderr)
        return 2
    prompt = sys.stdin.read()
    if not prompt:
        print("empty prompt", file=sys.stderr)
        return 2
    try:
        client = HTTPGatewayClient(
            base_url=args.base_url,
            bearer_token=token,
        )
        return stream_prompt(
            client,
            prompt=prompt,
            max_new_tokens=args.max_new_tokens,
            output=sys.stdout,
        )
    except (GatewayClientError, ValueError) as exc:
        code = exc.code if isinstance(exc, GatewayClientError) else str(exc)
        print(f"request failed: {code}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
