import json

import httpx
import pytest
from conftest import access, enroll
from fastapi.testclient import TestClient

from mcp_transfer.agent import Agent, create_agent_app
from mcp_transfer.cli import initialize_cloud
from mcp_transfer.cloud import create_cloud
from mcp_transfer.config import Settings
from mcp_transfer.connections import Connections
from mcp_transfer.exporters import generate
from mcp_transfer.pki import make_csr


async def test_two_clouds_share_ui_but_isolate_credentials_catalog_and_routes(cloud, tmp_path, monkeypatch):
    monkeypatch.setattr("mcp_transfer.exporters.application_paths", lambda: {"cursor": str(tmp_path / "cursor.json")})
    first_admin, first_app, _ = cloud
    second_settings = Settings(tmp_path / "second-cloud")
    initialize_cloud(second_settings, "https://secondserver", password="second-admin-password-123")
    second_app = create_cloud(second_settings)
    apps = {"testserver": first_app, "secondserver": second_app}

    async def route(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=apps[request.url.host])) as upstream:
            return await upstream.send(request)

    monkeypatch.setattr(Agent, "cloud_http", lambda _: httpx.AsyncClient(transport=httpx.MockTransport(route)))
    with TestClient(second_app, base_url="https://secondserver") as second_admin:
        login = second_admin.post("/api/auth/login", json={"username": "admin", "password": "second-admin-password-123"})
        second_admin.headers["x-csrf-token"] = login.json()["csrf"]
        for admin, name in [(first_admin, "First tool"), (second_admin, "Second tool")]:
            host = enroll(admin, "host", csr=make_csr(tmp_path / name, "host"))
            assert admin.put("/api/hosts/me/servers/srv_shared", headers=access(admin, host), json={"name": name, "transport": "stdio"}).status_code == 200
        primary = Agent(Settings(tmp_path / "client"), "client")
        app = create_agent_app(primary, background=False)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:17820") as ui:
            assert (await ui.get("/api/connections")).status_code == 401
            login = await ui.post("/api/auth/login", json={"password": primary.vault.read()["local_password"]})
            ui.headers["x-csrf-token"] = login.json()["csrf"]
            code = first_admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
            assert (await ui.post("/api/configure", json={"cloud_url": "https://testserver", "code": code, "name": "First client"})).status_code == 200
            old_secret = primary.vault.read()["refresh_token"]
            code = second_admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
            result = await ui.post("/api/connections", json={"cloud_url": "https://secondserver", "code": code, "name": "Second client", "label": "Second cloud"})
            assert result.status_code == 200, result.text
            cid = result.json()["id"]
            child = app.state.connections.agents[cid]
            prefix = "/connections/" + cid
            assert (await ui.post(prefix + "/api/sync", json={})).status_code == 200
            assert primary.vault.read()["refresh_token"] == old_secret
            wrong_origin = await ui.put("/api/connection", json={"cloud_url": "https://secondserver"})
            assert wrong_origin.status_code == 400
            assert primary.settings.data["cloud_url"] == "https://testserver"
            assert primary.vault.read()["refresh_token"] == old_secret
            assert child.frp is None and not (child.settings.home / "bin").exists()
            assert not {"frp_port", "frp_server", "remote_port"} & child.settings.data.keys()
            assert (await ui.get("/api/servers")).json()[0]["name"] == "First tool"
            assert (await ui.get(prefix + "/api/servers")).json()[0]["name"] == "Second tool"
            for route_prefix in ("", prefix):
                assert (await ui.post(route_prefix + "/api/subscribe", json={"all": True})).status_code == 200
            wrong_token = primary.vault.read()["mappings"]["srv_shared"]["token"]
            assert (await ui.post(prefix + "/mcp/srv_shared", headers={"Authorization": "Bearer " + wrong_token}, json={})).status_code == 401
            duplicate = await ui.post("/api/connections", json={"cloud_url": "https://SECONDserver:443", "name": "duplicate", "code": "unused"})
            assert duplicate.status_code == 400
            preview = (await ui.post("/api/exports/preview", json={"target": "cursor", "scope": "all"})).json()
            group = json.loads(preview["content"])["mcpServers"]
            assert len(group) == 2
            assert (await ui.post("/api/exports/apply", json={"preview_id": preview["preview_id"]})).status_code == 200
            assert {tuple(entry["args"][-2:]) for entry in group.values()} == {("--server", "srv_shared")}
            restored = Connections(Agent(Settings(primary.settings.home), "client"))
            assert set(restored.agents) == {"primary", cid}
            await restored.close()
            deleted = await ui.delete("/api/connections/" + cid)
            assert deleted.status_code == 200, deleted.text
            assert (await ui.get(prefix + "/api/status")).status_code == 404
            remaining_config = json.loads((tmp_path / "cursor.json").read_text())["mcpServers"]
            assert len(remaining_config) == 1
            assert (await ui.get("/api/status")).json()["device_id"] == primary.settings.data["device_id"]
            devices = second_admin.get("/api/devices").json()
            assert next(item for item in devices if item["id"] == child.settings.data["device_id"])["active"] is False
            await app.state.connections.close()


@pytest.mark.parametrize("target", ["cursor", "codex"])
@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_export_of_one_cloud_never_removes_other_cloud_entries(tmp_path, target, mode):
    mappings = {"srv_shared": {"token": "local"}}
    first = generate(target, ["srv_shared"], tmp_path / "first", mappings, 17820, mode)
    second = generate(target, ["srv_shared"], tmp_path / "second", mappings, 17820, mode, first, route_prefix="/connections/conn_other")
    assert "mcpt_srv_shared" in second and "mcpt_conn_other_srv_shared" in second
    removed = generate(target, [], tmp_path / "second", mappings, 17820, mode, second, route_prefix="/connections/conn_other")
    assert "mcpt_srv_shared" in removed and "mcpt_conn_other_srv_shared" not in removed


async def test_failed_new_cloud_enrollment_preserves_old_profile_and_cleans_staging(tmp_path, monkeypatch):
    primary = Agent(Settings(tmp_path), "client")
    primary.settings.data.update(cloud_url="https://first.example", device_id="old")
    primary.settings.save()
    primary.vault.update(refresh_token="old-secret")

    async def refused(request):
        return httpx.Response(401, json={"detail": "expired"})

    monkeypatch.setattr(Agent, "cloud_http", lambda _: httpx.AsyncClient(transport=httpx.MockTransport(refused)))
    registry = Connections(primary)
    with pytest.raises(httpx.HTTPStatusError):
        await registry.add({"cloud_url": "https://second.example", "name": "New", "code": "invalid"})
    assert primary.vault.read()["refresh_token"] == "old-secret"
    assert not list((tmp_path / "connections").glob("conn_*"))
    assert list(registry.agents) == ["primary"]
    await registry.close()
