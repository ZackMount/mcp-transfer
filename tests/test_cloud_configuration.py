import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import httpx
import pytest
from conftest import enroll
from cryptography import x509
from fastapi.testclient import TestClient
from sqlalchemy import select

from mcp_transfer.agent import Agent
from mcp_transfer.cli import initialize_cloud
from mcp_transfer.cloud import create_cloud
from mcp_transfer.config import Settings, validate_cloud_url
from mcp_transfer.database import User


@pytest.mark.parametrize(
    "url", ["http://203.0.113.10:18080", "http://cloud.example:18080", "http://192.168.1.2:18080"]
)
def test_public_http_requires_explicit_opt_in(url):
    with pytest.raises(ValueError, match="allow-insecure-http"):
        validate_cloud_url(url)
    with pytest.raises(ValueError):
        validate_cloud_url(url, development=True)
    assert validate_cloud_url(url, allow_insecure_http=True) == url
    assert validate_cloud_url("https://cloud.example:18443") == "https://cloud.example:18443"


@pytest.mark.parametrize(
    "url",
    [
        "http://host:0",
        "http://host:65536",
        "http://host:abc",
        "http://u:p@host",
        "http://host/mcp",
        "http://host\n.evil",
        "http://host\\evil",
    ],
)
def test_http_opt_in_does_not_disable_url_validation(url):
    with pytest.raises(ValueError):
        validate_cloud_url(url, allow_insecure_http=True)


async def test_manually_edited_http_profile_is_rejected_before_sending_credentials(tmp_path):
    settings = Settings(tmp_path / "client")
    settings.data["cloud_url"] = "http://203.0.113.10:18080"
    agent = Agent(settings, "client")
    agent.vault.update(refresh_token="must-not-be-transmitted")
    with pytest.raises(ValueError, match="allow-insecure-http"):
        await agent.cloud_access()


def test_origin_migration_preserves_identity_and_invalidates_browser_sessions(cloud):
    client, old_app, settings = cloud
    device = enroll(client)
    old_key = (settings.home / "signing.key").read_bytes()
    old_ca = (settings.home / "pki/ca.crt").read_bytes()
    new_url = "http://203.0.113.10:18080"
    with pytest.raises(ValueError, match="migrate-origin"):
        initialize_cloud(settings, new_url, allow_insecure_http=True)
    initialize_cloud(settings, new_url, allow_insecure_http=True, frp_port=17000, migrate_origin=True)
    assert (settings.home / "signing.key").read_bytes() == old_key
    assert (settings.home / "pki/ca.crt").read_bytes() == old_ca
    assert client.get("/api/me").status_code == 401
    with old_app.state.db() as session:
        assert len(session.scalars(select(User)).all()) == 1
    with TestClient(create_cloud(settings), base_url=new_url) as migrated:
        response = migrated.post("/api/token", json={"refresh_token": device["refresh_token"]})
        assert response.status_code == 200
        token = response.json()["access_token"]
        result = migrated.get("/api/devices/connection", headers={"authorization": "Bearer " + token})
        assert result.json()["frp_port"] == 17000
        assert result.json()["device_id"] == device["device_id"]
        login = migrated.post(
            "/api/auth/login", json={"username": "admin", "password": "correct-test-password-123"}
        )
        assert login.status_code == 200
        assert "secure" not in login.headers["set-cookie"].lower()
        assert "httponly" in login.headers["set-cookie"].lower()
    certificate = x509.load_pem_x509_certificate((settings.home / "pki/frps.crt").read_bytes())
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "203.0.113.10" in {str(name.value) for name in names}
    frps = tomllib.loads((settings.home / "frps.toml").read_text())
    assert frps["bindPort"] == 17000
    assert frps["proxyBindAddr"] == "127.0.0.1"
    assert frps["transport"]["tls"]["force"] is True


async def test_host_changes_address_and_frp_port_without_reenrollment(cloud, tmp_path):
    admin, old_app, settings = cloud
    agent_settings = Settings(tmp_path / "host")
    agent_settings.data["disable_frp"] = True
    agent = Agent(agent_settings, "host")
    agent.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=old_app))
    code = admin.post("/api/enrollment-codes", json={"role": "host"}).json()["code"]
    await agent.enroll("https://testserver", code, "Original host")
    did = agent.settings.data["device_id"]
    secret = agent.vault.read()["frp_secret"]
    agent.vault.update(
        services={
            "srv_preserved": {"name": "Preserved", "transport": "stdio", "command": "python", "args": []}
        }
    )
    new_url = "http://203.0.113.10:18080"
    initialize_cloud(settings, new_url, allow_insecure_http=True, frp_port=17000, migrate_origin=True)
    new_app = create_cloud(settings)
    agent.cloud_http = lambda: httpx.AsyncClient(transport=httpx.ASGITransport(app=new_app))
    await agent.update_connection(new_url, allow_insecure_http=True)
    assert agent.settings.data["device_id"] == did
    assert agent.settings.data["frp_port"] == 17000
    assert "srv_preserved" in agent.vault.read()["services"]
    assert agent.vault.read()["frp_secret"] == secret
    config = tomllib.loads((agent.settings.home / "frpc.toml").read_text())
    assert config["serverPort"] == 17000
    assert config["auth"]["oidc"]["tokenEndpointURL"] == new_url + "/oauth/token"
    assert config["transport"]["tls"]["enable"] is True
    # Changing only the frp port is automatically picked up on a normal heartbeat.
    settings.data["frp_port"] = 17001
    await agent.tick()
    assert agent.settings.data["frp_port"] == 17001
    await agent.close()


@pytest.mark.skipif(os.name == "nt", reason="Linux installer uses bash")
def test_installer_handles_legacy_env_and_requires_migration_consent(tmp_path):
    source = Path(__file__).parents[1] / "scripts/install-cloud.sh"
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    target = scripts / "install-cloud.sh"
    shutil.copyfile(source, target)
    base = [
        "bash",
        str(target),
        "--public-url",
        "http://203.0.113.10:18080",
        "--frp-port",
        "17000",
        "--print-config",
    ]
    result = subprocess.run(base, capture_output=True, text=True)
    assert result.returncode != 0 and "--allow-insecure-http" in result.stderr
    result = subprocess.run([*base, "--allow-insecure-http"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "MCPT_FRP_PORT=17000" in result.stdout
    (tmp_path / ".env").write_text("MCPT_DOMAIN=old.example\nMCPT_DB_PASSWORD=preserve-me\n")
    result = subprocess.run([*base, "--allow-insecure-http"], capture_output=True, text=True)
    assert result.returncode != 0 and "--migrate-origin" in result.stderr
    result = subprocess.run(
        [*base, "--allow-insecure-http", "--migrate-origin"], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "preserve-me" not in result.stdout
    assert "MCPT_DOMAIN=old.example" in (tmp_path / ".env").read_text()
    for port in ("17800", "17900", "15432", "21000", "18080", "65536"):
        result = subprocess.run(
            [
                "bash",
                str(target),
                "--public-url",
                "http://203.0.113.10:18080",
                "--frp-port",
                port,
                "--allow-insecure-http",
                "--migrate-origin",
                "--print-config",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
