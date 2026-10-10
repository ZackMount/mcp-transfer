import time

import httpx
import pytest
from conftest import access, enroll
from sqlalchemy import select

from mcp_transfer.agent import Agent
from mcp_transfer.config import Settings
from mcp_transfer.database import Device, Grant, Service
from mcp_transfer.pki import make_csr


def recovery_code(admin, did):
    response = admin.post(f"/api/devices/{did}/recovery-code")
    assert response.status_code == 200, response.text
    return response.json()["code"]


@pytest.mark.parametrize("revoked", [False, True])
async def test_host_recovery_preserves_services_grants_and_local_keys_on_failure(cloud, tmp_path, revoked):
    admin, app, _ = cloud
    settings = Settings(tmp_path / "host")
    settings.data["disable_frp"] = True
    host = Agent(settings, "host")
    host.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    code = admin.post("/api/enrollment-codes", json={"role": "host"}).json()["code"]
    await host.enroll("https://testserver", code, "Original host")
    host.vault.update(services={"srv_kept": {
        "name": "cheat-engine", "transport": "stdio", "command": "python",
        "args": ["bridge_server.py"], "env": {"SECRET": "host-only"}, "enabled": True,
    }})
    await host.tick()
    old_id, old_port = settings.data["device_id"], settings.data["remote_port"]
    old_key = (settings.home / "device.key").read_bytes()
    old_cert = (settings.home / "device.crt").read_bytes()
    old_services = host.vault.read()["services"]
    # Invalid registration must never replace a still-usable private key.
    bad = app.state.signer.issue(old_id, "device-recovery", ttl=-30, kind="recover", role="host")
    with pytest.raises(ValueError, match="恢复码"):
        await host.reconnect("https://testserver", bad, "Original host")
    assert (settings.home / "device.key").read_bytes() == old_key
    assert (settings.home / "device.crt").read_bytes() == old_cert
    consumer = enroll(admin, "client")
    grant = admin.post("/api/grants", json={
        "user_id": admin.get("/api/me").json()["id"],
        "server_id": "srv_kept", "device_id": consumer["device_id"], "policy": {"tools": ["read"]},
    })
    assert grant.status_code == 200, grant.text
    if revoked:
        admin.post(f"/api/devices/{old_id}/revoke")
    else:
        with app.state.db.begin() as db:
            db.get(Device, old_id).refresh_expires = time.time() - 1
        host.access = ""
    result = await host.reconnect("https://testserver", recovery_code(admin, old_id), "Recovered host")
    assert result["synced"] is True
    assert settings.data["device_id"] != old_id
    assert settings.data["remote_port"] == old_port
    assert host.vault.read()["services"] == old_services
    assert (settings.home / "device.key").read_bytes() != old_key
    with app.state.db() as db:
        assert db.get(Device, old_id).active is False
        assert db.get(Device, old_id).port is None
        assert db.get(Service, "srv_kept").host_id == settings.data["device_id"]
        kept_grant = db.scalar(select(Grant).where(Grant.server_id == "srv_kept"))
        assert kept_grant.device_id == consumer["device_id"]
        assert kept_grant.policy == {"tools": ["read"]}
    assert admin.post(f"/api/devices/{old_id}/recovery-code").status_code == 409
    await host.close()


def test_recovery_binding_role_single_use_and_old_credentials_stay_revoked(cloud, tmp_path):
    admin, app, _ = cloud
    original = enroll(admin, "host", csr=make_csr(tmp_path / "old", "old"))
    old_api = access(admin, original)
    old_frp = admin.post("/oauth/token", data={
        "grant_type": "client_credentials", "client_id": original["device_id"],
        "client_secret": original["frp_secret"],
    }).json()["access_token"]
    code = recovery_code(admin, original["device_id"])
    assert admin.post("/api/devices/enroll", json={"code": code}).status_code == 400
    assert admin.post("/api/devices/recover", json={"code": code, "role": "client"}).status_code == 403
    assert admin.post("/api/devices/recover", json={"code": code, "role": "host", "csr": "bad"}).status_code == 400
    body = {"code": code, "role": "host", "csr": make_csr(tmp_path / "new", "new")}
    response = admin.post("/api/devices/recover", json=body)
    assert response.status_code == 200, response.text
    assert admin.post("/api/devices/recover", json=body).status_code == 401
    assert admin.post("/api/devices/heartbeat", headers=old_api).status_code == 401
    assert admin.post("/api/token", json={"refresh_token": original["refresh_token"]}).status_code == 401
    assert admin.post("/oauth/token", data={
        "grant_type": "client_credentials", "client_id": original["device_id"],
        "client_secret": original["frp_secret"],
    }).status_code == 401
    from fastapi.testclient import TestClient

    from mcp_transfer.cloud import create_frp_plugin

    with TestClient(create_frp_plugin(app.state.settings)) as plugin:
        assert plugin.post("/internal/frp?op=Ping", json={"content": {
            "user": {"user": original["device_id"]}, "privilege_key": old_frp
        }}).json()["reject"] is True


