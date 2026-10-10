import json
import sys
import tomllib

import pytest
from fastapi.testclient import TestClient

from mcp_transfer.agent import Agent, create_agent_app, validate_service
from mcp_transfer.config import Settings
from mcp_transfer.exporters import generate, write_export
from mcp_transfer.policy import authorize_message, filter_result


def test_exports_preserve_unrelated_entries_and_contain_no_cloud_secret(tmp_path):
    mappings = {"srv_a": {"token": "local-only", "enabled": True}}
    original = '{"mcpServers":{"existing":{"command":"test"}},"theme":"dark"}'
    content = generate("cursor", ["srv_a"], tmp_path, mappings, 17820, existing=original)
    result = json.loads(content)
    assert result["theme"] == "dark"
    assert result["mcpServers"]["existing"] == {"command": "test"}
    assert result["mcpServers"]["mcpt_srv_a"]["command"] == sys.executable
    assert "local-only" not in content
    toml = generate(
        "codex",
        ["srv_a"],
        tmp_path,
        mappings,
        17820,
        existing='# keep this comment\nmodel="configured-model"\n',
    )
    assert "# keep this comment" in toml
    assert tomllib.loads(toml)["model"] == "configured-model"
    assert "srv_a" in tomllib.loads(toml)["mcp_servers"]["mcpt_srv_a"]["args"]


def test_export_rejects_conflicts_and_changed_files(tmp_path):
    with pytest.raises(ValueError, match="not owned"):
        generate(
            "cursor",
            ["srv_a"],
            tmp_path,
            {},
            17820,
            existing='{"mcpServers":{"mcpt_srv_a":{"command":"user-program"}}}',
        )
    path = tmp_path / "config.json"
    path.write_text("changed")
    with pytest.raises(ValueError, match="changed after preview"):
        write_export({"path": str(path), "before": "old", "content": "new"})
    assert path.read_text() == "changed"


@pytest.mark.parametrize("target", ["cursor", "codex"])
@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_removes_revoked_platform_entries_without_other_changes(tmp_path, target, mode):
    mappings = {"srv_a": {"token": "local-only", "enabled": True}}
    existing = generate(target, ["srv_a"], tmp_path, mappings, 17820, mode)
    result = generate(target, [], tmp_path, mappings, 17820, mode, existing)
    assert "mcpt_srv_a" not in result


def test_pythonw_agent_exports_console_python_for_stdio(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", str(tmp_path / "pythonw.exe"))
    result = json.loads(generate("cursor", ["srv_a"], tmp_path, {}, 17820))
    assert result["mcpServers"]["mcpt_srv_a"]["command"].endswith("python.exe")


def test_manual_mapping_enable_does_not_undo_automatic_disable(tmp_path):
    agent = Agent(Settings(tmp_path), "client")
    agent.catalog = [{"id": "srv_a"}]
    agent.settings.data["subscribe_all"] = True
    agent.vault.update(mappings={"srv_a": {"token": "local", "enabled": False}})
    agent.subscribe(["srv_a"])
    assert agent.vault.read()["mappings"]["srv_a"]["enabled"] is False
    agent.subscribe(["srv_a"], enable_existing=True)
    assert agent.vault.read()["mappings"]["srv_a"]["enabled"] is True


def test_loopback_security_and_mapping_auth(tmp_path):
    agent = Agent(Settings(tmp_path), "client")
    with TestClient(create_agent_app(agent, background=False), base_url="http://127.0.0.1:17820") as client:
        assert client.get("/api/status").status_code == 401
        assert client.post("/mcp/srv_a", json={}).status_code == 401
        assert client.get("/api/info", headers={"origin": "https://evil.example"}).status_code == 403
        response = client.post("/api/auth/login", json={"password": agent.vault.read()["local_password"]})
        assert response.status_code == 200
        assert client.get("/api/status").status_code == 200
        assert client.post("/api/subscribe", json={"all": True}).status_code == 403
        assert client.post("/api/exports/apply", json={"path": "/tmp/arbitrary"}).status_code == 403


def test_upstream_url_allowlist_and_argument_shapes():
    for url in [
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/mcp",
        "file:///etc/passwd",
        "http://user:password@127.0.0.1/mcp",
    ]:
        with pytest.raises(ValueError):
            validate_service({"name": "bad", "transport": "http", "url": url})
    with pytest.raises(ValueError):
        validate_service({"name": "bad", "transport": "stdio", "command": "python", "args": "--command"})
    assert (
        validate_service({"name": "ok", "transport": "http", "url": "http://127.0.0.1:8080/mcp"})["transport"]
        == "http"
    )


def test_filtering_does_not_replace_call_authorization():
    policy = {"tools": ["read"]}
    result = filter_result(
        {"method": "tools/list"}, {"result": {"tools": [{"name": "read"}, {"name": "write"}]}}, policy
    )
    assert result["result"]["tools"] == [{"name": "read"}]
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        authorize_message({"method": "tools/call", "params": {"name": "write"}}, policy)
