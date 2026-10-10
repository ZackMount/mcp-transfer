import httpx
import pytest
from conftest import enroll

from mcp_transfer.agent import Agent
from mcp_transfer.config import Settings
from mcp_transfer.pki import make_csr


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
