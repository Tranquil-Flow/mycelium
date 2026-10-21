# SPDX-License-Identifier: AGPL-3.0-only
"""The browser stream owns the upstream request until terminal publication."""
import asyncio
import json

import pytest

from mycelium_ui_gateway import GatewayConfig, create_product_gateway_application
from ui_gateway_support import (
    FakeUpstream, asgi_request, default_observatory, default_request,
    json_body, session_headers, submission,
)


@pytest.mark.parametrize("ending", ["disconnect", "terminal", "explicit_cancel", "before_frame", "task_cancel", "failed_cancel"])
def test_stream_lifecycle_cancels_abandoned_work_once(ending):
    async def scenario():
        cancels = []
        stream_open = asyncio.Event()
        release = asyncio.Event()
        first_frame = asyncio.Event()
        inbound = asyncio.Queue()
        async def upstream(scope, receive, send):
            if scope["path"] == "/v1/inference/request-1/events":
                event = {"protocol": "mycelium.request_event.v1", "request_id": "request-1",
                         "publisher_generation": 1, "sequence": 0, "type": "accepted"}
                await send({"type": "http.response.start", "status": 200,
                            "headers": [(b"content-type", b"text/event-stream")]})
                frame = b"id: 1:0\nevent: accepted\ndata: " + json_body(event) + b"\n\n"
                stream_open.set()
                if ending != "before_frame":
                    await send({"type": "http.response.body", "body": frame, "more_body": True})
                await release.wait()
                if ending == "terminal":
                    event.update(sequence=1, type="completed")
                    frame = b"id: 1:1\nevent: completed\ndata: " + json_body(event) + b"\n\n"
                    await send({"type": "http.response.body", "body": frame, "more_body": False})
                else:
                    await receive()  # upstream sees stream disconnect
                return
            if scope["path"] == "/v1/inference/request-1" and scope["method"] == "DELETE":
                cancels.append(scope)
                if ending == "failed_cancel":
                    raise RuntimeError("cancel failed")
            body = bytearray()
            while True:
                message = await receive()
                body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            status, headers, chunks = await default_request(scope, bytes(body))
            await send({"type": "http.response.start", "status": status, "headers": headers})
            for index, chunk in enumerate(chunks):
                await send({"type": "http.response.body", "body": chunk,
                            "more_body": index + 1 < len(chunks)})

        from ui_gateway_support import FakeCoordinator
        app = create_product_gateway_application(config=GatewayConfig(),
            observatory_app=FakeUpstream(default_observatory), request_gateway_app=upstream,
            swarm_coordinator=FakeCoordinator(), request_gateway_bearer_token="private-request-bearer")
        boot = await asgi_request(app, "/api/v1/bootstrap")
        submit = await asgi_request(app, "/api/v1/inference", method="POST",
            body=json_body(submission()), headers=session_headers(boot, csrf=True)
            + [(b"content-type", b"application/json")])
        assert submit.status == 202
        sent = []
        async def receive():
            return await inbound.get()
        async def send(message):
            sent.append(message)
            if b"event: accepted" in message.get("body", b""):
                first_frame.set()
        scope = {"type": "http", "http_version": "1.1", "method": "GET", "scheme": "http",
                 "path": submit.json()["event_path"], "query_string": b"",
                 "headers": [(b"host", b"127.0.0.1:8080")] + session_headers(boot),
                 "client": ("127.0.0.1", 49152), "server": ("127.0.0.1", 8080)}
        task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(stream_open.wait() if ending == "before_frame" else first_frame.wait(), 2)
            if ending == "explicit_cancel":
                response = await asgi_request(app, submit.json()["cancel_path"], method="DELETE",
                                              headers=session_headers(boot, csrf=True))
                assert response.status == 202
            if ending == "task_cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                release.set()
            elif ending == "terminal":
                release.set()
                await asyncio.wait_for(task, 2)
                await inbound.put({"type": "http.disconnect"})
            else:
                await inbound.put({"type": "http.disconnect"})
                release.set()
                await asyncio.wait_for(task, 2)
            assert len(cancels) == (0 if ending == "terminal" else 1)
            if cancels:
                assert any(name == b"x-mycelium-session" and value == b"session-owner-token-0000000000000001"
                           for name, value in cancels[0]["headers"])
        finally:
            release.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())
