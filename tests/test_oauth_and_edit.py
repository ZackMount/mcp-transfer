import base64
import hashlib

from conftest import access, enroll
from fastapi.testclient import TestClient

from mcp_transfer.agent import Agent, create_agent_app
from mcp_transfer.config import Settings
from mcp_transfer.pki import make_csr


def test_oauth_pkce_single_use_and_exact_resource(cloud, tmp_path):
    client, _, _ = cloud
    host = enroll(client, "host", csr=make_csr(tmp_path / "keys", "host"))
    client.put(
        "/api/hosts/me/servers/srv_demo",
        headers=access(client, host),
        json={"name": "Demo", "transport": "stdio"},
    )
    verifier = "a" * 64
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    request = {
        "client_id": "mcp-transfer",
        "redirect_uri": "http://127.0.0.1:12345/oauth/callback",
        "resource": "https://testserver/mcp/srv_demo",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": "opaque-state",
    }
    bad = {**request, "redirect_uri": "https://evil.example/callback"}
    assert client.post("/api/oauth/approve", json=bad).status_code == 400
    response = client.post("/api/oauth/approve", json=request)
    assert response.status_code == 200
    from urllib.parse import parse_qs, urlsplit

    params = parse_qs(urlsplit(response.json()["redirect"]).query)
    assert params["state"] == ["opaque-state"]
    exchange = {
        "grant_type": "authorization_code",
        "client_id": "mcp-transfer",
        "code": params["code"][0],
        "redirect_uri": request["redirect_uri"],
        "resource": request["resource"],
        "code_verifier": verifier,
    }
    assert client.post("/oauth/token", data={**exchange, "code_verifier": "wrong"}).status_code == 400
    assert (
        client.post("/oauth/token", data={**exchange, "resource": "https://evil.example"}).status_code == 400
    )
    assert client.post("/oauth/token", data=exchange).status_code == 200
    assert client.post("/oauth/token", data=exchange).status_code == 400


def test_host_edits_keep_secrets_without_exposing_them(tmp_path):
    agent = Agent(Settings(tmp_path), "host")
    agent.vault.update(
        services={
            "srv_a": {
                "name": "Old",
                "transport": "stdio",
                "command": "python",
                "args": [],
                "env": {"API_KEY": "private"},
                "enabled": True,
            }
        }
    )
    with TestClient(create_agent_app(agent, background=False), base_url="http://127.0.0.1:17810") as client:
        response = client.post("/api/auth/login", json={"password": agent.vault.read()["local_password"]})
        assert "mcpt_host_17810" in response.headers["set-cookie"]
        client.headers["x-csrf-token"] = response.json()["csrf"]
        assert "private" not in client.get("/api/servers").text
        response = client.put("/api/servers/srv_a", json={"name": "New"})
        assert response.status_code == 200
        definition = agent.vault.read()["services"]["srv_a"]
        assert definition["name"] == "New"
        assert definition["env"] == {"API_KEY": "private"}
