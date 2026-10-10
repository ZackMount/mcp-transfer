import os
import socket
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import inspect

from mcp_transfer.cli import run_agent
from mcp_transfer.config import Settings
from mcp_transfer.database import database
from mcp_transfer.runtime_lock import profile_lock


def test_profile_lock_rejects_another_process_and_is_released(tmp_path):
    code = "from pathlib import Path; from mcp_transfer.runtime_lock import profile_lock; " \
           "lock=profile_lock(Path(__import__('sys').argv[1])); lock.__enter__()"
    with profile_lock(tmp_path):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
        assert result.returncode != 0
        assert "already running" in result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


async def test_occupied_port_is_rejected_before_agent_creation(tmp_path, monkeypatch):
    settings = Settings(tmp_path)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        settings.data["ui_port"] = listener.getsockname()[1]

        def unwanted_start(*args, **kwargs):
            raise AssertionError("An occupied port must not start background credential refresh")

        monkeypatch.setattr("mcp_transfer.cli.Agent", unwanted_start)
        with pytest.raises(ValueError, match="occupied"):
            await run_agent(settings, "client")


def test_existing_database_gets_the_retry_column_without_losing_history(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE consumed_refresh_tokens (hash VARCHAR(64) PRIMARY KEY, device_id VARCHAR(64), expires FLOAT)")
        db.execute("INSERT INTO consumed_refresh_tokens VALUES ('old-hash','d_old',9999999999)")
    factory = database(f"sqlite:///{path}")
    assert "request_hash" in {column["name"] for column in inspect(factory.kw["bind"]).get_columns("consumed_refresh_tokens")}
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT hash,device_id,request_hash FROM consumed_refresh_tokens").fetchone() == ("old-hash", "d_old", None)
    factory.kw["bind"].dispose()


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows launcher/process lifecycle test")
def test_windows_cleanup_stops_only_this_profile_agent_and_preserves_bridges(tmp_path):
    import time
    from pathlib import Path

    program = tmp_path / "mcp_transfer_test.py"
    program.write_text("import time; time.sleep(30)", encoding="utf-8")
    process = subprocess.Popen([sys.executable, str(program), "--home", str(tmp_path), "host"])
    bridge = subprocess.Popen([sys.executable, str(program), "--home", str(tmp_path), "connect", "--server", "srv_demo"])
    other = subprocess.Popen([sys.executable, str(program), "--home", str(tmp_path / "other"), "host"])
    try:
        time.sleep(0.3)
        runner = tmp_path / "stop.ps1"
        runner.write_text(
            "param([string]$Utility,[string]$Profile)\n. $Utility\n"
            "Stop-McpAgent -Role host -DataRoot $Profile -SkipScheduledTask\n",
            encoding="utf-8",
        )
        subprocess.run(["powershell", "-NoProfile", "-File", str(runner),
                        str(Path(__file__).parents[1] / "scripts/windows-runtime.ps1"), str(tmp_path)], check=True)
        process.wait(timeout=10)
        assert bridge.poll() is None
        assert other.poll() is None
    finally:
        for child in (process, bridge, other):
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
