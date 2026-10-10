import pytest
from fastapi.testclient import TestClient

from mcp_transfer.cli import initialize_cloud
from mcp_transfer.cloud import create_cloud
from mcp_transfer.config import Settings


@pytest.fixture
def cloud(tmp_path):
    settings = Settings(tmp_path / "cloud")
    initialize_cloud(settings, "https://testserver", password="correct-test-password-123")
    app = create_cloud(settings)
    with TestClient(app, base_url="https://testserver") as client:
        response = client.post(
            "/api/auth/login", json={"username": "admin", "password": "correct-test-password-123"}
        )
        assert response.status_code == 200
        client.headers["x-csrf-token"] = response.json()["csrf"]
        yield client, app, settings


def enroll(client, role="client", name="test", owner=None, csr=None):
    response = client.post("/api/enrollment-codes", json={"role": role, "owner": owner})
    assert response.status_code == 200, response.text
    response = client.post(
        "/api/devices/enroll", json={"code": response.json()["code"], "name": name, "csr": csr}
    )
    assert response.status_code == 200, response.text
    return response.json()


def access(client, device):
    response = client.post("/api/token", json={"refresh_token": device["refresh_token"]})
    assert response.status_code == 200, response.text
    device["refresh_token"] = response.json()["refresh_token"]
    return {"authorization": "Bearer " + response.json()["access_token"]}
