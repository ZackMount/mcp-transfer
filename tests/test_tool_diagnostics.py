import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import mcp.types as types
import pytest
from fastapi.testclient import TestClient

from mcp_transfer.agent import Agent, create_agent_app
from mcp_transfer.config import Settings
from mcp_transfer.protocol import WireResult, inspect_tools, proxy_server


def logged_in(agent):
    client = TestClient(create_agent_app(agent, background=False), base_url=f"http://127.0.0.1:{agent.port}")
    result = client.post("/api/auth/login", json={"password": agent.vault.read()["local_password"]})
    client.headers["x-csrf-token"] = result.json()["csrf"]
    return client


def test_import_preserves_real_server_name_and_stdio_arguments(tmp_path):
    agent = Agent(Settings(tmp_path), "host")
    with logged_in(agent) as client:
        result = client.post("/api/import", json={"mcpServers": {"cheat-engine": {
            "command": r"C:\Users\Win10\AppData\Local\Programs\Python\Python314\python.exe",
            "args": [r"C:\Users\Win10\Desktop\3\CE-MCP\bridge_server.py", "--host", "127.0.0.1", "--port", "8888"],
        }}})
        assert result.status_code == 200, result.text
        services = agent.vault.read()["services"]
        assert len(services) == 1
        service = services[result.json()["ids"][0]]
        assert service["name"] == "cheat-engine"
        assert service["transport"] == "stdio"
        assert service["args"][-4:] == ["--host", "127.0.0.1", "--port", "8888"]
        assert service["enabled"] is False


def test_host_diagnostic_reads_tools_from_a_real_2024_stdio_server(tmp_path):
    agent = Agent(Settings(tmp_path), "host")
    agent.vault.update(services={"srv_old": {
        "name": "cheat-engine", "transport": "stdio", "command": sys.executable,
        "args": [str(Path(__file__).parent / "fixtures/legacy_stdio_server.py")], "enabled": True,
    }})
    with logged_in(agent) as client:
        response = client.post("/api/servers/srv_old/test", json={})
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["version"] == "2024-11-05"
        assert data["stage"] == "host_upstream"
        assert data["server_name"] == "legacy-cheat-engine"
        assert data["tool_count"] == 1
        assert [tool["name"] for tool in data["tools"]] == ["add"]


def test_client_diagnostic_requires_auth_and_an_enabled_authorized_mapping(tmp_path):
    agent = Agent(Settings(tmp_path), "client")
    app = create_agent_app(agent, background=False)
    with TestClient(app, base_url="http://127.0.0.1:17820") as client:
        assert client.post("/api/servers/srv_a/test", json={}).status_code == 401
    with logged_in(agent) as client:
        assert client.post("/api/servers/srv_a/test", json={}).status_code == 400
        agent.vault.update(mappings={"srv_a": {"token": "local", "enabled": True}})
        assert client.post("/api/servers/srv_a/test", json={}).status_code == 403
        agent.catalog = [{"id": "srv_a", "name": "Demo"}]
        agent.vault.update(mappings={"srv_a": {"token": "local", "enabled": False}})
        assert client.post("/api/servers/srv_a/test", json={}).status_code == 400


async def test_inspection_reads_all_tool_pages_and_detects_broken_pagination():
    tool = types.Tool(name="add", input_schema={"type": "object"})
    first = types.ListToolsResult(tools=[tool], next_cursor="second")
    last = types.ListToolsResult(tools=[types.Tool(name="read", input_schema={"type": "object"})])
    client = SimpleNamespace(
        server_capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
        server_info=types.Implementation(name="old-peer", version="1.0"),
        protocol_version="2024-11-05", list_tools=AsyncMock(side_effect=[first, last]),
    )
    result = await inspect_tools(client)
    assert result["tool_count"] == 2
    assert [item["name"] for item in result["tools"]] == ["add", "read"]
    client.list_tools = AsyncMock(return_value=first)
    with pytest.raises(ValueError, match="repeated"):
        await inspect_tools(client)


async def test_resource_only_peer_is_reported_with_zero_tools():
    client = SimpleNamespace(
        server_capabilities=types.ServerCapabilities(resources=types.ResourcesCapability()),
        server_info=None, protocol_version="2024-11-05", list_tools=AsyncMock(),
    )
    result = await inspect_tools(client)
    assert result["tool_count"] == 0
    assert result["capabilities"].get("resources") is not None
    client.list_tools.assert_not_called()


@pytest.mark.parametrize("version", ["2024-11-05", "2026-07-28"])
async def test_proxy_adapts_legacy_tool_results_to_downstream_version(version):
    from mcp_types import methods

    upstream = SimpleNamespace(
        instructions=None, server_capabilities=types.ServerCapabilities(tools=types.ToolsCapability()),
        session=SimpleNamespace(send_request=AsyncMock(return_value=WireResult.model_validate({
            "tools": [{"name": "add", "inputSchema": {"type": "object"}}]
        }))),
    )
    server = proxy_server(upstream, "cheat-engine")
    handler = server.get_request_handler("tools/list")
    result = await handler.handler(SimpleNamespace(meta=None, protocol_version=version), handler.params_type())
    validated = methods.serialize_server_result("tools/list", version, result)
    assert validated["tools"][0]["name"] == "add"
    if version == "2026-07-28":
        assert validated["resultType"] == "complete"
        assert validated["ttlMs"] == 0
        assert validated["cacheScope"] == "private"
    else:
        assert "resultType" not in validated
        assert "ttlMs" not in validated
