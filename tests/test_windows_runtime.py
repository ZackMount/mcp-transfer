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


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows console attachment test")
async def test_frpc_started_from_pythonw_has_no_visible_console_and_dies_with_parent(tmp_path):
    import asyncio
    import json

    from mcp_transfer.frp import download_frp

    await download_frp(tmp_path, "frpc")
    (tmp_path / "frpc.toml").write_text('serverAddr = "127.0.0.1"\nserverPort = 1\nloginFailExit = false\n', encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import asyncio, sys\nfrom pathlib import Path\nfrom mcp_transfer.config import Settings\nfrom mcp_transfer.frp import FrpProcess\n"
        "async def run():\n    home=Path(sys.argv[1])\n    frp=FrpProcess(Settings(home),'frpc')\n"
        "    await frp.start()\n    (home/'pid.txt').write_text(str(frp.process.pid))\n    await asyncio.sleep(60)\n"
        "asyncio.run(run())\n", encoding="utf-8",
    )
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    process = subprocess.Popen([str(pythonw), str(worker), str(tmp_path)])
    try:
        for _ in range(100):
            if (tmp_path / "pid.txt").exists():
                break
            assert process.poll() is None
            await asyncio.sleep(0.1)
        pid = int((tmp_path / "pid.txt").read_text())
        probe = tmp_path / "console.json"
        program = "import ctypes,json,sys; from pathlib import Path; k=ctypes.WinDLL('kernel32',use_last_error=True); u=ctypes.WinDLL('user32'); k.GetConsoleWindow.restype=ctypes.c_void_p; u.IsWindowVisible.argtypes=[ctypes.c_void_p]; attached=bool(k.AttachConsole(int(sys.argv[1]))); error=ctypes.get_last_error(); window=k.GetConsoleWindow() if attached else None; visible=bool(u.IsWindowVisible(window)) if window else False; Path(sys.argv[2]).write_text(json.dumps({'attached':attached,'error':error,'visible':visible}))"
        checked = subprocess.Popen([str(pythonw), "-c", program, str(pid), str(probe)])
        checked.wait(timeout=10)
        result = json.loads(probe.read_text())
        # CREATE_NO_WINDOW may retain a headless console object. The user-facing
        # requirement is that it has no visible console window.
        assert result["visible"] is False, result
        assert result["attached"] or result["error"] == 6, result
        process.terminate()
        process.wait(timeout=10)
        import win32api
        import win32con
        import win32event

        # Job Object parent exit must stop this frpc rather than leave an orphan.
        try:
            handle = win32api.OpenProcess(win32con.SYNCHRONIZE, False, pid)
        except Exception:
            handle = None
        if handle:
            try:
                assert win32event.WaitForSingleObject(handle, 10000) == win32event.WAIT_OBJECT_0
            finally:
                handle.Close()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
