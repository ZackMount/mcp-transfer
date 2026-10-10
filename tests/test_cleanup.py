import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from mcp_transfer.config import Settings
from mcp_transfer.exporters import clean_exports, generate


@pytest.mark.parametrize("target", ["cursor", "codex"])
@pytest.mark.parametrize("mode", ["stdio", "http"])
def test_cleanup_removes_only_owned_primary_and_extra_cloud_entries(tmp_path, monkeypatch, target, mode):
    root = tmp_path / "installation"
    child = root / "connections" / "conn_test"
    settings = Settings(child)
    settings.data.update(ui_port=17820, route_prefix="/connections/conn_test")
    settings.save()
    mappings = {"srv_a": {"token": "local"}}
    content = generate(target, ["srv_a"], root, mappings, 17820, mode)
    content = generate(target, ["srv_a"], child, mappings, 17820, mode, content, route_prefix="/connections/conn_test")
    content = generate(target, ["srv_external"], tmp_path / "other-install", {"srv_external": {"token": "other"}}, 17920, mode, content)
    path = tmp_path / ("config.toml" if target == "codex" else "config.json")
    path.write_text(content, encoding="utf-8")
    monkeypatch.setattr("mcp_transfer.exporters.application_paths", lambda: {target: str(path)})
    result = clean_exports(root)
    assert result[0]["removed"] == 2
    cleaned = path.read_text(encoding="utf-8")
    assert "mcpt_srv_external" in cleaned
    assert "mcpt_srv_a" not in cleaned and "mcpt_conn_test_srv_a" not in cleaned
    assert Path(result[0]["backup"]).read_text(encoding="utf-8") == content


def test_cleanup_preserves_jsonc_for_manual_merge(tmp_path, monkeypatch):
    path = tmp_path / "mcp.json"
    original = '{ // user comments\n"mcpServers": {} }'
    path.write_text(original)
    monkeypatch.setattr("mcp_transfer.exporters.application_paths", lambda: {"cursor": str(path)})
    with pytest.raises(ValueError, match="Original retained"):
        clean_exports(tmp_path / "installation")
    assert path.read_text() == original


@pytest.mark.skipif(os.name == "nt", reason="Linux shell validation")
def test_cleaner_rejects_an_unrelated_directory_before_any_deletion(tmp_path):
    path = Path(__file__).parents[1] / "scripts/uninstall-cloud.sh"
    command = ["bash", str(path), "--install-dir", str(tmp_path), "--purge", "--yes"]
    # Root is checked first; either way the unrelated file must survive.
    unrelated = tmp_path / "keep.json"
    unrelated.write_text(json.dumps({"important": True}))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0
    assert unrelated.exists()


@pytest.mark.skipif(os.name == "nt", reason="Linux shell validation")
def test_shell_scripts_parse():
    scripts = Path(__file__).parents[1] / "scripts"
    for script in scripts.glob("*.sh"):
        result = subprocess.run([shutil.which("bash"), "-n", str(script)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
