"""The company client must never use the host's frp transport."""

import subprocess
import sys

import httpx

from mcp_transfer.agent import Agent
from mcp_transfer.config import Settings


def test_clean_client_process_never_loads_frp_module(tmp_path):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import asyncio, sys; from pathlib import Path; import mcp_transfer.cli; from mcp_transfer.agent import Agent; from mcp_transfer.config import Settings; a=Agent(Settings(Path(sys.argv[1])), 'client'); assert a.frp is None; asyncio.run(a.close()); assert 'mcp_transfer.frp' not in sys.modules",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_client_profile_rejects_manual_frp_installation(tmp_path):
    settings = Settings(tmp_path)
    settings.data["role"] = "client"
    settings.save()
    result = subprocess.run(
        [sys.executable, "-m", "mcp_transfer", "--home", str(tmp_path), "install-frp", "--binary", "frpc"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 1
    assert "Client profiles do not support" in result.stderr
    assert not (tmp_path / "bin").exists()


async def test_client_registration_sync_and_reconnect_use_only_http_gateway(cloud, tmp_path):
    admin, app, _ = cloud
    agent = Agent(Settings(tmp_path), "client")
    requests = []

    async def record(request):
        requests.append(request.url)

    agent.cloud_http = lambda: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), event_hooks={"request": [record]}
    )
    code = admin.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    await agent.enroll("https://testserver", code, "Company client")
    await agent.tick()
    await agent.update_connection("https://testserver")
    await agent.close()
    assert requests
    assert all(
        url.scheme == "https" and url.host == "testserver" and url.port in {None, 443} for url in requests
    )
    assert not any(url.path == "/api/devices/connection" for url in requests)
    assert agent.frp is None
    assert not {"frp_server", "frp_port", "remote_port"} & agent.settings.data.keys()
    assert "frp_secret" not in agent.vault.read()
    assert not (tmp_path / "frpc.toml").exists()
    assert not (tmp_path / "bin").exists()


def test_client_discards_unused_legacy_tunnel_settings(tmp_path):
    settings = Settings(tmp_path)
    settings.data.update(role="client", frp_port=17000, frp_server="unused", remote_port=None)
    settings.save()
    agent = Agent(settings, "client")
    assert agent.frp is None
    assert not {"frp_server", "frp_port", "remote_port"} & Settings(tmp_path).data.keys()
