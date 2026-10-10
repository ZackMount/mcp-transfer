import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest


@pytest.mark.skipif(os.name != "nt", reason="Actual pythonw runtime requires Windows")
def test_background_pythonw_serves_authenticated_webui(tmp_path):
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    assert pythonw.exists()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [str(pythonw), "-m", "mcp_transfer", "--home", str(tmp_path), "client", "--port", str(port)]
    )
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=2) as client:
            for _ in range(60):
                try:
                    response = client.get("/healthz")
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    raise AssertionError((tmp_path / "agent.log").read_text())
                time.sleep(0.25)
            else:
                raise AssertionError("pythonw did not start the WebUI")
            assert response.json()["role"] == "client"
            assert client.get("/api/status").status_code == 401
            from mcp_transfer.vault import Vault

            password = Vault(tmp_path).read()["local_password"]
            assert client.post("/api/auth/login", json={"password": password}).status_code == 200
            assert client.get("/api/status").status_code == 200
    finally:
        process.terminate()
        process.wait(timeout=10)
