import httpx
import pytest
from fastapi.testclient import TestClient

from mcp_transfer.agent import Agent, create_agent_app
from mcp_transfer.config import Settings


async def registered_client(cloud, tmp_path):
    admin, app, _ = cloud
    agent = Agent(Settings(tmp_path), "client")
    agent.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    await agent.enroll("https://testserver", code, "Original")
    await agent.tick()
    agent.vault.update(mappings={"srv_kept": {"token": "local-token", "enabled": True}})
    return agent


async def test_revoked_client_can_reenroll_without_losing_local_mappings(cloud, tmp_path):
    admin, _, _ = cloud
    agent = await registered_client(cloud, tmp_path)
    old_id = agent.settings.data["device_id"]
    old_password = agent.vault.read()["local_password"]
    old_mappings = agent.vault.read()["mappings"]
    agent.resource_tokens["srv_kept"] = ("old-resource-token", 9999999999)
    assert admin.post(f"/api/devices/{old_id}/revoke").status_code == 200
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    result = await agent.reconnect_client("https://testserver", code, "Recovered")
    assert result["synced"] is True
    assert agent.settings.data["device_id"] != old_id
    assert agent.settings.data["name"] == "Recovered"
    assert agent.vault.read()["mappings"] == old_mappings
    assert agent.vault.read()["local_password"] == old_password
    assert not agent.resource_tokens
    assert agent.frp is None
    assert not (tmp_path / "bin").exists()
    assert not {"frp_server", "frp_port", "remote_port"} & agent.settings.data.keys()
    devices = {item["id"]: item for item in admin.get("/api/devices").json()}
    assert devices[old_id]["active"] is False
    assert devices[agent.settings.data["device_id"]]["active"] is True
    await agent.close()


async def test_bad_recovery_code_preserves_existing_profile(cloud, tmp_path):
    admin, _, _ = cloud
    agent = await registered_client(cloud, tmp_path)
    admin.post(f"/api/devices/{agent.settings.data['device_id']}/revoke")
    settings_before = dict(agent.settings.data)
    secrets_before = agent.vault.read()
    with pytest.raises(httpx.HTTPStatusError):
        await agent.reconnect_client("https://testserver", "wrong-code", "Replacement")
    assert agent.settings.data == settings_before
    assert agent.vault.read() == secrets_before
    await agent.close()


async def test_recovery_works_when_local_refresh_credential_is_missing(cloud, tmp_path):
    admin, _, _ = cloud
    agent = await registered_client(cloud, tmp_path)
    admin.post(f"/api/devices/{agent.settings.data['device_id']}/revoke")
    agent.vault.update(refresh_token="")
    agent.access = ""
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    assert (await agent.reconnect_client("https://testserver", code, "Recovered"))["synced"] is True
    assert agent.vault.read()["mappings"]["srv_kept"]["token"] == "local-token"
    await agent.close()


async def test_live_registration_is_not_silently_replaced(cloud, tmp_path):
    admin, app, _ = cloud
    agent = await registered_client(cloud, tmp_path / "old")
    old_id = agent.settings.data["device_id"]
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    with pytest.raises(ValueError, match="仍然有效"):
        await agent.reconnect_client("https://testserver", code, "Replacement")
    assert agent.settings.data["device_id"] == old_id
    # Rejection happens before the new code is consumed.
    fresh = Agent(Settings(tmp_path / "new"), "client")
    fresh.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    await fresh.enroll("https://testserver", code, "Fresh")
    await fresh.close()
    await agent.close()


def test_recovery_api_requires_local_auth_and_csrf(tmp_path):
    agent = Agent(Settings(tmp_path / "host"), "host")
    with TestClient(create_agent_app(agent, background=False), base_url="http://127.0.0.1:17810") as client:
        assert client.post("/api/reconnect", json={}).status_code == 401
        login = client.post("/api/auth/login", json={"password": agent.vault.read()["local_password"]})
        assert client.post("/api/reconnect", json={}).status_code == 403
        client.headers["x-csrf-token"] = login.json()["csrf"]
        assert client.post("/api/reconnect", json={}).status_code == 400
