"""Real TLS + frps/frpc + SDK + subprocess integration (opt-in network test)."""

import asyncio
import os
import socket
import ssl
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

from mcp_transfer.agent import Agent, HostAdapter, create_agent_app
from mcp_transfer.cli import initialize_cloud
from mcp_transfer.cloud import create_cloud, create_frp_plugin
from mcp_transfer.config import Settings
from mcp_transfer.frp import FrpProcess, download_frp, write_frps
from mcp_transfer.protocol import mapped_client


@pytest.mark.skipif(os.environ.get("MCPT_E2E") != "1", reason="Real multi-cloud CLI + frp integration")
async def test_real_two_clouds_share_one_host_and_client_ui_without_overwriting_connections(tmp_path, monkeypatch):
    import subprocess

    from mcp_transfer.database import Device
    from mcp_transfer.security import digest

    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")

    workers = []
    frps = []
    logs = []
    try:
        async with AsyncExitStack() as stack:
            admins = []
            for index in range(2):
                settings = Settings(tmp_path / f"cloud-{index}")
                settings.data.update(frp_port=available_port(), plugin_port=available_port())
                settings.save()
                port = available_port()
                origin = f"http://127.0.0.1:{port}"
                initialize_cloud(settings, origin, password="multi-cloud-password-123", development=True)
                app = create_cloud(settings)
                await stack.enter_async_context(serve(app, port))
                await stack.enter_async_context(serve(create_frp_plugin(settings), settings.data["plugin_port"]))
                admin = await stack.enter_async_context(httpx.AsyncClient(base_url=origin, trust_env=False, timeout=30))
                login = await admin.post("/api/auth/login", json={"username": "admin", "password": "multi-cloud-password-123"})
                admin.headers["x-csrf-token"] = login.json()["csrf"]
                if index:
                    owner = (await admin.get("/api/me")).json()["id"]
                    with app.state.db.begin() as session:
                        session.add(Device(id="d_reserved", owner=owner, name="Reserve local test port", role="host", active=False, refresh_hash=digest("reserved"), refresh_expires=0, port=21000))
                await download_frp(settings.home, "frps")
                process = FrpProcess(settings, "frps")
                await process.start()
                frps.append(process)
                admins.append((admin, origin))
            host_home = tmp_path / "host"
            host_settings = Settings(host_home)
            host_settings.data.update(role="host", adapter_port=available_port())
            host_settings.save()
            await download_frp(host_home, "frpc")
            client_home = tmp_path / "client"
            frontends = {}
            for role, home in [("host", host_home), ("client", client_home)]:
                port = available_port()
                log = open(tmp_path / (role + "-cli.log"), "wb")
                logs.append(log)
                worker = subprocess.Popen([sys.executable, "-m", "mcp_transfer", "--home", str(home), role, "--port", str(port), "--development"], stdout=log, stderr=log)
                workers.append(worker)
                ui = await stack.enter_async_context(httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=45))
                for _ in range(200):
                    try:
                        response = await ui.get("/healthz")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert worker.poll() is None
                    await asyncio.sleep(0.05)
                else:
                    raise RuntimeError("CLI WebUI did not start")
                from mcp_transfer.vault import Vault

                password = Vault(home).read()["local_password"]
                login = await ui.post("/api/auth/login", json={"password": password})
                ui.headers["x-csrf-token"] = login.json()["csrf"]
                prefixes = []
                for index, (admin, origin) in enumerate(admins):
                    code = (await admin.post("/api/enrollment-codes", json={"role": role})).json()["code"]
                    route = "/api/configure" if not index else "/api/connections"
                    registered = await ui.post(route, json={"cloud_url": origin, "name": role + str(index), "code": code})
                    assert registered.status_code == 200, registered.text
                    prefix = "" if not index else "/connections/" + registered.json()["id"]
                    prefixes.append(prefix)
                    if role == "host":
                        added = await ui.post(prefix + "/api/servers", json={"name": f"Cloud {index} demo", "transport": "stdio", "command": sys.executable, "args": ["-m", "mcp_transfer", "demo-server"]})
                        assert added.status_code == 200
                        assert (await ui.post(prefix + "/api/sync", json={})).status_code == 200
                frontends[role] = (ui, prefixes)
            client_ui, prefixes = frontends["client"]
            for index, prefix in enumerate(prefixes):
                for _ in range(50):
                    await client_ui.post(prefix + "/api/sync", json={})
                    catalog = (await client_ui.get(prefix + "/api/servers")).json()
                    if catalog:
                        break
                    await asyncio.sleep(0.1)
                assert len(catalog) == 1
                assert catalog[0]["name"] == f"Cloud {index} demo"
                await client_ui.post(prefix + "/api/subscribe", json={"all": True})
                home = client_home if not prefix else client_home / "connections" / prefix.rsplit("/", 1)[-1]
                parameters = StdioServerParameters(command=sys.executable, args=["-m", "mcp_transfer", "--home", str(home), "connect", "--server", catalog[0]["id"]])
                # Retry only read-only discovery while the new TLS adapter starts.
                for attempt in range(12):
                    try:
                        async with Client(parameters, mode="legacy", cache=None) as mcp:
                            listed = await mcp.list_tools()
                            assert "add" in {tool.name for tool in listed.tools}
                            assert mcp.server_info.name == f"Cloud {index} demo"
                            result = await mcp.call_tool("add", {"a": 20, "b": 22})
                            assert result.content[0].text == "42"
                        break
                    except Exception:
                        if attempt == 11:
                            raise
                        await asyncio.sleep(0.5)
            assert len((await frontends["host"][0].get("/api/connections")).json()) == 2
            assert len((await client_ui.get("/api/connections")).json()) == 2
            assert not list(client_home.rglob("frpc*"))
    finally:
        for worker in reversed(workers):
            if worker.poll() is None:
                worker.terminate()
                try:
                    await asyncio.to_thread(worker.wait, timeout=15)
                except subprocess.TimeoutExpired:
                    worker.kill()
                    await asyncio.to_thread(worker.wait)
        for process in reversed(frps):
            await process.stop()
        for log in logs:
            log.close()


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
@pytest.mark.parametrize("mode,public_http,old_upstream", [
    ("auto", False, False), ("legacy", False, False), ("auto", True, False),
    ("legacy", True, True),
])
async def test_real_frp_stdio_roundtrip_and_revocation(tmp_path, monkeypatch, mode, public_http, old_upstream):
    cloud_port, adapter_port, client_port = available_port(), available_port(), available_port()
    cloud_settings = Settings(tmp_path / "cloud")
    cloud_settings.data["frp_port"] = available_port()
    cloud_settings.data["plugin_port"] = available_port()
    cloud_settings.save()
    # Go uses the macOS system trust store instead of SSL_CERT_FILE. Keep the
    # test-only cloud origin on loopback HTTP there; Linux CI exercises HTTPS.
    use_https = sys.platform.startswith("linux") and not public_http
    development = not use_https and not public_http
    public = f"{'https' if use_https else 'http'}://127.0.0.1:{cloud_port}"
    initialize_cloud(
        cloud_settings,
        public,
        password="integration-password-123",
        development=development,
        allow_insecure_http=public_http,
    )
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
                await host.enroll(
                    public, code, "Integration host", development=development, allow_insecure_http=public_http
                )
                user_response = await admin.post(
                    "/api/users", json={"username": "consumer", "password": "consumer-password-123"}
                )
                owner = user_response.json()["id"]
                code = (
                    await admin.post("/api/enrollment-codes", json={"role": "client", "owner": owner})
                ).json()["code"]
                await agent.enroll(
                    public,
                    code,
                    "Integration client",
                    development=development,
                    allow_insecure_http=public_http,
                )
                assert agent.frp is None
                assert not (client_settings.home / "bin").exists()
                host.vault.update(
                    services={
                        "srv_demo": {
                            "name": "Demo",
                            "transport": "stdio",
                            "command": sys.executable,
                            "args": ([str(Path(__file__).parent / "fixtures/legacy_stdio_server.py")]
                                     if old_upstream else ["-m", "mcp_transfer", "demo-server"]),
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
                        async with httpx.AsyncClient(
                            base_url=f"http://127.0.0.1:{client_port}", trust_env=False, timeout=45
                        ) as local:
                            login = await local.post("/api/auth/login", json={
                                "password": agent.vault.read()["local_password"]
                            })
                            local.headers["x-csrf-token"] = login.json()["csrf"]
                            check = await local.post("/api/servers/srv_demo/test", json={})
                            assert check.status_code == 200, check.text
                            assert check.json()["stage"] == "client_roundtrip"
                            assert [tool["name"] for tool in check.json()["tools"]] == ["add"]
                            assert check.json()["tool_count"] == 1
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
                                # Exercise the exact executable bridge used by exported application configs.
                                await agent.tick()
                                params = StdioServerParameters(
                                    command=sys.executable,
                                    args=[
                                        "-m",
                                        "mcp_transfer",
                                        "--home",
                                        str(client_settings.home),
                                        "connect",
                                        "--server",
                                        "srv_demo",
                                    ],
                                )
                                async with Client(params, cache=None, mode=mode) as bridged:
                                    bridge_tools = await bridged.list_tools()
                                    assert [tool.name for tool in bridge_tools.tools] == ["add"]
                                    bridge_result = await bridged.call_tool("add", {"a": 19, "b": 23})
                                    assert "42" in bridge_result.model_dump_json()
                                assert (
                                    await admin.post(
                                        f"/api/devices/{client_settings.data['device_id']}/revoke"
                                    )
                                ).status_code == 200
                                with pytest.raises((MCPError, httpx2.HTTPError)):
                                    await mcp.call_tool("add", {"a": 1, "b": 2})
                                old_device = client_settings.data["device_id"]
                                recovery_code = (await admin.post(
                                    "/api/enrollment-codes", json={"role": "client", "owner": owner}
                                )).json()["code"]
                                recovered = await agent.reconnect_client(
                                    public, recovery_code, "Recovered client", allow_insecure_http=public_http
                                )
                                assert recovered["synced"] is True
                                assert client_settings.data["device_id"] != old_device
                                local_token = agent.vault.read()["mappings"]["srv_demo"]["token"]
                                async with mapped_client(client_port, "srv_demo", local_token) as fresh:
                                    assert [tool.name for tool in (await fresh.list_tools()).tools] == ["add"]
                                    assert "42" in (await fresh.call_tool("add", {"a": 20, "b": 22})).model_dump_json()
    finally:
        await host.close()
        await agent.close()
        await frps.stop()


@pytest.mark.skipif(os.environ.get("MCPT_E2E") != "1", reason="Real runtime and pinned frp integration")
async def test_running_host_recovers_through_webui_with_its_services_and_tls_adapter(tmp_path, monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    public_port, host_ui, client_port = available_port(), available_port(), available_port()
    cloud_settings = Settings(tmp_path / "cloud")
    cloud_settings.data.update(frp_port=available_port(), plugin_port=available_port())
    public = f"http://127.0.0.1:{public_port}"
    initialize_cloud(cloud_settings, public, password="runtime-recovery-password-123", allow_insecure_http=True)
    await download_frp(cloud_settings.home, "frps")
    frps = FrpProcess(cloud_settings, "frps")
    host_settings = Settings(tmp_path / "host")
    host_settings.data.update(adapter_port=available_port(), ui_port=host_ui)
    host = Agent(host_settings, "host")
    await download_frp(host_settings.home, "frpc")
    client_settings = Settings(tmp_path / "client")
    client_settings.data["ui_port"] = client_port
    client = Agent(client_settings, "client")
    process = None
    log = open(tmp_path / "runtime.log", "wb")
    try:
        async with serve(create_cloud(cloud_settings), public_port), serve(
            create_frp_plugin(cloud_settings), cloud_settings.data["plugin_port"]
        ):
            async with httpx.AsyncClient(base_url=public, trust_env=False, timeout=60) as admin:
                login = await admin.post("/api/auth/login", json={"username": "admin", "password": "runtime-recovery-password-123"})
                admin.headers["x-csrf-token"] = login.json()["csrf"]
                code = (await admin.post("/api/enrollment-codes", json={"role": "host"})).json()["code"]
                await host.enroll(public, code, "旧主机", allow_insecure_http=True)
                old_id = host_settings.data["device_id"]
                host.vault.update(services={"srv_runtime": {
                    "name": "legacy-demo", "transport": "stdio", "command": sys.executable,
                    "args": [str(Path(__file__).parent / "fixtures/legacy_stdio_server.py")], "enabled": True,
                }})
                await frps.start()
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-X", "utf8=0", "-m", "mcp_transfer", "--home", str(host_settings.home),
                    "host", "--port", str(host_ui), "--allow-insecure-http", stdout=log, stderr=log,
                )
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{host_ui}", trust_env=False, timeout=60) as local:
                    for _ in range(120):
                        if process.returncode is not None:
                            raise AssertionError((tmp_path / "runtime.log").read_text(encoding="utf-8", errors="replace"))
                        try:
                            health = await local.get("/healthz")
                            servers = await admin.get("/api/servers")
                            if health.status_code == 200 and any(x["id"] == "srv_runtime" for x in servers.json()):
                                break
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(0.1)
                    else:
                        raise AssertionError("Runtime did not publish its service")
                    login = await local.post("/api/auth/login", json={"password": host.vault.read()["local_password"]})
                    local.headers["x-csrf-token"] = login.json()["csrf"]
                    code = (await admin.post("/api/enrollment-codes", json={"role": "client"})).json()["code"]
                    await client.enroll(public, code, "Client", allow_insecure_http=True)
                    await client.tick()
                    client.subscribe(["srv_runtime"])
                    assert (await admin.post(f"/api/devices/{old_id}/revoke")).status_code == 200
                    assert (await admin.delete(f"/api/devices/{old_id}")).status_code == 200
                    code = (await admin.post("/api/enrollment-codes", json={"role": "host"})).json()["code"]
                    result = await local.post("/api/reconnect", json={
                        "cloud_url": public, "code": code, "name": "恢复主机", "allow_insecure_http": True,
                    })
                    assert result.status_code == 200, result.text
                    assert result.json()["synced"] is True
                    assert result.json()["tunnel_ready"] is True, result.text
                    new_id = Settings(host_settings.home).data["device_id"]
                    assert new_id != old_id
                    server = (await admin.get("/api/servers")).json()[0]
                    new_sid = server["id"]
                    assert new_sid != "srv_runtime" and server["host_id"] == new_id
                    client.settings.data["subscribe_all"] = True
                    await client.tick()
                    async with serve(create_agent_app(client, background=False), client_port):
                        token = client.vault.read()["mappings"][new_sid]["token"]
                        async with mapped_client(client_port, new_sid, token) as peer:
                            assert [tool.name for tool in (await peer.list_tools()).tools] == ["add"]
                            assert "42" in (await peer.call_tool("add", {"a": 20, "b": 22})).model_dump_json()
    finally:
        if process and process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 15)
        log.close()
        await host.close()
        await client.close()
        await frps.stop()
