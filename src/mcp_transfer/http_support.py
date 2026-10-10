from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

import httpx
from fastapi import HTTPException
from starlette.responses import Response, StreamingResponse

from .policy import filter_result

REQUEST_HEADERS = {
    "accept",
    "content-type",
    "mcp-protocol-version",
    "mcp-session-id",
    "last-event-id",
    "mcp-method",
    "mcp-name",
}
RESPONSE_HEADERS = {
    "content-type",
    "mcp-session-id",
    "mcp-protocol-version",
    "retry-after",
    "www-authenticate",
}


async def relay_response(
    upstream: httpx.Response,
    cleanup: Callable,
    alive: Callable,
    message: dict | None = None,
    policy: dict | None = None,
):
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in RESPONSE_HEADERS}
    headers["X-Accel-Buffering"] = "no"
    if "text/event-stream" not in upstream.headers.get("content-type", ""):
        try:
            data = bytearray()
            async for part in upstream.aiter_bytes():
                data.extend(part)
                if len(data) > 8 * 1024 * 1024:
                    raise HTTPException(502, "Upstream response exceeds limit")
            if message and policy and data and "application/json" in upstream.headers.get("content-type", ""):
                parsed = json.loads(data)
                if isinstance(parsed, dict):
                    data = json.dumps(filter_result(message, parsed, policy)).encode()
            return Response(bytes(data), status_code=upstream.status_code, headers=headers)
        finally:
            await upstream.aclose()
            await cleanup()

    async def stream():
        iterator = upstream.aiter_lines().__aiter__()
        pending = None
        event_lines = []
        event_size = 0
        last_check = 0.0
        try:
            while True:
                if time.monotonic() - last_check >= 2:
                    if not await alive():
                        return
                    last_check = time.monotonic()
                if pending is None:
                    pending = asyncio.create_task(anext(iterator))
                done, _ = await asyncio.wait({pending}, timeout=2)
                if not done:
                    yield b": keepalive\n\n"
                    continue
                try:
                    line = pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    pending = None
                event_size += len(line.encode())
                if event_size > 8 * 1024 * 1024:
                    return
                if line:
                    event_lines.append(line)
                    continue
                if message and policy:
                    payload = "\n".join(x[5:].lstrip(" ") for x in event_lines if x.startswith("data:"))
                    if payload:
                        try:
                            parsed = json.loads(payload)
                            if isinstance(parsed, dict):
                                parsed = filter_result(message, parsed, policy)
                                event_lines = [x for x in event_lines if not x.startswith("data:")] + [
                                    "data: " + json.dumps(parsed)
                                ]
                        except ValueError:
                            pass
                yield ("\n".join(event_lines) + "\n\n").encode()
                event_lines = []
                event_size = 0
        finally:
            if pending:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
            await upstream.aclose()
            await cleanup()

    return StreamingResponse(stream(), status_code=upstream.status_code, headers=headers)
