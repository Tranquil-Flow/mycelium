# Use your private swarm through the OpenAI API

The Mycelium app on the owner's Mac can answer the OpenAI chat API for your swarm. Programs that already speak that API (the Python `openai` package, `curl`, editors and chat front ends that let you set a base URL) can then talk to the model your Macs serve. This is the macOS preview and, like the rest of the app, it is **not yet proven on two Macs**; the behaviour below is checked by automated tests against stand-ins for the swarm route (see Sources).

It is **your** swarm, not a hosted service. Only keys you create in the app work, every request runs on your joined Macs, and nothing is sent to `mycelium.now`.

## What you get

| | |
|---|---|
| `POST /v1/chat/completions` | Chat completion, streaming (`"stream": true`) and not. |
| `GET /v1/models` (and `/v1/models/{id}`) | Lists exactly one model: the one this swarm run is pinned to. |
| Authentication | `Authorization: Bearer <key>`, with keys you issue in the app. |
| Address | `http://127.0.0.1:8793/v1` by default, on this Mac only. |

How it behaves, so nothing surprises you:

- **One request at a time.** The swarm route serves one request for the whole swarm. A second request gets HTTP 429 `swarm_busy` with a `Retry-After`; retry it. The shared web chat page uses the same route.
- **Greedy decoding.** The route does not sample, so `temperature` and `top_p` are accepted only as `0` and `1` (the values that mean the same thing). Anything else is a clear HTTP 400, not a silent no-op.
- **One user turn.** The model's own chat template is applied to a single user turn. A `messages` list with several turns is sent as a plain transcript (`User: ...` / `Assistant: ...`) inside that turn, with any system messages in front. A single user message is sent as it is.
- **Token counts.** `usage.completion_tokens` is the number of tokens the swarm produced. `usage.prompt_tokens` is an estimate (about one token per four bytes), because the route does not report it.

## Turn it on

1. Open the Mycelium app on the owner Mac and start your swarm run as usual. The API serves only while the run is serving.
2. In the card **Use this private swarm from your own tools**, tick **Serve the API on this Mac (127.0.0.1 only)** and press **Save settings**. The card shows the address and the model id.
3. Under **API keys**, enter a label, optionally a quota, and press **Create key**. The key (it starts with `myc-sk-`) is shown **once**. Copy it then. Mycelium keeps only a hash of it in an owner-only file in the app's state folder (`openai-api/keys.json`, mode 0600) and cannot show it again. If you lose it, revoke it and make another.

