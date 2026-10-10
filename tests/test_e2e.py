"""Real TLS + frps/frpc + SDK + subprocess integration (opt-in network test)."""

import asyncio
import os
import socket
import ssl
import sys
from contextlib import asynccontextmanager

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from mcp_transfer.agent import Agent, HostAdapter, create_agent_app
from mcp_transfer.cli import initialize_cloud
from mcp_transfer.cloud import create_cloud, create_frp_plugin
from mcp_transfer.config import Settings
from mcp_transfer.frp import FrpProcess, download_frp, write_frps


def available_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@asynccontextmanager
async def serve(application, port, **kwargs):
    server = uvicorn.Server(
        uvicorn.Config(
            application, host="127.0.0.1", port=port, log_level="error", access_log=False, **kwargs
        )
    )
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        if task.done():
            await task
        await asyncio.sleep(0.025)
    else:
        raise RuntimeError("Server did not start")
    try:
        yield server
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), 8)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.skipif(
    os.environ.get("MCPT_E2E") != "1",
    reason="Set MCPT_E2E=1 to download pinned frp and run real TLS integration",
)
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_real_frp_stdio_roundtrip_and_revocation(tmp_path, monkeypatch, mode):
    cloud_port, adapter_port, client_port = available_port(), available_port(), available_port()
    cloud_settings = Settings(tmp_path / "cloud")
    cloud_settings.data["frp_port"] = available_port()
    cloud_settings.data["plugin_port"] = available_port()
    cloud_settings.save()
    # Go uses the macOS system trust store instead of SSL_CERT_FILE. Keep the
    # test-only cloud origin on loopback HTTP there; Linux CI exercises HTTPS.
    use_https = sys.platform.startswith("linux")
    public = f"{'https' if use_https else 'http'}://127.0.0.1:{cloud_port}"
    initialize_cloud(cloud_settings, public, password="integration-password-123", development=not use_https)
    cloud = create_cloud(cloud_settings)
    host_settings = Settings(tmp_path / "host")
    host_settings.data.update(
        adapter_port=adapter_port, enterprise_ca=str(cloud_settings.home / "pki/ca.crt")
    )
    host_settings.save()
    host = Agent(host_settings, "host")
    client_settings = Settings(tmp_path / "client")
    client_settings.data.update(ui_port=client_port, enterprise_ca=str(cloud_settings.home / "pki/ca.crt"))
    client_settings.save()
    agent = Agent(client_settings, "client")
    await download_frp(cloud_settings.home, "frps")
    await download_frp(host_settings.home, "frpc")
    monkeypatch.setenv("SSL_CERT_FILE", str(cloud_settings.home / "pki/ca.crt"))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    ca = ssl.create_default_context(cafile=str(cloud_settings.home / "pki/ca.crt"))
    frps = FrpProcess(cloud_settings, "frps")
    try:
        async with (
            serve(
                cloud,
                cloud_port,
                **(
                    {
                        "ssl_certfile": str(cloud_settings.home / "pki/frps.crt"),
                        "ssl_keyfile": str(cloud_settings.home / "pki/frps.key"),
                    }
                    if use_https
                    else {}
                ),
            ),
            serve(create_frp_plugin(cloud_settings), cloud_settings.data["plugin_port"]),
        ):
            async with httpx.AsyncClient(base_url=public, verify=ca, trust_env=False) as admin:
                login = await admin.post(
                    "/api/auth/login", json={"username": "admin", "password": "integration-password-123"}
                )
                assert login.status_code == 200, login.text
                admin.headers["x-csrf-token"] = login.json()["csrf"]
                code = (await admin.post("/api/enrollment-codes", json={"role": "host"})).json()["code"]
                await host.enroll(public, code, "Integration host", development=not use_https)
                user_response = await admin.post(
                    "/api/users", json={"username": "consumer", "password": "consumer-password-123"}
                )
                owner = user_response.json()["id"]
                code = (
                    await admin.post("/api/enrollment-codes", json={"role": "client", "owner": owner})
                ).json()["code"]
                await agent.enroll(public, code, "Integration client", development=not use_https)
                host.vault.update(
                    services={
                        "srv_demo": {
                            "name": "Demo",
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": ["-m", "mcp_transfer", "demo-server"],
                            "enabled": True,
                        }
                    }
                )
                write_frps(cloud_settings)
                await frps.start()
                async with serve(
                    HostAdapter(host),
                    adapter_port,
                    lifespan="off",
                    ssl_certfile=str(host_settings.home / "device.crt"),
                    ssl_keyfile=str(host_settings.home / "device.key"),
                    ssl_ca_certs=str(host_settings.home / "ca.crt"),
                    ssl_cert_reqs=ssl.CERT_REQUIRED,
                ):
                    await host.tick()
                    grant = await admin.post(
                        "/api/grants",
                        json={"user_id": owner, "server_id": "srv_demo", "policy": {"tools": ["add"]}},
                    )
                    assert grant.status_code == 200, grant.text
                    await agent.tick()
                    agent.subscribe(["srv_demo"])
                    for _ in range(80):
                        try:
                            reader, writer = await asyncio.open_connection(
                                "127.0.0.1", host_settings.data["remote_port"]
                            )
                            writer.close()
                            await writer.wait_closed()
                            break
                        except OSError:
                            await asyncio.sleep(0.25)
                    else:
                        raise AssertionError((host_settings.home / "frpc.log").read_text())
                    async with serve(create_agent_app(agent, background=False), client_port):
                        headers = {
                            "Authorization": "Bearer " + agent.vault.read()["mappings"]["srv_demo"]["token"],
                            "X-MCPT-Connection": "integration",
                        }
                        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=30) as http:
                            async with Client(
                                streamable_http_client(
                                    f"http://127.0.0.1:{client_port}/mcp/srv_demo", http_client=http
                                ),
                                cache=None,
                                mode=mode,
                            ) as mcp:
                                listed = await mcp.list_tools()
                                assert [tool.name for tool in listed.tools] == ["add"]
                                result = await mcp.call_tool("add", {"a": 20, "b": 22})
                                assert "42" in result.model_dump_json()
                                assert (
                                    await admin.post(
                                        f"/api/devices/{client_settings.data['device_id']}/revoke"
                                    )
                                ).status_code == 200
                                with pytest.raises((MCPError, httpx2.HTTPError)):
                                    await mcp.call_tool("add", {"a": 1, "b": 2})
    finally:
        await host.close()
        await agent.close()
        await frps.stop()
