from __future__ import annotations

import difflib
import json
import os
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import tomlkit

from .config import atomic_write

TARGETS = {"generic", "cursor", "vscode", "claude", "codex"}


def application_paths() -> dict[str, str]:
    home = Path.home()
    appdata = Path(os.environ.get("APPDATA", str(home / ".config")))
    return {
        "generic": str(home / "mcp-transfer.json"),
        "cursor": str(home / ".cursor" / "mcp.json"),
        "vscode": str(appdata / "Code" / "User" / "mcp.json")
        if sys.platform == "win32"
        else str(home / ".config" / "Code" / "User" / "mcp.json"),
        "claude": str(appdata / "Claude" / "claude_desktop_config.json")
        if sys.platform == "win32"
        else str(home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"),
        "codex": str(home / ".codex" / "config.toml"),
    }


def generate(
    target: str,
    server_ids: list[str],
    home: Path,
    mappings: dict,
    port: int,
    mode: str = "stdio",
    existing: str = "",
) -> str:
    if target not in TARGETS or mode not in {"stdio", "http"}:
        raise ValueError("Unknown export format or mode")
    if target == "claude" and mode != "stdio":
        raise ValueError("Use stdio for the Claude Desktop local configuration")
    if target == "codex":
        document = tomlkit.parse(existing) if existing.strip() else tomlkit.document()
        group = document.setdefault("mcp_servers", tomlkit.table())
    else:
        try:
            document = json.loads(existing) if existing.strip() else {}
        except ValueError as error:
            raise ValueError(
                "Existing file contains JSONC or invalid JSON. Download the generated config and merge it manually; the original will not be overwritten."
            ) from error
        if not isinstance(document, dict):
            raise ValueError("Configuration root must be an object")
        group = document.setdefault("servers" if target == "vscode" else "mcpServers", {})
    desired_names = {"mcpt_" + sid for sid in server_ids}
    # Only remove entries that actually launch our bridge, never arbitrary similarly named entries.
    for key in list(group):
        item = group[key]
        ours = (
            isinstance(item, dict)
            and item.get("args", [])[:2] == ["-m", "mcp_transfer"]
            and "connect" in item.get("args", [])
        )
        if ours and key not in desired_names:
            del group[key]
    for sid in server_ids:
        name = "mcpt_" + sid
        if mode == "stdio":
            entry = {
                "command": sys.executable,
                "args": ["-m", "mcp_transfer", "--home", str(home), "connect", "--server", sid],
            }
            if target == "vscode":
                entry["type"] = "stdio"
        else:
            entry = {"url": f"http://127.0.0.1:{port}/mcp/{sid}"}
            entry["http_headers" if target == "codex" else "headers"] = {
                "Authorization": "Bearer " + mappings[sid]["token"]
            }
            if target == "vscode":
                entry["type"] = "http"
        if name in group:
            current = group[name]
            args = current.get("args", []) if hasattr(current, "get") else []
            ours = args[:2] == ["-m", "mcp_transfer"] and "connect" in args
            local_url = current.get("url", "") if hasattr(current, "get") else ""
            ours = ours or local_url == f"http://127.0.0.1:{port}/mcp/{sid}"
            if not ours:
                raise ValueError(f"Existing entry {name} is not owned by this platform")
        group[name] = entry
    return (
        tomlkit.dumps(document)
        if target == "codex"
        else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def export_preview(target: str, server_ids: list[str], home: Path, mappings: dict, port: int, mode: str):
    path = Path(application_paths()[target])
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    content = generate(target, server_ids, home, mappings, port, mode, existing)
    diff = "".join(
        difflib.unified_diff(
            existing.splitlines(True), content.splitlines(True), fromfile=str(path), tofile=str(path)
        )
    )
    return {"path": str(path), "content": content, "diff": diff, "before": existing}


def write_export(preview: dict):
    path = Path(preview["path"])
    current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if current != preview["before"]:
        raise ValueError("Configuration changed after preview; preview again")
    backup = None
    if path.exists():
        backup = str(path) + ".mcpt-backup-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
    atomic_write(path, preview["content"])
    return {"path": str(path), "backup": backup}
