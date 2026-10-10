import time

import jwt
from conftest import access, enroll
from fastapi.testclient import TestClient

from mcp_transfer.cloud import create_frp_plugin
from mcp_transfer.pki import make_csr


def test_anonymous_and_origin_boundaries(cloud):
    client, app, _ = cloud
    with TestClient(app, base_url="https://testserver") as anonymous:
        assert anonymous.get("/api/devices").status_code == 401
        response = anonymous.post("/mcp/srv_test", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401
        assert "resource_metadata" in response.headers["www-authenticate"]
    assert client.get("/api/me", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/me", headers={"host": "evil.example"}).status_code == 403
    assert (
        client.post(
            "/api/enrollment-codes", headers={"x-csrf-token": "wrong"}, json={"role": "client"}
        ).status_code
        == 403
    )


def test_invite_single_use_and_roles(cloud):
    client, _, _ = cloud
    code = client.post("/api/enrollment-codes", json={"role": "client"}).json()["code"]
    body = {"code": code, "name": "Laptop"}
    enrolled = client.post("/api/devices/enroll", json=body)
    assert enrolled.status_code == 200
    assert client.post("/api/devices/enroll", json=body).status_code == 401
    headers = access(client, enrolled.json())
    assert (
        client.put(
            "/api/hosts/me/servers/srv_fake", headers=headers, json={"name": "Bad", "transport": "http"}
        ).status_code
        == 403
    )


def test_refresh_rotation_reuse_revokes_device(cloud):
    client, _, _ = cloud
    device = enroll(client)
    first = device["refresh_token"]
    headers = access(client, device)
    assert client.get("/api/catalog", headers=headers).status_code == 200
    assert client.post("/api/token", json={"refresh_token": first}).status_code == 401
    assert client.get("/api/catalog", headers=headers).status_code == 401


def test_revocation_invalidates_existing_access_token(cloud):
    client, _, _ = cloud
    device = enroll(client)
    headers = access(client, device)
    assert client.get("/api/catalog", headers=headers).status_code == 200
    assert client.post(f"/api/devices/{device['device_id']}/revoke").status_code == 200
    assert client.get("/api/catalog", headers=headers).status_code == 401


def test_cross_user_catalog_and_tool_acl(cloud, tmp_path):
    client, app, _ = cloud
    host = enroll(client, "host", csr=make_csr(tmp_path / "keys", "host"))
    host_headers = access(client, host)
    assert (
        client.put(
            "/api/hosts/me/servers/srv_files",
            headers=host_headers,
            json={"name": "Files", "transport": "stdio"},
        ).status_code
        == 200
    )
    user = client.post("/api/users", json={"username": "member", "password": "long-enough-password"}).json()[
        "id"
    ]
    device = enroll(client, owner=user)
    headers = access(client, device)
    assert client.get("/api/catalog", headers=headers).json()["servers"] == []
    grant = client.post(
        "/api/grants", json={"user_id": user, "server_id": "srv_files", "policy": {"tools": ["read"]}}
    )
    assert grant.status_code == 200
    assert len(client.get("/api/catalog", headers=headers).json()["servers"]) == 1
    token = client.post("/api/access-token", headers=headers, json={"server_id": "srv_files"}).json()[
        "access_token"
    ]
    response = client.post(
        "/mcp/srv_files",
        headers={"authorization": "Bearer " + token},
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "write", "arguments": {}},
        },
    )
    assert response.status_code == 403
    assert client.delete("/api/grants/" + grant.json()["id"]).status_code == 200
    response = client.post(
        "/mcp/srv_files",
        headers={"authorization": "Bearer " + token},
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    assert response.status_code == 403
    # Session identifiers cannot substitute for authentication.
    assert client.get("/mcp/srv_files", headers={"mcp-session-id": "stolen"}).status_code == 401


def test_wrong_issuer_audience_and_algorithm(cloud):
    client, app, _ = cloud
    device = enroll(client)
    signer = app.state.signer
    token = signer.issue(device["device_id"], "wrong-audience")
    assert client.get("/api/catalog", headers={"authorization": "Bearer " + token}).status_code == 401
    payload = {
        "sub": device["device_id"],
        "aud": "cloud-api",
        "iss": "https://evil.example",
        "exp": int(time.time()) + 300,
        "iat": int(time.time()),
        "nbf": int(time.time()),
        "jti": "bad",
    }
    token = jwt.encode(payload, signer.private, algorithm="RS256")
    assert client.get("/api/catalog", headers={"authorization": "Bearer " + token}).status_code == 401
    token = jwt.encode({**payload, "iss": "https://testserver"}, "attacker-secret-which-is-longer-than-32-bytes", algorithm="HS256")
    assert client.get("/api/catalog", headers={"authorization": "Bearer " + token}).status_code == 401


def test_frp_cannot_claim_another_host_port(cloud, tmp_path):
    client, app, settings = cloud
    host = enroll(client, "host", csr=make_csr(tmp_path / "keys", "host"))
    token = client.post(
        "/oauth/token",
        data={
            "grant_type": "client_credentials",
            "client_id": host["device_id"],
            "client_secret": host["frp_secret"],
        },
    )
    assert token.status_code == 200, token.text
    with TestClient(create_frp_plugin(settings)) as plugin:
        identity = host["device_id"]
        assert (
            plugin.post(
                "/internal/frp?op=Login",
                json={"content": {"user": identity, "privilege_key": token.json()["access_token"]}},
            ).json()["reject"]
            is False
        )
        content = {
            "user": {"user": identity},
            "proxy_type": "tcp",
            "proxy_name": identity + ".adapter",
            "remote_port": host["remote_port"],
        }
        assert plugin.post("/internal/frp?op=NewProxy", json={"content": content}).json()["reject"] is False
        content["remote_port"] += 1
        assert plugin.post("/internal/frp?op=NewProxy", json={"content": content}).json()["reject"] is True
        content["remote_port"] -= 1
        content["proxy_type"] = "http"
        assert plugin.post("/internal/frp?op=NewProxy", json={"content": content}).json()["reject"] is True
        assert client.post(f"/api/devices/{identity}/revoke").status_code == 200
        assert (
            plugin.post(
                "/internal/frp?op=Login",
                json={"content": {"user": identity, "privilege_key": token.json()["access_token"]}},
            ).json()["reject"]
            is True
        )
    # Plugin is deliberately absent from the public router.
    assert client.post("/internal/frp?op=Login", json={}).status_code == 404
