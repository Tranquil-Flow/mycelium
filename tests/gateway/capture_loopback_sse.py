# SPDX-License-Identifier: AGPL-3.0-only
"""Loopback end-to-end capture for the M1 gateway/control runtime.

Runs the real composition on loopback (no launchd, no DNS/tunnel, no model),
prints the raw SSE bytes as they arrive off the socket, then drives a dead route
and prints the named cold-route error.  Output is the acceptance transcript.
"""

from __future__ import annotations

import http.client
import json
import socket
import sys
import threading
import time

from mycelium_demo.gateway_control_runtime import (
    EchoStubBackend,
    GatewayControlRuntime,
    load_stub_qualification,
)
from mycelium_request_gateway.contracts import InferenceSubmission, qualification_binding

PUBLIC_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 14351
CONTROL_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 14352
STATE_ROOT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/m1-gateway-capture"

hold_release = threading.Event()
backend = EchoStubBackend(hold_after_tokens=1, hold_release=hold_release)
runtime = GatewayControlRuntime(
    public_origin_port=PUBLIC_PORT,
    gateway_upstream_port=0,
    control_origin_port=CONTROL_PORT,
    public_host="mycelium.now",
    state_root=STATE_ROOT,
    backend=backend,
    request_bearer_token="capture-diagnostic-token",
).start()
print("== status ==")
print(json.dumps(runtime.status(), sort_keys=True, indent=2))
print("== listening sockets ==")
for label, port in (
    ("public-origin", runtime.public_origin_port),
    ("gateway-upstream", runtime.gateway_server.port),
    ("control-origin", runtime.control_origin.port),
):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(2.0)
        print(f"{label} 127.0.0.1:{port} connect_ex={probe.connect_ex(('127.0.0.1', port))}")

body = json.dumps(
    InferenceSubmission(
        prompt="alpha beta gamma",
        max_new_tokens=3,
        qualification=qualification_binding(load_stub_qualification()),
    ).to_dict(),
    sort_keys=True,
).encode("utf-8")

connection = http.client.HTTPConnection("127.0.0.1", PUBLIC_PORT, timeout=10)
connection.request(
    "POST",
    "/api/v1/inference",
    body=body,
    headers={"Host": "mycelium.now", "Content-Type": "application/json"},
)
response = connection.getresponse()
accepted = json.loads(response.read().decode("utf-8"))
connection.close()
print("== POST /api/v1/inference through the public origin ==")
print(f"HTTP {response.status}")
print(json.dumps(accepted, sort_keys=True))

stream = socket.create_connection(("127.0.0.1", PUBLIC_PORT), timeout=15)
stream.settimeout(15)
stream.sendall(
    (
        f"GET /api/v1/inference/{accepted['request_id']}/events HTTP/1.1\r\n"
        "Host: mycelium.now\r\n"
        "Accept: text/event-stream\r\n"
        f"X-Mycelium-Session: {accepted['session_token']}\r\n"
        "\r\n"
    ).encode("ascii")
)
buffer = b""
while b"\r\n\r\n" not in buffer:
    buffer += stream.recv(4096)
head, _, buffer = buffer.partition(b"\r\n\r\n")
print("== raw SSE response headers (bytes on the wire) ==")
print(repr(head.decode("latin-1")))

started = time.monotonic()
print("== raw SSE blocks as they arrive (backend is held after 1 token) ==")
while True:
    index = buffer.find(b"\n\n")
    if index == -1:
        try:
            chunk = stream.recv(4096)
        except (socket.timeout, TimeoutError):
            print("!! stream idle timeout")
            break
        if not chunk:
            print("-- stream closed by the origin")
            break
        buffer += chunk
        continue
    block, buffer = buffer[:index], buffer[index + 2 :]
    if block.strip():
        print(f"[+{time.monotonic() - started:6.2f}s] {block.decode('utf-8')!r}")
        if b"event: token" in block and not hold_release.is_set():
            print(
                f"    -> token arrived while the backend is still held "
                f"(backend.emitted_tokens={backend.emitted_tokens}, "
                f"release set={hold_release.is_set()})"
            )
            hold_release.set()
        if b"event: completed" in block:
            break
stream.close()

print("== dead route: kill the serving route, submit again ==")
runtime.kill_current_route()
started = time.monotonic()
connection = http.client.HTTPConnection("127.0.0.1", PUBLIC_PORT, timeout=10)
connection.request(
    "POST",
    "/api/v1/inference",
    body=body,
    headers={"Host": "mycelium.now", "Content-Type": "application/json"},
)
response = connection.getresponse()
payload = json.loads(response.read().decode("utf-8"))
connection.close()
print(f"HTTP {response.status} after {time.monotonic() - started:.3f}s")
print(json.dumps(payload, sort_keys=True))
print("cold_route_restart projection:", json.dumps(runtime.status()["cold_route_restart"], sort_keys=True))

print("== successor request on the replacement route ==")
connection = http.client.HTTPConnection("127.0.0.1", PUBLIC_PORT, timeout=10)
connection.request(
    "POST",
    "/api/v1/inference",
    body=body,
    headers={"Host": "mycelium.now", "Content-Type": "application/json"},
)
response = connection.getresponse()
successor = json.loads(response.read().decode("utf-8"))
connection.close()
print(f"HTTP {response.status} request_id={successor.get('request_id')}")

runtime.stop()
print("== runtime stopped without touching launchd, DNS, the tunnel or 127.0.0.1:4351 ==")
