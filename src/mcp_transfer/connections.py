"""Independent cloud identities behind one authenticated loopback WebUI."""
from __future__ import annotations

import asyncio
import difflib
import re
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from .config import Settings, validate_cloud_url
from .security import new_id
from .vault import secure_windows_directory


def origin_key(url: str):
    parsed = urlsplit(url)
    return parsed.scheme.lower(), (parsed.hostname or "").lower(), parsed.port or (443 if parsed.scheme == "https" else 80)


class Connections:
    def __init__(self, primary):
        self.primary = primary
        primary.connection_registry = self
        self.agents = {"primary": primary}
        self.lock = asyncio.Lock()
        self.running = False
        for cid in primary.settings.data.get("connections", []):
            if not isinstance(cid, str) or not re.fullmatch(r"conn_[a-f0-9]{24}", cid):
                raise ValueError("Invalid saved cloud connection identifier")
            self.agents[cid] = self.make_agent(cid)

    def make_agent(self, cid):
        from .agent import Agent

        settings = Settings(self.primary.settings.home / "connections" / cid)
        settings.data.update(
            role=self.primary.role, ui_port=self.primary.port, route_prefix="/connections/" + cid,
            disable_frp=self.primary.settings.data.get("disable_frp", False),
            development=self.primary.settings.data.get("development", False),
        )
        if self.primary.role == "host" and not settings.data.get("adapter_port"):
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                settings.data["adapter_port"] = listener.getsockname()[1]
        settings.save()
        agent = Agent(settings, self.primary.role)
        agent.vault.update(local_password=self.primary.vault.read()["local_password"])
        # frpc is already checksum-verified by the installer. Reuse its cached
        # executable; a second cloud connection does not need another download.
        if agent.role == "host":
            for binary in ("frpc.exe", "frpc"):
                source = self.primary.settings.home / "bin" / binary
                destination = settings.home / "bin" / binary
                if source.is_file() and not destination.exists():
                    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copy2(source, destination)
        secure_windows_directory(settings.home)
        return agent

    def summary(self):
        return [{
            "id": cid, "name": agent.settings.data.get("connection_label") or agent.settings.data.get("name") or "默认连接",
            "cloud_url": agent.settings.data.get("cloud_url", ""), "device_id": agent.settings.data.get("device_id"),
            "status": agent.status, "role": agent.role,
        } for cid, agent in self.agents.items()]

    async def add(self, body):
        from .agent import validate_enrollment

        url, code, name, allow_http = validate_enrollment(body, self.primary.settings)
        url = validate_cloud_url(url, bool(self.primary.settings.data.get("development")), allow_http)
        label = body.get("label", "").strip() or url
        if len(label) > 128:
            raise ValueError("连接名称最多 128 个字符")
        async with self.lock:
            if len(self.agents) >= 20:
                raise ValueError("最多可配置 20 个独立云端连接")
            if any(origin_key(a.settings.data.get("cloud_url", "")) == origin_key(url) for a in self.agents.values()):
                raise ValueError("此云端已连接，请选择现有连接；更换登记使用“替换当前登记”")
            cid = new_id("conn")
            agent = self.make_agent(cid)
            agent.settings.data.update(
                connection_label=label, proxy=self.primary.settings.data.get("proxy", ""),
                enterprise_ca=self.primary.settings.data.get("enterprise_ca", ""),
            )
            agent.settings.save()
            try:
                await agent.enroll(url, code, name, bool(agent.settings.data.get("development")), allow_http)
            except Exception:
                await agent.close()
                shutil.rmtree(agent.settings.home)
                raise
            self.agents[cid] = agent
            if agent.role == "host" and body.get("copy_services") is True:
                agent.vault.update(services={new_id("srv"): {**definition, "enabled": False}
                                            for definition in self.primary.vault.read().get("services", {}).values()})
            self.primary.settings.data["connections"] = [key for key in self.agents if key != "primary"]
            self.primary.settings.save()
            if self.running:
                agent.task = asyncio.create_task(agent.run())
            return {"ok": True, "id": cid, "message": "已添加独立云端连接，原有连接继续运行。请在新连接中配置服务或订阅目录。"}

    async def remove(self, cid):
        async with self.lock:
            if cid == "primary":
                raise ValueError("默认连接保留用于兼容已有配置，可替换登记或暂停连接")
            agent = self.agents.get(cid)
            if not agent:
                raise HTTPException(404, "云端连接不存在")
            # Revoke before discarding the only local refresh credential. A
            # failed network request keeps the connection available for retry.
            try:
                await agent.api("POST", "/api/devices/me/disconnect")
            except Exception as error:
                from httpx import HTTPStatusError

                if not isinstance(error, HTTPStatusError) or error.response.status_code not in {401, 403, 404}:
                    raise ValueError("云端撤销失败，连接已保留。请检查网络，或先在云端删除此设备。") from error
            await agent.close()
            self.agents.pop(cid)
            self.primary.settings.data["connections"] = [key for key in self.agents if key != "primary"]
            self.primary.settings.save()
            # Keep host definitions in a private archive; removal should not
            # silently erase user-entered commands and secrets.
            archive = self.primary.settings.home / "removed-connections"
            archive.mkdir(mode=0o700, exist_ok=True)
            agent.settings.home.rename(archive / cid)
            return {"ok": True, "message": "云端设备已撤销，连接已移除。本机配置保留在 removed-connections。"}

    async def start(self):
        self.running = True
        for agent in self.agents.values():
            agent.task = asyncio.create_task(agent.run())

    def export_all(self, target, mode):
        from .exporters import application_paths, generate

        path = Path(application_paths()[target])
        existing = path.read_text(encoding="utf-8-sig") if path.exists() else ""
        content = existing
        for agent in self.agents.values():
            content = generate(
                target, agent.active_mapping_ids(), agent.settings.home,
                agent.vault.read().get("mappings", {}), agent.port, mode, content,
                {item["id"]: item["name"] for item in agent.catalog}, agent.route_prefix,
            )
        diff = "".join(difflib.unified_diff(existing.splitlines(True), content.splitlines(True), fromfile=str(path), tofile=str(path)))
        return {"path": str(path), "before": existing, "content": content, "diff": diff}

    async def close(self):
        self.running = False
        await asyncio.gather(*(agent.close() for agent in list(self.agents.values())))


class ConnectionRouter:
    def __init__(self, connections, sessions):
        self.connections = connections
        self.sessions = sessions
        self.apps = {}

    async def __call__(self, scope, receive, send):
        from .agent import create_agent_app

        cid = scope.get("path_params", {}).get("connection_id")
        agent = self.connections.agents.get(cid) if cid != "primary" else None
        if agent is None:
            await JSONResponse({"detail": "云端连接不存在"}, status_code=404)(scope, receive, send)
            return
        if cid not in self.apps:
            self.apps[cid] = create_agent_app(agent, background=False, session_store=self.sessions)
        await self.apps[cid](scope, receive, send)
