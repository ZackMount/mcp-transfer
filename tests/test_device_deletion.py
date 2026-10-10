import httpx
from conftest import access, enroll
from fastapi.testclient import TestClient
from sqlalchemy import select

from mcp_transfer.agent import Agent
from mcp_transfer.config import Settings
from mcp_transfer.database import Device, Grant, Service
from mcp_transfer.pki import make_csr


def test_cleanup_removes_old_hosts_services_and_grants_but_preserves_live_devices(cloud, tmp_path):
    admin, app, _ = cloud
    host = enroll(admin, "host", csr=make_csr(tmp_path / "keys", "host"))
    live = enroll(admin, "client", name="Live client")
    headers = access(admin, host)
    admin.put("/api/hosts/me/servers/srv_old", headers=headers, json={"name": "Old", "transport": "stdio"})
    admin.post("/api/grants", json={"user_id": admin.get("/api/me").json()["id"], "server_id": "srv_old", "policy": {"tools": ["*"]}})
    admin.post(f"/api/devices/{host['device_id']}/revoke")
    result = admin.post("/api/devices/cleanup")
    assert result.status_code == 200
    assert result.json() == {"devices": 1, "servers": 1}
    with app.state.db() as db:
        assert db.get(Device, host["device_id"]) is None
        assert db.get(Device, live["device_id"]).active is True
        assert db.get(Service, "srv_old") is None
        assert not list(db.scalars(select(Grant)))
    assert admin.post("/api/token", json={"refresh_token": host["refresh_token"]}).status_code == 401


def test_user_cannot_delete_another_owners_device(cloud):
    admin, app, _ = cloud
    device = enroll(admin)
    admin.post("/api/users", json={"username": "other", "password": "other-password-123"})
    with TestClient(app, base_url="https://testserver") as other:
        login = other.post("/api/auth/login", json={"username": "other", "password": "other-password-123"})
        other.headers["x-csrf-token"] = login.json()["csrf"]
        assert other.delete(f"/api/devices/{device['device_id']}").status_code == 404


async def test_host_ordinary_code_reenrollment_preserves_commands_and_republishes_new_ids(cloud, tmp_path):
    admin, app, _ = cloud
    settings = Settings(tmp_path)
    settings.data["disable_frp"] = True
    host = Agent(settings, "host")
    host.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    code = admin.post("/api/enrollment-codes", json={"role": "host"}).json()["code"]
    await host.enroll("https://testserver", code, "Old host")
    definition = {"name": "cheat-engine", "transport": "stdio", "command": "python", "args": ["bridge.py"], "env": {"SECRET": "local-only"}, "enabled": True}
    host.vault.update(services={"srv_original": definition})
    await host.tick()
    old_id = settings.data["device_id"]
    assert admin.delete(f"/api/devices/{old_id}").status_code == 200
    code = admin.post("/api/enrollment-codes", json={"role": "host"}).json()["code"]
    assert (await host.reconnect("https://testserver", code, "New host"))["synced"] is True
    services = host.vault.read()["services"]
    assert list(services.values()) == [definition]
    assert "srv_original" not in services
    assert settings.data["device_id"] != old_id
    published = admin.get("/api/servers").json()
    assert len(published) == 1
    assert published[0]["id"] in services
    assert published[0]["name"] == "cheat-engine"
    await host.close()
