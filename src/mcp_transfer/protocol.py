from __future__ import annotations

import asyncio
import contextlib
import time
from contextlib import asynccontextmanager

import httpx2
import mcp.types as types
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.shared.exceptions import MCPError
from pydantic import ConfigDict


class WireParams(types.RequestParams):
    model_config = ConfigDict(extra="allow")


class WireResult(types.Result):
    model_config = ConfigDict(extra="allow")


@asynccontextmanager
async def upstream_client(config: dict):
    kind = config["transport"]
    if kind == "stdio":
        target = StdioServerParameters(
            command=config["command"],
            args=config.get("args", []),
            env=config.get("env", {}),
            cwd=config.get("cwd") or None,
        )
        async with Client(target, cache=None, mode=config.get("mode", "auto")) as client:
            yield client
    else:
        async with httpx2.AsyncClient(
            headers=config.get("headers", {}),
            follow_redirects=False,
            trust_env=False,
            timeout=httpx2.Timeout(30, read=None),
        ) as http:
            if kind == "sse":
                # SDK validates the message endpoint remains on the configured origin.
                def factory(**kwargs):
                    return httpx2.AsyncClient(**kwargs, trust_env=False, follow_redirects=False)

                target = sse_client(
                    config["url"], headers=config.get("headers", {}), httpx_client_factory=factory
                )
            else:
                target = streamable_http_client(config["url"], http_client=http)
            async with Client(
                target, cache=None, mode="legacy" if kind == "sse" else config.get("mode", "auto")
            ) as client:
                yield client


def proxy_server(client: Client, name: str) -> Server:
    """Terminate transports with the official SDK; expose only bridged capabilities."""
    server = Server(name, version="0.1.0", instructions=client.instructions)
    capabilities = client.server_capabilities
    methods = {"ping"}
    if capabilities.tools:
        methods.update({"tools/list", "tools/call"})
    if capabilities.resources:
        methods.update({"resources/list", "resources/templates/list", "resources/read"})
    if capabilities.prompts:
        methods.update({"prompts/list", "prompts/get"})
    if capabilities.completions:
        methods.add("completion/complete")

    for method in methods:

        async def handler(ctx, params, method=method):
            async def progress(progress, total=None, message=None):
                if ctx.meta and ctx.meta.progress_token is not None:
                    await ctx.session.send_progress_notification(
                        ctx.meta.progress_token, progress, total, message
                    )

            result = await client.session.send_request(
                types.Request(method=method, params=params), WireResult, progress_callback=progress
            )
            dumped = result.model_dump(by_alias=True, exclude_none=True)
            # A transport bridge cannot silently perform client-side interactions.
            if dumped.get("resultType") == "input_required" or "inputRequests" in dumped:
                raise MCPError(
                    types.ErrorData(
                        code=-32601, message="Interactive client capabilities are disabled for this adapter"
                    )
                )
            return dumped

        server.add_request_handler(method, WireParams, handler)
    return server


class Actor:
    def __init__(self, config: dict, sid: str):
        self.config = config
        self.sid = sid
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.error = None
        self.app = None
        self.last_used = time.monotonic()
        self.active = 0
        self.task = asyncio.create_task(self.run())

    async def run(self):
        try:
            async with upstream_client(self.config) as client:
                server = proxy_server(client, self.config.get("name", self.sid))
                self.app = server.streamable_http_app(
                    streamable_http_path=f"/mcp/{self.sid}", max_sessions=8, session_idle_timeout=300
                )
                async with self.app.router.lifespan_context(self.app):
                    self.ready.set()
                    await self.stop.wait()
        except BaseException as error:
            self.error = error
            self.ready.set()

    async def get_app(self):
        await asyncio.wait_for(self.ready.wait(), timeout=30)
        if self.error:
            raise RuntimeError(
                "MCP adapter could not start; inspect host process and dependencies"
            ) from self.error
        self.last_used = time.monotonic()
        return self.app

    async def close(self):
        self.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
        except TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