def test_recovery_code_is_not_available_to_another_owner(cloud):
    admin, app, _ = cloud
    device = enroll(admin)
    from fastapi.testclient import TestClient

    admin.post("/api/users", json={"username": "other", "password": "other-password-123"})
    with TestClient(app, base_url="https://testserver") as other:
        login = other.post("/api/auth/login", json={"username": "other", "password": "other-password-123"})
        other.headers["x-csrf-token"] = login.json()["csrf"]
        assert other.post(f"/api/devices/{device['device_id']}/recovery-code").status_code == 404


@pytest.mark.parametrize("has_proof", [False, True])
def test_nonadmin_recovery_cannot_move_device_scoped_grants_without_old_credentials(cloud, tmp_path, has_proof):
    admin, app, _ = cloud
    from fastapi.testclient import TestClient

    owner = admin.post("/api/users", json={"username": "consumer", "password": "consumer-password-123"}).json()["id"]
    host = enroll(admin, "host", csr=make_csr(tmp_path / "host", "host"))
    admin.put("/api/hosts/me/servers/srv_scoped", headers=access(admin, host), json={"name": "Scoped", "transport": "stdio"})
    device = enroll(admin, "client", owner=owner)
    grant = admin.post("/api/grants", json={"user_id": owner, "server_id": "srv_scoped", "device_id": device["device_id"], "policy": {"tools": ["read"]}})
    assert grant.status_code == 200, grant.text
    with TestClient(app, base_url="https://testserver") as consumer:
        login = consumer.post("/api/auth/login", json={"username": "consumer", "password": "consumer-password-123"})
        consumer.headers["x-csrf-token"] = login.json()["csrf"]
        code = recovery_code(consumer, device["device_id"])
        body = {"code": code, "role": "client"}
        if has_proof:
            body["old_refresh_token"] = device["refresh_token"]
        response = consumer.post("/api/devices/recover", json=body)
        assert response.status_code == 200, response.text
        new_id = response.json()["device_id"]
        with app.state.db() as db:
            scoped = db.scalar(select(Grant).where(Grant.server_id == "srv_scoped"))
            assert scoped.device_id == (new_id if has_proof else device["device_id"])
            assert db.get(Device, device["device_id"]).active is False


async def test_failed_certificate_renewal_preserves_the_original_key_pair(cloud, tmp_path):
    admin, app, _ = cloud
    settings = Settings(tmp_path)
    settings.data["disable_frp"] = True
    host = Agent(settings, "host")
    host.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    code = admin.post("/api/enrollment-codes", json={"role": "host"}).json()["code"]
    await host.enroll("https://testserver", code, "Host")
    # Make its existing certificate eligible for renewal without changing its key.
    from mcp_transfer.config import atomic_write
    from mcp_transfer.pki import PKI

    original_csr = make_csr(tmp_path / "replacement", "host")
    atomic_write(tmp_path / "device.key", (tmp_path / "replacement/device.key").read_bytes())
    atomic_write(tmp_path / "device.crt", PKI(app.state.settings.home).sign_csr(
        original_csr, settings.data["device_id"], [settings.data["device_id"] + ".mcp.internal"], days=1
    ))
    key, cert = (tmp_path / "device.key").read_bytes(), (tmp_path / "device.crt").read_bytes()
    real_api = host.api

    async def fail_renewal(method, path, **kwargs):
        if path == "/api/devices/certificate":
            raise httpx.ConnectError("simulated outage")
        return await real_api(method, path, **kwargs)

    host.api = fail_renewal
    with pytest.raises(httpx.ConnectError):
        await host.tick()
    assert (tmp_path / "device.key").read_bytes() == key
    assert (tmp_path / "device.crt").read_bytes() == cert
    await host.close()


def test_refresh_retry_recovers_a_lost_response_without_disabling_reuse_detection(cloud):
    admin, _, _ = cloud
    device = enroll(admin)
    original = device["refresh_token"]
    body = {"refresh_token": original, "request_id": "nonce-for-lost-response-123"}
    first = admin.post("/api/token", json=body)
    retry = admin.post("/api/token", json=body)
    assert first.status_code == retry.status_code == 200
    assert first.json()["refresh_token"] == retry.json()["refresh_token"]
    malicious = admin.post("/api/token", json={**body, "request_id": "different-nonce-12345"})
    assert malicious.status_code == 401
    assert admin.post("/api/token", json={"refresh_token": first.json()["refresh_token"]}).status_code == 401


async def test_agent_keeps_the_nonce_after_a_lost_token_response(cloud, tmp_path):
    admin, app, _ = cloud
    agent = Agent(Settings(tmp_path), "client")
    agent.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    await agent.enroll("https://testserver", code, "Client")
    real_transport = httpx.ASGITransport(app=app)
    fail_once = True

    class LostResponse(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal fail_once
            response = await real_transport.handle_async_request(request)
            if request.url.path == "/api/token" and fail_once:
                fail_once = False
                await response.aclose()
                raise httpx.RemoteProtocolError("simulated lost response")
            return response

    agent.cloud_http = lambda: httpx.AsyncClient(transport=LostResponse())
    with pytest.raises(httpx.RemoteProtocolError):
        await agent.cloud_access()
    pending = agent.vault.read()["refresh_request_id"]
    assert pending
    assert await agent.cloud_access()
    assert not agent.vault.read()["refresh_request_id"]
    assert admin.get("/api/devices").json()[0]["active"] is True
    await agent.close()
