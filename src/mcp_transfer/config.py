from __future__ import annotations

import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def default_home() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".local" / "share")))
    return Path(os.environ.get("MCPT_HOME", base / "mcp-transfer"))


def atomic_write(path: Path, content: str | bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".mcpt-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content.encode() if isinstance(content, str) else content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def validate_cloud_url(value: str, development: bool = False) -> str:
    url = urlsplit(value)
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("Cloud URL must be an origin, without credentials, path or query")
    if not url.hostname or (url.scheme != "https" and not (development and local and url.scheme == "http")):
        raise ValueError("Cloud URL must use HTTPS (localhost HTTP requires --development)")
    return value.rstrip("/")


class Settings:
    def __init__(self, home: Path | None = None):
        self.home = home or default_home()
        self.path = self.home / "settings.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def save(self):
        atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=2))

    def secret(self, name: str) -> str:
        if not self.data.get(name):
            self.data[name] = secrets.token_urlsafe(32)
            self.save()
        return self.data[name]

    @property
    def cloud_url(self) -> str:
        return os.environ.get("MCPT_PUBLIC_URL", self.data.get("cloud_url", "http://127.0.0.1:17800"))

    @property
    def database_url(self) -> str:
        return os.environ.get("DATABASE_URL", f"sqlite:///{self.home / 'cloud.db'}")
