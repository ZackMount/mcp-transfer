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


def validate_cloud_url(value: str, development: bool = False, allow_insecure_http: bool = False) -> str:
    if not value or any(character.isspace() or ord(character) < 32 for character in value) or "\\" in value:
        raise ValueError("Cloud URL cannot contain whitespace, control characters or backslashes")
    url = urlsplit(value)
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Cloud port must be between 1 and 65535")
    local = url.hostname in {"127.0.0.1", "localhost", "::1"}
    if url.username or url.password or url.query or url.fragment or url.path not in {"", "/"}:
        raise ValueError("Cloud URL must be an origin, without credentials, path or query")
    http_allowed = url.scheme == "http" and (allow_insecure_http or (development and local))
    if not url.hostname or (url.scheme != "https" and not http_allowed):
        raise ValueError("HTTP requires explicit --allow-insecure-http consent; otherwise use HTTPS")
    return value.rstrip("/")


class Settings:
    def __init__(self, home: Path | None = None):
        self.home = home or default_home()
        self.path = self.home / "settings.json"
        self.data = json.loads(self.path.read_text(encoding="utf-8-sig")) if self.path.exists() else {}

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

    @property
    def frp_port(self) -> int:
        value = int(self.data.get("frp_port", 7000))
        if not 1 <= value <= 65535:
            raise ValueError("frp port must be between 1 and 65535")
        return value
