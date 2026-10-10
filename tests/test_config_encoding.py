import json
import os
import subprocess
import sys

import pytest

from mcp_transfer.config import Settings
from mcp_transfer.vault import Vault


@pytest.mark.parametrize("bom", [False, True])
def test_unicode_settings_are_read_with_utf8_mode_disabled(tmp_path, bom):
    home = tmp_path / "profile"
    settings = Settings(home)
    settings.data = {"role": "client", "name": "我的电脑🌍", "path": "C:\\用户\\资料"}
    settings.save()
    if bom:
        settings.path.write_bytes(b"\xef\xbb\xbf" + settings.path.read_bytes())
    env = dict(os.environ, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C")
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-c",
         "import json,sys; from pathlib import Path; from mcp_transfer.config import Settings; "
         "print(json.dumps(Settings(Path(sys.argv[1])).data, ensure_ascii=True))", str(home)],
        env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == settings.data


def test_local_password_command_reads_unicode_profile_under_legacy_locale(tmp_path):
    settings = Settings(tmp_path)
    settings.data = {"role": "client", "name": "我的工作电脑🌍"}
    settings.save()
    Vault(tmp_path).update(local_password="test-local-access-password")
    result = subprocess.run(
        [sys.executable, "-X", "utf8=0", "-m", "mcp_transfer", "--home", str(tmp_path), "local-password"],
        env=dict(os.environ, PYTHONUTF8="0", PYTHONCOERCECLOCALE="0", LC_ALL="C"),
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "test-local-access-password"
