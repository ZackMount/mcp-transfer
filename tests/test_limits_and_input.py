import pytest
from conftest import access, enroll
from fastapi import HTTPException

from mcp_transfer.limits import RequestLimits


def test_limits_are_live_persisted_and_validate_ranges(cloud):
    admin, app, settings = cloud
    assert admin.get("/api/settings/limits").json() == {"request_concurrency": 32, "stream_concurrency": 128}
    for value in [-1, 4097, True, "32", None]:
        assert admin.put("/api/settings/limits", json={"request_concurrency": value, "stream_concurrency": 5}).status_code == 400
    assert admin.put("/api/settings/limits", json={"request_concurrency": 1, "stream_concurrency": 2}).status_code == 200
    limiter = app.state.limits
    request = limiter.acquire("device", "POST")
    stream = limiter.acquire("device", "GET")
    with pytest.raises(HTTPException) as rejected:
        limiter.acquire("device", "POST")
    assert rejected.value.status_code == 429 and rejected.value.headers["Retry-After"] == "1"
    second = limiter.acquire("device", "GET")
    assert admin.put("/api/settings/limits", json={"request_concurrency": 0, "stream_concurrency": 0}).status_code == 200
    release = limiter.acquire("device", "POST")
    release()
    release()
    request()
    stream()
    second()
    assert not limiter.active
    assert RequestLimits(settings).settings.data["limits"]["request_concurrency"] == 0


@pytest.mark.parametrize("body", ["{", "[]", '"text"', "null"])
def test_malformed_api_body_is_rejected_as_a_client_error(cloud, body):
    admin, _, _ = cloud
    response = admin.post("/api/enrollment-codes", content=body, headers={"content-type": "application/json"})
    assert response.status_code == 400


def test_invalid_connection_id_does_not_consume_quota_and_upstream_failure_releases_it(cloud, tmp_path):
    from mcp_transfer.pki import make_csr

    admin, app, _ = cloud
    host = enroll(admin, "host", csr=make_csr(tmp_path, "host"))
    admin.put("/api/hosts/me/servers/srv_limit", headers=access(admin, host), json={"name": "Limit", "transport": "stdio"})
    device = enroll(admin)
    token = admin.post("/api/access-token", headers=access(admin, device), json={"server_id": "srv_limit"}).json()["access_token"]
    headers = {"authorization": "Bearer " + token, "x-mcpt-connection": "invalid!"}
    assert admin.post("/mcp/srv_limit", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=headers).status_code == 400
    assert not app.state.limits.active
    headers["x-mcpt-connection"] = "valid"
    assert admin.post("/mcp/srv_limit", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=headers).status_code == 503
    assert not app.state.limits.active
