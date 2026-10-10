from __future__ import annotations

import difflib
import json
import os
import re
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

import tomlkit

from .config import Settings, atomic_write

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
    server_names: dict[str, str] | None = None,
    route_prefix: str = "",
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
    if not hasattr(group, "items"):
        raise ValueError("MCP server configuration must be an object")
    def export_name(sid):
        label = re.sub(r"[^a-zA-Z0-9_-]+", "-", (server_names or {}).get(sid, ""))[:48].strip("-_")
        connection = route_prefix.rsplit("/", 1)[-1] if route_prefix else ""
        return "mcpt_" + "_".join(part for part in (label, connection, sid) if part)

    def owned_bridge(item):
        if not hasattr(item, "get"):
            return False
        args = item.get("args", [])
        if not isinstance(args, list) or args[:2] != ["-m", "mcp_transfer"] or "connect" not in args:
            return False
        try:
            return Path(args[args.index("--home") + 1]) == home
        except (ValueError, IndexError, TypeError):
            return False

    desired_names = {export_name(sid) for sid in server_ids}
    # Only remove entries that actually launch our bridge, never arbitrary similarly named entries.
    for key in list(group):
        item = group[key]
        ours = (
            key.startswith("mcpt_")
            and owned_bridge(item)
        )
        if key.startswith("mcpt_") and hasattr(item, "get"):
            url = item.get("url", "")
            match = re.fullmatch(
                rf"http://127\.0\.0\.1:{port}{re.escape(route_prefix)}/mcp/(srv_[a-zA-Z0-9_-]+)", url
            ) if isinstance(url, str) else None
            ours = ours or bool(match and key.endswith("_" + match.group(1)))
        if ours and key not in desired_names:
            del group[key]
    for sid in server_ids:
        name = export_name(sid)
        if mode == "stdio":
            executable = sys.executable
            if Path(executable).name.lower() == "pythonw.exe":
                executable = str(Path(executable).with_name("python.exe"))
            entry = {
                "command": executable,
                "args": ["-m", "mcp_transfer", "--home", str(home), "connect", "--server", sid],
            }
            if target == "vscode":
                entry["type"] = "stdio"
        else:
            entry = {"url": f"http://127.0.0.1:{port}{route_prefix}/mcp/{sid}"}
            entry["http_headers" if target == "codex" else "headers"] = {
                "Authorization": "Bearer " + mappings[sid]["token"]
            }
            if target == "vscode":
                entry["type"] = "http"
        if name in group:
            current = group[name]
            ours = owned_bridge(current)
            local_url = current.get("url", "") if hasattr(current, "get") else ""
            ours = ours or local_url == f"http://127.0.0.1:{port}{route_prefix}/mcp/{sid}"
            if not ours:
                raise ValueError(f"Existing entry {name} is not owned by this platform")
        group[name] = entry
    return (
        tomlkit.dumps(document)
        if target == "codex"
        else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    )


def export_preview(
    target: str, server_ids: list[str], home: Path, mappings: dict, port: int, mode: str,
    server_names: dict[str, str] | None = None,
    route_prefix: str = "",
):
    path = Path(application_paths()[target])
    existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    content = generate(target, server_ids, home, mappings, port, mode, existing, server_names, route_prefix)
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


def clean_exports(profile_root: Path):
    """Remove only this installation's entries, preserving all other settings."""
    root = profile_root.resolve()
    profiles = [Settings(root)]
    profiles.extend(Settings(path.parent) for path in (root / "connections").glob("conn_*/settings.json"))
    profiles.extend(Settings(path.parent) for path in (root / "removed-connections").glob("conn_*/settings.json"))
    urls = {(int(profile.data.get("ui_port", 17820)), profile.data.get("route_prefix", "")) for profile in profiles}
    results = []
    for target, filename in application_paths().items():
        path = Path(filename)
        if not path.exists():
            continue
        try:
            existing = path.read_text(encoding="utf-8-sig")
            document = tomlkit.parse(existing) if target == "codex" else json.loads(existing)
            group = document.get("mcp_servers" if target == "codex" else "servers" if target == "vscode" else "mcpServers", {})
            if not hasattr(group, "items"):
                raise ValueError("MCP entries are not an object")
            removed = 0
            for key, item in list(group.items()):
                if not key.startswith("mcpt_") or not hasattr(item, "get"):
                    continue
                args = item.get("args", [])
                owned = False
                if isinstance(args, list) and args[:2] == ["-m", "mcp_transfer"] and "connect" in args:
                    try:
                        home = Path(args[args.index("--home") + 1]).resolve()
                        owned = home == root or home.is_relative_to(root / "connections") or home.is_relative_to(root / "removed-connections")
                    except (ValueError, TypeError, IndexError):
                        pass
                url = item.get("url", "")
                if isinstance(url, str):
                    for port, prefix in urls:
                        match = re.fullmatch(rf"http://127\.0\.0\.1:{port}{re.escape(prefix)}/mcp/(srv_[a-zA-Z0-9_-]+)", url)
                        owned = owned or bool(match and key.endswith("_" + match.group(1)))
                if owned:
                    del group[key]
                    removed += 1
            if removed:
                content = tomlkit.dumps(document) if target == "codex" else json.dumps(document, ensure_ascii=False, indent=2) + "\n"
                written = write_export({"path": filename, "before": existing, "content": content})
                results.append({"target": target, "removed": removed, **written})
        except (ValueError, OSError) as error:
            raise ValueError(f"Cannot safely clean {filename}: {error}. Original retained; merge manually before purging.") from error
    return results