The API is off until you turn it on, and it listens only on `127.0.0.1` on its own port (not the app's window port). Programs on other computers cannot reach it.

### Quotas per key

Each key has a label and an optional quota per window (a fixed window, 1 minute to 30 days, default 1 hour, starting at the key's first use in that window):

- **requests per window**, and/or
- **generated tokens per window.** A request reserves its `max_tokens` when it starts and is charged the tokens actually produced when it ends. A request whose `max_tokens` could never fit the quota is a 400 (`max_tokens_exceeds_quota`).

Over quota is HTTP 429 `rate_limit_exceeded` with `Retry-After` in seconds. A request that fails before the swarm produced anything does not count. **Revoke** a key in the app and it stops working at once.

## curl

```sh
export MYCELIUM_KEY='myc-sk-...'          # the key you copied
BASE=http://127.0.0.1:8793/v1

# the model this swarm serves
curl -s $BASE/models -H "Authorization: Bearer $MYCELIUM_KEY"

# one answer
curl -s $BASE/chat/completions \
  -H "Authorization: Bearer $MYCELIUM_KEY" -H 'Content-Type: application/json' \
  -d '{"model": "Qwen/Qwen3-0.6B", "max_tokens": 64,
       "messages": [{"role": "user", "content": "What is the capital of Germany?"}]}'

# streamed (server-sent events, ends with data: [DONE])
curl -sN $BASE/chat/completions \
  -H "Authorization: Bearer $MYCELIUM_KEY" -H 'Content-Type: application/json' \
  -d '{"model": "Qwen/Qwen3-0.6B", "stream": true, "max_tokens": 64,
       "messages": [{"role": "user", "content": "Count to five."}]}'
```

Use the model id that `GET /v1/models` prints (the one above is only an example). A different `model` is a 404 `model_not_found`. `max_tokens` (or `max_completion_tokens`) defaults to 256 and may be 1 to 4096.

## Python (`openai` package)

```python
from openai import OpenAI

client = OpenAI(base_url="http://127.0.0.1:8793/v1", api_key="myc-sk-...")

model = client.models.list().data[0].id

reply = client.chat.completions.create(
    model=model,
    max_tokens=64,
    messages=[{"role": "user", "content": "What is the capital of Germany?"}],
)
print(reply.choices[0].message.content)

for chunk in client.chat.completions.create(
        model=model, stream=True,
        messages=[{"role": "user", "content": "Count to five."}]):
    if chunk.choices and chunk.choices[0].delta.content:
        print(chunk.choices[0].delta.content, end="", flush=True)
```

Do not pass `temperature` or `top_p` other than 0 and 1, `n`, `stop`, `seed`, `tools`, `response_format` and similar; the client raises `BadRequestError` with `code == "unsupported_parameter"` and the name of the parameter in `param`. Front ends that always send `temperature=0.7` need that setting turned off or set to 0.

## What is accepted

| Field | Accepted |
|---|---|
| `model`, `messages` (roles `system`, `user`, `assistant`, `developer` as system; string content or text parts; the last message from the user) | yes |
| `max_tokens`, `max_completion_tokens`, `stream`, `stream_options.include_usage`, `user` | yes |
| `temperature` = 0, `top_p` = 1, `n` = 1, `presence_penalty` / `frequency_penalty` = 0 | yes (same as the route's behaviour) |
| other values of those, `stop`, `seed`, `logprobs`, `tools`, `tool_choice`, `functions`, `logit_bias`, images/audio, JSON mode | no: 400 `unsupported_parameter` |
| any field not in the OpenAI chat API | no: 400 `unknown_parameter` |

## Errors

Every error body is `{"error": {"message", "type", "param", "code"}}`. Gateway and router codes are mapped; the bounded upstream code is kept in `message` so you can quote it.

| HTTP | `type` | `code` | Meaning |
|---|---|---|---|
| 400 | `invalid_request_error` | `invalid_json`, `invalid_value`, `invalid_type`, `unsupported_parameter`, `unknown_parameter`, `context_length_exceeded`, `max_tokens_exceeds_quota`, `invalid_request` | Fix the request. `param` names the field. |
| 401 | `authentication_error` | `missing_api_key`, `invalid_api_key` | No key, a wrong key, or a revoked key. |
| 404 | `not_found_error` | `model_not_found`, `not_found` | Wrong model id or path. |
| 408 | `invalid_request_error` | `slow_consumer` | The stream was closed because the client read too slowly. |
| 413 | `invalid_request_error` | `request_too_large` | Body over 256 KiB. |
| 429 | `rate_limit_error` | `rate_limit_exceeded` | The key's quota. `Retry-After` says when. |
| 429 | `rate_limit_error` | `swarm_busy` | The swarm is serving another request. Retry. |
| 502 | `server_error` | `route_failed` | The route failed (backend, router or transport). |
| 503 | `server_error` | `swarm_not_ready` | The run is not serving yet (starting, rejoining, stopped). |
| 503 | `server_error` | `route_unavailable` | The route changed or dropped mid-request (stale qualification, path change, route dropped). Retry. |
| 503 | `server_error` | `request_cancelled` | The request was cancelled (for example the owner stopped the run). |
| 500 | `server_error` | `generation_failed` | Anything else; the message carries the bounded code. |

If a stream has already started when the route fails, the HTTP status is already 200, so the error arrives as a final `data: {"error": {...}}` event and there is no `[DONE]`. If the failure comes before the first token, you get a normal HTTP error instead. If the client disconnects mid-stream the app cancels the request on the swarm.

## Optional: reach it through the swarm's relay address

Off by default. In the same card, tick **Also answer on this swarm's own relay address** and the confirmation, then **Save settings**. The API is then also answered at `https://<your relay address>/v1`, using the relay your swarm already uses for join codes and web chat (a Cloudflare quick tunnel, or your own address). Keys, quotas and errors are the same.

Be clear about what this means:

- **The relay can read it.** The tunnel ends HTTPS at Cloudflare (or at the front you chose), so it can see your requests, the replies and your API key in transit. Use a key with a small quota for this path and revoke it when done.
- **Not the swarm directory.** This never goes through `mycelium.now`. The directory origin keeps answering 404 for `/v1/*`, and while the setting is off the relay answers 404 as well.
- **Streams arrive whole.** The relay hands back one buffered response, so with `"stream": true` the SSE text arrives only when the answer is finished, not token by token. Use the local address for live streaming.
- Anyone holding a key can use the swarm's Macs. The relay address changes when a temporary tunnel restarts.

Turn the setting off and `/v1/*` on the relay returns 404 again at once.

## Limits and what is not proven

- Tested with automated tests against stand-ins for the swarm route, including the real `openai` Python client. **Not yet run on two Macs.**
- No per-IP limits; the only limits are per key. The local port accepts only `Host: 127.0.0.1:<port>` or `localhost:<port>` and grants no cross-origin (CORS) access.
- Request bodies are limited to 256 KiB and the prompt to 131072 bytes.
- Chat content, keys and tokens are never written to logs.

## Sources

- `mycelium_a13_macos/openai_api.py` (endpoints, validation, error mapping, relay handler), `mycelium_a13_macos/openai_keys.py` (hashed keys, quotas)
- `mycelium_a13_macos/chat.py` (`RegistryClient`, the route's one-request-at-a-time rule), `mycelium_a13_macos/public_relay.py` (relay route)
- `mycelium_a13_macos/web/index.html`, `mycelium_a13_macos/web/openai.js` (the app card)
- `docs/request-streaming-session-lifecycle.md`, `docs/request-gateway-api-contract.md` (gateway failure codes, cancel on disconnect)
- `tests/a13_macos/test_openai_keys.py`, `tests/a13_macos/test_openai_api.py`, `tests/a13_macos/test_openai_api_wire.py`, `tests/demo/test_openai_api_surface_closed.py`
