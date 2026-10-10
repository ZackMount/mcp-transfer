from __future__ import annotations

import asyncio
import json
import logging
import secrets
import ssl
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Settings, atomic_write, validate_cloud_url
from .exporters import TARGETS, application_paths, export_preview, write_export
from .http_support import REQUEST_HEADERS, relay_response
from .pki import make_csr
from .policy import authorize_message, parse_message
from .protocol import Actor, inspect_tools, mapped_client, upstream_client
from .security import (
    BoundaryMiddleware,
    RateLimiter,
    bearer,
    digest,
    limited_body,
    new_id,
    require_csrf,
    verify_token,
)
from .vault import Vault, secure_windows_directory


class Agent:
    def __init__(self, settings: Settings, role: str):
        self.settings = settings
        self.role = role
        self.settings.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.vault = Vault(settings.home)
        if not self.vault.read().get("local_password"):
            self.vault.update(local_password=secrets.token_urlsafe(18))
        self.access = ""
        self.access_expires = 0
        self.refresh_lock = asyncio.Lock()
        self.connection_lock = asyncio.Lock()
        self.resource_tokens = {}
        self.catalog = []
        self.synced_at = 0.0
        self.status = "未连接云端"
        self.tunnel_ready = None
        self.frp = None
        if role == "host":
            from .frp import FrpProcess

            self.frp = FrpProcess(settings, "frpc")
        elif role == "client":
            fields = {"frp_server", "frp_port", "remote_port"} & self.settings.data.keys()
            if fields:
                for field in fields:
                    self.settings.data.pop(field)
                self.settings.save()
        self.actors: dict[tuple, Actor] = {}
        self.used_tickets = {}
        self.published = {}
        self.closed = asyncio.Event()
        self.task = None

    @property
    def port(self):
        return int(self.settings.data.get("ui_port", 17810 if self.role == "host" else 17820))

    @property
    def route_prefix(self):
        return self.settings.data.get("route_prefix", "")

    def cloud_http(self):
        options = {"timeout": httpx.Timeout(20, read=None), "follow_redirects": False}
        if self.settings.data.get("proxy"):
            options["proxy"] = self.settings.data["proxy"]
        ca = self.settings.data.get("enterprise_ca")
        if ca:
            options["verify"] = ssl.create_default_context(cafile=ca)
        return httpx.AsyncClient(**options)

    async def enroll(
        self, cloud_url: str, code: str, name: str, development=False, allow_insecure_http=False,
        replace_existing=False,
    ):
        if self.settings.data.get("device_id") and not replace_existing:
            raise ValueError(
                "Device already enrolled. Revoke it in the cloud and use a new profile to re-enroll."
            )
        cloud_url = validate_cloud_url(cloud_url, development, allow_insecure_http)
        payload = {"code": code, "name": name, "role": self.role}
        with tempfile.TemporaryDirectory(prefix=".enroll-", dir=self.settings.home) as temporary:
            staged = Path(temporary)
            if self.role == "host":
                payload["csr"] = make_csr(staged, "pending-host")
            endpoint = "/api/devices/enroll"
            async with self.cloud_http() as client:
                response = await client.post(cloud_url + endpoint, json=payload, timeout=20)
                response.raise_for_status()
                result = response.json()
            if result["role"] != self.role:
                raise ValueError("Enrollment code role does not match this agent")
            secrets_update = {"refresh_token": result["refresh_token"], "refresh_request_id": "", "refresh_request_hash": ""}
            fields = ["device_id", "role", "signing_public_key"]
            if self.role == "host":
                self.save_host_certificate(staged, result)
                secrets_update["frp_secret"] = result["frp_secret"]
                fields.extend(["remote_port", "frp_server", "frp_port"])
            self.vault.update(**secrets_update)
            self.settings.data.update({field: result[field] for field in fields})
            self.settings.data.update(
                cloud_url=cloud_url, name=name, development=development, allow_insecure_http=allow_insecure_http
            )
            self.settings.save()
            if self.role == "host":
                from .frp import write_frpc

                write_frpc(self.settings, self.vault.read())
        secure_windows_directory(self.settings.home)

    def save_host_certificate(self, staged: Path, result: dict):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization

        cert = x509.load_pem_x509_certificate(result["certificate"].encode())
        ca = x509.load_pem_x509_certificate(result["ca"].encode())
        key = serialization.load_pem_private_key((staged / "device.key").read_bytes(), None)
        encoding, format_ = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        if cert.public_key().public_bytes(encoding, format_) != key.public_key().public_bytes(encoding, format_):
            raise ValueError("Cloud certificate does not match the requested key")
        cert.verify_directly_issued_by(ca)
        atomic_write(self.settings.home / "device.key", (staged / "device.key").read_bytes())
        atomic_write(self.settings.home / "ca.crt", result["ca"])
        atomic_write(self.settings.home / "device.crt", result["certificate"])

    async def reconnect_client(self, cloud_url: str, code: str, name: str, allow_insecure_http=False):
        if self.role != "client":
            raise ValueError("Only client profiles support this recovery operation")
        return await self.reconnect(cloud_url, code, name, allow_insecure_http)

    async def reconnect(self, cloud_url: str, code: str, name: str, allow_insecure_http=False):
        async with self.connection_lock:
            async with self.refresh_lock:
                # A failed code exchange leaves the existing profile intact.
                # No new credentials can race a background refresh during replacement.
                if self.frp is not None:
                    await self.frp.stop()
                try:
                    await self.enroll(
                        cloud_url, code, name, bool(self.settings.data.get("development")),
                        allow_insecure_http, replace_existing=True,
                    )
                except Exception:
                    if self.frp is not None and not self.settings.data.get("disable_frp", False):
                        await self.frp.start()
                    raise
                if self.role == "host":
                    services = self.vault.read().get("services", {})
                    self.vault.update(services={new_id("srv"): definition for definition in services.values()})
                    self.settings.data.pop("pending_deletions", None)
                    self.settings.save()
                else:
                    self.vault.update(mappings={})
                    self.settings.data["subscribe_all"] = True
                    self.settings.save()
                await asyncio.gather(*(actor.close() for actor in list(self.actors.values())))
                self.actors.clear()
                self.access = ""
                self.access_expires = 0
                self.resource_tokens.clear()
                self.catalog = []
                self.synced_at = 0
                self.published.clear()
            try:
                await self._tick()
            except Exception as error:
                self.status = "连接失败：" + type(error).__name__
                return {"ok": True, "synced": False, "message": "新登记已保存，但同步失败。请检查网络后点击立即同步。"}
            if self.role == "host" and not self.settings.data.get("disable_frp", False):
                self.tunnel_ready = False
                for _ in range(8):
                    try:
                        self.tunnel_ready = bool((await self.api("GET", "/api/hosts/me/ready"))["ready"])
                        break
                    except httpx.HTTPError:
                        await asyncio.sleep(1)
                if not self.tunnel_ready:
                    return {"ok": True, "synced": True, "tunnel_ready": False,
                            "message": "已重新登记，但主机隧道尚未就绪。请检查 frpc.log 与云端状态，再检测工具。"}
            return {"ok": True, "synced": True, "tunnel_ready": self.tunnel_ready,
                    "message": "已重新登记并同步，本机 MCP 启动配置已保留，服务目录已重新生成。"}

    async def cloud_access(self):
        async with self.refresh_lock:
            validate_cloud_url(
                self.settings.data.get("cloud_url", ""),
                self.settings.data.get("development", False),
                self.settings.data.get("allow_insecure_http", False),
            )
            if self.access and self.access_expires > time.time() + 30:
                return self.access
            stored = self.vault.read()
            refresh = stored.get("refresh_token")
            if not refresh:
                raise ValueError("Agent is not enrolled")
            request_id = stored.get("refresh_request_id")
            if not request_id or stored.get("refresh_request_hash") != digest(refresh):
                request_id = secrets.token_urlsafe(32)
                self.vault.update(refresh_request_id=request_id, refresh_request_hash=digest(refresh))
            async with self.cloud_http() as client:
                response = await client.post(
                    self.settings.data["cloud_url"] + "/api/token",
                    json={"refresh_token": refresh, "request_id": request_id},
                    timeout=20,
                )
                response.raise_for_status()
                data = response.json()
            claims = verify_token(
                data["access_token"],
                self.settings.data["signing_public_key"],
                self.settings.data["cloud_url"],
                "cloud-api",
            )
            if claims["sub"] != self.settings.data["device_id"] or claims.get("kind") != "device":
                raise ValueError("Cloud returned credentials for a different device")
            # Persist the rotated secret before making another request.
            self.vault.update(refresh_token=data["refresh_token"], refresh_request_id="", refresh_request_hash="")
            self.access = data["access_token"]
            self.access_expires = time.time() + data["expires_in"]
            return self.access

    async def api(self, method: str, path: str, **kwargs):
        token = await self.cloud_access()
        async with self.cloud_http() as client:
            response = await client.request(
                method,
                self.settings.data["cloud_url"] + path,
                headers={"authorization": "Bearer " + token},
                timeout=20,
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

    async def update_connection(self, cloud_url: str, allow_insecure_http=False):
        cloud_url = validate_cloud_url(
            cloud_url, self.settings.data.get("development", False), allow_insecure_http
        )
        async with self.connection_lock:
            # This operation changes the address of the SAME cloud identity.
            # Adding another server uses an independent enrollment instead.
            from cryptography.hazmat.primitives import serialization
            from jwt.algorithms import RSAAlgorithm

            known_key = serialization.load_pem_public_key(self.settings.data["signing_public_key"].encode())
            async with self.cloud_http() as client:
                response = await client.get(cloud_url + "/oauth/jwks", timeout=20)
                response.raise_for_status()
                keys = response.json().get("keys", [])
            matched = False
            for key in keys:
                try:
                    matched = RSAAlgorithm.from_jwk(key).public_numbers() == known_key.public_numbers()
                except (ValueError, TypeError, AttributeError):
                    continue
                if matched:
                    break
            if not matched:
                raise ValueError("新地址属于另一台云服务器，原连接已保留。请使用“添加云端连接”登记新服务器。")
            async with self.refresh_lock:
                self.settings.data.update(cloud_url=cloud_url, allow_insecure_http=allow_insecure_http)
                self.settings.save()
                self.access = ""
                self.access_expires = 0
                self.resource_tokens.clear()
                self.synced_at = 0
                self.published.clear()
            if self.frp is not None:
                await self.frp.stop()
            await asyncio.gather(*(actor.close() for actor in list(self.actors.values())))
            self.actors.clear()
            await self._tick()

    async def tick(self):
        async with self.connection_lock:
            await self._tick()

    async def _tick(self):
        if not self.settings.data.get("device_id"):
            return
        await self.api("POST", "/api/devices/heartbeat")
        if self.role == "host":
            from cryptography import x509

            from .frp import write_frpc

            details = await self.api("GET", "/api/devices/connection")
            if (
                details["device_id"] != self.settings.data["device_id"]
                or details["signing_public_key"] != self.settings.data["signing_public_key"]
            ):
                raise ValueError("Cloud identity does not match the enrolled server")
            fields = ("frp_server", "frp_port", "remote_port")
            if any(details[key] != self.settings.data.get(key) for key in fields):
                self.settings.data.update({key: details[key] for key in fields})
                self.settings.save()
                await self.frp.stop()
            # Also refresh the token endpoint after a public URL change.
            write_frpc(self.settings, self.vault.read())

            certificate = x509.load_pem_x509_certificate((self.settings.home / "device.crt").read_bytes())
            if certificate.not_valid_after_utc.timestamp() < time.time() + 7 * 86400:
                with tempfile.TemporaryDirectory(prefix=".renew-", dir=self.settings.home) as temporary:
                    staged = Path(temporary)
                    result = await self.api(
                        "POST", "/api/devices/certificate",
                        json={"csr": make_csr(staged, self.settings.data["device_id"])},
                    )
                    self.save_host_certificate(staged, {
                        "certificate": result["certificate"],
                        "ca": (self.settings.home / "ca.crt").read_text(encoding="utf-8"),
                    })
                # The adapter's TLS context is refreshed by the CLI supervisor.
                self.settings.data["certificate_revision"] = time.time()
                self.settings.save()
                await self.frp.stop()
            if not self.settings.data.get("disable_frp", False):
                await self.frp.start()
            for sid in list(self.settings.data.get("pending_deletions", [])):
                try:
                    await self.api("DELETE", "/api/hosts/me/servers/" + sid)
                except httpx.HTTPStatusError as error:
                    if error.response.status_code != 404:
                        raise
                self.settings.data["pending_deletions"].remove(sid)
                self.settings.save()
            for sid, service in self.vault.read().get("services", {}).items():
                exported = {
                    k: service[k] for k in ("name", "description", "transport", "enabled") if k in service
                }
                fingerprint = digest(json.dumps(exported, sort_keys=True))
                if self.published.get(sid) != fingerprint:
                    await self.api("PUT", "/api/hosts/me/servers/" + sid, json=exported)
                    self.published[sid] = fingerprint
        else:
            data = await self.api("GET", "/api/catalog")
            self.catalog = data["servers"]
            self.synced_at = time.time()
            if self.settings.data.get("subscribe_all"):
                self.subscribe([x["id"] for x in self.catalog])
            for target, mode in self.settings.data.get("auto_exports", {}).items():
                ids = self.active_mapping_ids()
                mappings = self.vault.read().get("mappings", {})
                if self.settings.data.get("auto_export_all", {}).get(target) and hasattr(self, "connection_registry"):
                    preview = self.connection_registry.export_all(target, mode)
                else:
                    preview = export_preview(
                        target, ids, self.settings.home, mappings, self.port, mode,
                        {item["id"]: item["name"] for item in self.catalog}, self.route_prefix,
                    )
                if preview["diff"]:
                    write_export(preview)
        self.status = "已连接"

    def active_mapping_ids(self):
        allowed = {s["id"] for s in self.catalog}
        return [
            sid
            for sid, item in self.vault.read().get("mappings", {}).items()
            if item.get("enabled", True) and sid in allowed
        ]

    def subscribe(self, ids: list[str], enable_existing=False):
        allowed = {s["id"] for s in self.catalog}
        if any(sid not in allowed for sid in ids):
            raise ValueError("A selected server is not in the authorized catalog")
        mappings = self.vault.read().get("mappings", {})
        for sid in ids:
            if sid not in mappings:
                mappings[sid] = {"token": secrets.token_urlsafe(32), "enabled": True}
            elif enable_existing:
                mappings[sid]["enabled"] = True
        self.vault.update(mappings=mappings)

    async def run(self):
        while not self.closed.is_set():
            try:
                await self.tick()
            except Exception as error:
                self.status = "连接失败：" + type(error).__name__
                # Fail closed. In particular, revocation must stop a still-connected frpc.
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in {401, 403}:
                    self.access_expires = 0
                    self.synced_at = 0
                    if self.frp is not None:
                        await self.frp.stop()
                    await asyncio.gather(*(actor.close() for actor in list(self.actors.values())))
                    self.actors.clear()
            for key, actor in list(self.actors.items()):
                if actor.active == 0 and actor.last_used < time.monotonic() - 300:
                    await actor.close()
                    self.actors.pop(key, None)
            try:
                await asyncio.wait_for(self.closed.wait(), 10)
            except TimeoutError:
                pass

    async def close(self):
        self.closed.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.frp is not None:
            await self.frp.stop()
        await asyncio.gather(*(actor.close() for actor in list(self.actors.values())))


def validate_enrollment(body: dict, settings: Settings):
    url, code, name = (body.get(key, "") for key in ("cloud_url", "code", "name"))
    if not all(isinstance(value, str) for value in (url, code, name)):
        raise ValueError("云端地址、接入码和设备名称必须是文本")
    url, code, name = url.strip(), code.strip(), name.strip()
    if not name or len(name) > 128 or not code or len(code) > 512:
        raise ValueError("请填写设备名称和一次性接入码；设备名称最多 128 个字符")
    if "allow_insecure_http" in body and type(body["allow_insecure_http"]) is not bool:
        raise ValueError("HTTP 选项必须是布尔值")
    return url, code, name, bool(body.get("allow_insecure_http", settings.data.get("allow_insecure_http", False)))


def cloud_error_message(error: httpx.HTTPStatusError):
    try:
        detail = error.response.json().get("detail")
        if isinstance(detail, str):
            return detail[:300]
    except (ValueError, AttributeError):
        pass
    return f"云端拒绝请求（HTTP {error.response.status_code}），请检查地址、代理及接入码"


def validate_service(body: dict) -> dict:
    transport = body.get("transport")
    name = str(body.get("name", "")).strip()
    if transport not in {"http", "stdio", "sse"} or not name or len(name) > 128:
        raise ValueError("Name and a supported transport are required")
    if "enabled" in body and type(body["enabled"]) is not bool:
        raise ValueError("Enabled must be a boolean")
    result = {
        "name": name,
        "description": str(body.get("description", ""))[:1000],
        "transport": transport,
        "enabled": bool(body.get("enabled", True)),
    }
    if transport == "stdio":
        command, args = body.get("command"), body.get("args", [])
        if (
            not isinstance(command, str)
            or not command
            or "\0" in command
            or not isinstance(args, list)
            or any(not isinstance(x, str) or "\0" in x for x in args)
        ):
            raise ValueError("Provide an executable and a JSON string array of arguments")
        env = body.get("env", {})
        if not isinstance(env, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or not k or "=" in k or "\0" in k + v for k, v in env.items()
        ):
            raise ValueError("Environment must be a string-to-string object")
        cwd = body.get("cwd")
        if cwd is not None and (not isinstance(cwd, str) or "\0" in cwd):
            raise ValueError("Working directory must be a path string")
        result.update(command=command, args=args, env=env, cwd=body.get("cwd") or None)
    else:
        if not isinstance(body.get("url", ""), str):
            raise ValueError("Upstream URL must be text")
        url = urlsplit(body.get("url", ""))
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError("Upstream port must be between 1 and 65535")
        if (
            url.scheme not in {"http", "https"}
            or url.hostname not in {"127.0.0.1", "::1", "localhost"}
            or url.username
            or url.password
            or url.fragment
        ):
            raise ValueError("Upstream must be an explicitly configured loopback HTTP(S) URL")
        headers = body.get("headers", {})
        if not isinstance(headers, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) or "\n" in k + v or "\r" in k + v
            for k, v in headers.items()
        ):
            raise ValueError("Invalid upstream headers")
        result.update(url=body["url"], headers=headers)
    return result


def create_agent_app(agent: Agent, background=True, session_store=None, connections=None) -> FastAPI:
    if session_store is None:
        from .connections import Connections

        connections = connections or Connections(agent, lock_profiles=background)

    @asynccontextmanager
    async def lifespan(app):
        if background:
            await connections.start()
        yield
        if connections:
            await connections.close()

    app = FastAPI(title="MCP Transfer " + agent.role, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.agent = agent
    app.state.connections = connections
    origins = {f"http://127.0.0.1:{agent.port}", f"http://localhost:{agent.port}"}
    app.add_middleware(
        BoundaryMiddleware, origins=origins, hosts={f"127.0.0.1:{agent.port}", f"localhost:{agent.port}"}
    )
    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web, check_dir=False), name="static")
    sessions = session_store if session_store is not None else {}
    cookie_name = f"mcpt_{agent.role}_{agent.port}"
    previews = {}
    rate = RateLimiter()

    def local_user(request: Request):
        value = request.cookies.get(cookie_name, "")
        login = sessions.get(digest(value))
        if not login or login[1] < time.time():
            raise HTTPException(401, "Enter this agent's local access password")
        require_csrf(request, login[0])
        return login[0]

    if connections:
        from .connections import ConnectionRouter

        @app.get("/api/connections")
        async def list_connections(request: Request):
            local_user(request)
            return connections.summary()

        @app.post("/api/connections")
        async def add_connection(request: Request):
            local_user(request)
            try:
                return await connections.add(await request.json())
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            except httpx.HTTPStatusError as error:
                raise HTTPException(400, cloud_error_message(error)) from error
            except httpx.HTTPError as error:
                raise HTTPException(503, "无法连接新云端，原有连接已保留") from error

        @app.delete("/api/connections/{cid}")
        async def remove_connection(cid: str, request: Request):
            local_user(request)
            try:
                return await connections.remove(cid)
            except ValueError as error:
                raise HTTPException(400, str(error)) from error

        app.mount("/connections/{connection_id}", ConnectionRouter(connections, sessions))

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "role": agent.role, "version": __version__}

    @app.get("/api/info")
    async def info():
        return {
            "role": agent.role,
            "version": __version__,
            "insecure_http": agent.settings.data.get("cloud_url", "").startswith("http://"),
        }

    @app.post("/api/auth/login")
    async def login(request: Request):
        rate.check("login", 10)
        body = await request.json()
        if not secrets.compare_digest(str(body.get("password", "")), agent.vault.read()["local_password"]):
            raise HTTPException(401, "Invalid local access password")
        value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        sessions[digest(value)] = (csrf, time.time() + 8 * 3600)
        response = JSONResponse({"csrf": csrf})
        response.set_cookie(cookie_name, value, httponly=True, samesite="strict", max_age=8 * 3600)
        return response

    @app.get("/api/me")
    async def me(request: Request):
        return {"csrf": local_user(request), "username": agent.role, "admin": True}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        local_user(request)
        sessions.pop(digest(request.cookies.get(cookie_name, "")), None)
        response = JSONResponse({"ok": True})
        response.delete_cookie(cookie_name)
        return response

    @app.get("/api/status")
    async def status(request: Request):
        local_user(request)
        return {
            "role": agent.role,
            "status": agent.status,
            "device_id": agent.settings.data.get("device_id"),
            "cloud_url": agent.settings.data.get("cloud_url"),
            "name": agent.settings.data.get("name", ""),
            "allow_insecure_http": bool(agent.settings.data.get("allow_insecure_http")),
            "frp_running": bool(
                agent.frp is not None and agent.frp.process and agent.frp.process.returncode is None
            ),
            "tunnel_ready": agent.tunnel_ready,
            "port": agent.port,
            "subscribe_all": bool(agent.settings.data.get("subscribe_all")),
            "adapter_processes": len(agent.actors),
            "last_sync": agent.synced_at,
            "paths": application_paths(),
            "auto_exports": agent.settings.data.get("auto_exports", {}),
            "proxy": agent.settings.data.get("proxy", ""),
            "enterprise_ca": agent.settings.data.get("enterprise_ca", ""),
        }

    @app.post("/api/configure")
    async def configure(request: Request):
        local_user(request)
        body = await request.json()
        try:
            url, code, name, allow_http = validate_enrollment(body, agent.settings)
            await agent.enroll(
                url, code, name, bool(agent.settings.data.get("development")), allow_http,
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except httpx.HTTPStatusError as error:
            raise HTTPException(400, cloud_error_message(error)) from error
        except httpx.HTTPError as error:
            raise HTTPException(503, "无法连接云端，登记尚未完成，请检查地址和网络") from error
        try:
            await agent.tick()
        except Exception:
            return {"ok": True, "synced": False, "message": "登记已保存；暂未同步成功，请检查网络后点击立即同步，无需重复输入接入码。"}
        return {"ok": True}

    @app.put("/api/connection")
    async def update_connection(request: Request):
        local_user(request)
        body = await request.json()
        try:
            await agent.update_connection(
                body.get("cloud_url", ""), bool(body.get("allow_insecure_http", False))
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except (HTTPException, httpx.HTTPError) as error:
            raise HTTPException(
                503,
                "Address saved, but connection failed. Check the cloud address and retry synchronization.",
            ) from error
        return {"ok": True}

    @app.post("/api/reconnect")
    async def reconnect(request: Request):
        local_user(request)
        body = await request.json()
        try:
            url, code, name, allow_http = validate_enrollment(body, agent.settings)
            return await agent.reconnect(url, code, name, allow_http)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        except httpx.HTTPStatusError as error:
            try:
                detail = error.response.json().get("detail", "登记被云端拒绝，请检查接入码")
            except ValueError:
                detail = "登记被云端拒绝，请检查接入码"
            raise HTTPException(400, str(detail)[:300]) from error
        except httpx.HTTPError as error:
            raise HTTPException(503, "无法连接云端。旧登记和本机映射已保留，请检查地址和网络。") from error

    @app.post("/api/network")
    async def network(request: Request):
        local_user(request)
        body = await request.json()
        proxy, ca = body.get("proxy", ""), body.get("enterprise_ca", "")
        if proxy and urlsplit(proxy).scheme not in {"http", "https"}:
            raise HTTPException(400, "Proxy must be HTTP(S)")
        if ca and not Path(ca).is_file():
            raise HTTPException(400, "CA file does not exist")
        agent.settings.data.update(proxy=proxy, enterprise_ca=ca)
        agent.settings.save()
        return {"ok": True}

    @app.get("/api/settings/limits")
    async def host_limits(request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "请在主机或云端管理并发限制")
        return {"adapter_processes": agent.settings.data.get("max_adapter_processes", 64),
                "sessions_per_adapter": agent.settings.data.get("max_adapter_sessions", 8)}

    @app.put("/api/settings/limits")
    async def update_host_limits(request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        body = await request.json()
        for key in ("adapter_processes", "sessions_per_adapter"):
            if type(body.get(key)) is not int or not 0 <= body[key] <= 4096:
                raise HTTPException(400, "上限必须是 0–4096 的整数，0 表示不限制")
        agent.settings.data.update(max_adapter_processes=body["adapter_processes"], max_adapter_sessions=body["sessions_per_adapter"])
        agent.settings.save()
        return {"ok": True}

    @app.post("/api/sync")
    async def sync(request: Request):
        local_user(request)
        try:
            await agent.tick()
        except Exception as error:
            raise HTTPException(503, "Synchronization failed: " + type(error).__name__) from error
        return {"ok": True}

    @app.get("/api/servers")
    async def servers(request: Request):
        local_user(request)
        if agent.role == "host":
            return [
                {
                    "id": sid,
                    **{k: value for k, value in server.items() if k not in {"env", "headers"}},
                    "secret_fields": [k for k in ("env", "headers") if server.get(k)],
                }
                for sid, server in agent.vault.read().get("services", {}).items()
            ]
        mappings = agent.vault.read().get("mappings", {})
        return [
            {
                **item,
                "mapped": item["id"] in mappings and mappings[item["id"]].get("enabled", True),
                "local_url": f"http://127.0.0.1:{agent.port}{agent.route_prefix}/mcp/{item['id']}",
            }
            for item in agent.catalog
        ]

    @app.post("/api/servers")
    async def add_server(request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        body = await request.json()
        try:
            definition = validate_service(body)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        sid = new_id("srv")
        services = agent.vault.read().get("services", {})
        services[sid] = definition
        agent.vault.update(services=services)
        return {"id": sid, "message": "Saved locally; synchronize to publish"}

    @app.post("/api/import")
    async def import_servers(request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        body = await request.json()
        definitions = body.get("mcpServers", body.get("servers", {}))
        if not isinstance(definitions, dict) or len(definitions) > 100:
            raise HTTPException(400, "Expected a configuration with at most 100 servers")
        services = agent.vault.read().get("services", {})
        imported = []
        for name, item in definitions.items():
            try:
                definition = validate_service(
                    {
                        **item,
                        "name": name,
                        "transport": "stdio" if "command" in item else "http",
                        "enabled": False,
                    }
                )
            except (ValueError, TypeError) as error:
                raise HTTPException(400, f"Invalid definition for {name}: {error}") from error
            sid = new_id("srv")
            services[sid] = definition
            imported.append(sid)
        agent.vault.update(services=services)
        return {"ids": imported, "message": "Imported disabled; inspect before enabling or testing"}

    @app.put("/api/servers/{sid}")
    async def edit_server(sid: str, request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        services = agent.vault.read().get("services", {})
        if sid not in services:
            raise HTTPException(404, "Service not found")
        try:
            services[sid] = validate_service({**services[sid], **(await request.json())})
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        agent.vault.update(services=services)
        for key, actor in list(agent.actors.items()):
            if key[0] == sid:
                await actor.close()
                agent.actors.pop(key, None)
        return {"ok": True}

    @app.delete("/api/servers/{sid}")
    async def delete_server(sid: str, request: Request):
        local_user(request)
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        services = agent.vault.read().get("services", {})
        if sid not in services:
            raise HTTPException(404, "Service not found")
        # Queue cloud removal before changing local state. Offline deletion is
        # fail-closed and retried by the next successful heartbeat.
        queued = agent.settings.data.setdefault("pending_deletions", [])
        if sid not in queued:
            queued.append(sid)
            agent.settings.save()
        del services[sid]
        agent.vault.update(services=services)
        for key, actor in list(agent.actors.items()):
            if key[0] == sid:
                await actor.close()
                agent.actors.pop(key, None)
        return {"ok": True, "message": "本机服务已删除，云端服务和授权将在下次同步时清理"}

    @app.post("/api/servers/{sid}/toggle")
    async def toggle(sid: str, request: Request):
        local_user(request)
        values = agent.vault.read()
        key = "services" if agent.role == "host" else "mappings"
        items = values.get(key, {})
        if sid not in items:
            raise HTTPException(404, "Service not found")
        items[sid]["enabled"] = not items[sid].get("enabled", True)
        agent.vault.update(**{key: items})
        return {"ok": True}

    @app.post("/api/servers/{sid}/test")
    async def test_server(sid: str, request: Request):
        local_user(request)
        if agent.role == "host":
            definition = agent.vault.read().get("services", {}).get(sid)
            if not definition:
                raise HTTPException(404, "Service not found")
            context = upstream_client(definition)
        else:
            mapping = agent.vault.read().get("mappings", {}).get(sid)
            if not mapping or not mapping.get("enabled", True):
                raise HTTPException(400, "请先启用此服务的本地映射")
            if sid not in {item["id"] for item in agent.catalog}:
                raise HTTPException(403, "此服务不在已授权目录中，请先同步")
            context = mapped_client(agent.port, sid, mapping["token"], agent.route_prefix)
        try:
            async with asyncio.timeout(30), context as client:
                result = await inspect_tools(client)
                result["stage"] = "host_upstream" if agent.role == "host" else "client_roundtrip"
                return result
        except Exception as error:
            logging.exception("MCP diagnostic failed: role=%s server=%s", agent.role, sid)
            stage = "主机本地 MCP" if agent.role == "host" else "客户端 → 云端 → 主机 MCP"
            raise HTTPException(
                502, stage + "检测失败（" + type(error).__name__ + "），请查看本机 data/agent.log 与云端调用审计"
            ) from error

    @app.post("/api/subscribe")
    async def subscribe(request: Request):
        local_user(request)
        if agent.role != "client":
            raise HTTPException(403, "Client role required")
        body = await request.json()
        try:
            if "all" in body:
                agent.settings.data["subscribe_all"] = bool(body["all"])
                agent.settings.save()
            agent.subscribe(body.get("ids", [x["id"] for x in agent.catalog]), enable_existing=True)
        except ValueError as error:
            raise HTTPException(400, str(error)) from error
        return {"ok": True}

    @app.post("/api/exports/preview")
    async def preview(request: Request):
        local_user(request)
        if agent.role != "client":
            raise HTTPException(403, "Client role required")
        body = await request.json()
        target, mode = body.get("target", "generic"), body.get("mode", "stdio")
        if target not in TARGETS:
            raise HTTPException(400, "Unknown target application")
        ids = agent.active_mapping_ids()
        mappings = agent.vault.read().get("mappings", {})
        try:
            if body.get("scope") == "all" and connections:
                result = connections.export_all(target, mode)
            else:
                result = export_preview(
                    target, ids, agent.settings.home, mappings, agent.port, mode,
                    {item["id"]: item["name"] for item in agent.catalog}, agent.route_prefix,
                )
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error
        key = secrets.token_urlsafe(24)
        previews[key] = {**result, "target": target, "mode": mode, "scope": body.get("scope"), "expires": time.time() + 300}
        return {**{k: result[k] for k in ("path", "content", "diff")}, "preview_id": key}

    @app.post("/api/exports/apply")
    async def apply_export(request: Request):
        local_user(request)
        body = await request.json()
        preview = previews.pop(body.get("preview_id"), None)
        if not preview or preview["expires"] < time.time():
            raise HTTPException(400, "Preview expired; generate it again")
        try:
            result = write_export(preview)
        except (ValueError, OSError) as error:
            raise HTTPException(409, str(error)) from error
        targets_written = agent.settings.data.setdefault("export_targets", [])
        if preview["target"] not in targets_written:
            targets_written.append(preview["target"])
        agent.settings.save()
        if body.get("auto_sync"):
            targets = agent.settings.data.setdefault("auto_exports", {})
            targets[preview["target"]] = preview["mode"]
            agent.settings.data.setdefault("auto_export_all", {})[preview["target"]] = preview.get("scope") == "all"
            agent.settings.save()
        return result

    @app.api_route("/mcp/{sid}", methods=["GET", "POST", "DELETE"])
    async def local_mcp(sid: str, request: Request):
        if agent.role != "client":
            raise HTTPException(404)
        mapping = agent.vault.read().get("mappings", {}).get(sid)
        if (
            not mapping
            or not mapping.get("enabled", True)
            or not secrets.compare_digest(bearer(request), mapping["token"])
        ):
            raise HTTPException(401, "Invalid local mapping credential")
        if agent.synced_at < time.time() - 30 or sid not in {x["id"] for x in agent.catalog}:
            raise HTTPException(503, "Authorized catalog is unavailable; synchronize first")
        body = await limited_body(request)
        token = agent.resource_tokens.get(sid)
        if not token or token[1] < time.time() + 15:
            try:
                result = await agent.api("POST", "/api/access-token", json={"server_id": sid})
            except httpx.HTTPStatusError as error:
                raise HTTPException(error.response.status_code, "云端授权失效或服务不可用，请同步目录并检查设备登记") from error
            except httpx.HTTPError as error:
                raise HTTPException(503, "无法连接云端，请检查网络；调用未被自动重放") from error
            token = (result["access_token"], time.time() + result["expires_in"])
            agent.resource_tokens[sid] = token
        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        connection = request.headers.get("x-mcpt-connection", "default")
        if len(connection) > 64 or not connection.replace("-", "").isalnum():
            raise HTTPException(400, "Invalid connection identifier")
        headers.update(authorization="Bearer " + token[0], **{"x-mcpt-connection": connection})
        client = agent.cloud_http()
        try:
            outbound = client.build_request(
                request.method,
                agent.settings.data["cloud_url"] + "/mcp/" + sid,
                headers=headers,
                content=body,
            )
            response = await client.send(outbound, stream=True)
        except httpx.HTTPError:
            await client.aclose()
            raise HTTPException(
                503, "Cloud unavailable; the operation was not automatically replayed"
            ) from None

        async def alive():
            mapping = agent.vault.read().get("mappings", {}).get(sid, {})
            return (
                agent.synced_at > time.time() - 30
                and mapping.get("enabled", False)
                and sid in {x["id"] for x in agent.catalog}
            )

        return await relay_response(response, client.aclose, alive)

    return app


class HostAdapter:
    def __init__(self, agent: Agent):
        self.agent = agent

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        agent = self.agent
        try:
            sid = request.url.path.removeprefix("/mcp/")
            # This adapter listener requires mTLS. The gateway checks the
            # enrolled identity here before reporting host recovery ready.
            if request.url.path == "/healthz" and request.method == "GET":
                await JSONResponse({"device_id": agent.settings.data.get("device_id")})(scope, receive, send)
                return
            if request.url.path != "/mcp/" + sid or not sid.startswith("srv_") or "/" in sid:
                raise HTTPException(404, "Unknown route")
            ticket = verify_token(
                bearer(request),
                agent.settings.data["signing_public_key"],
                agent.settings.data["cloud_url"],
                agent.settings.data["device_id"],
            )
            body = await limited_body(request)
            if (
                ticket.get("kind") != "invoke"
                or ticket.get("sid") != sid
                or ticket.get("method") != request.method
                or ticket.get("body_hash") != digest(body)
            ):
                raise HTTPException(403, "Invocation ticket does not match request")
            now = time.time()
            agent.used_tickets = {
                key: expires for key, expires in agent.used_tickets.items() if expires > now
            }
            if ticket["jti"] in agent.used_tickets:
                raise HTTPException(403, "Invocation ticket was already used")
            agent.used_tickets[ticket["jti"]] = ticket["exp"]
            definition = agent.vault.read().get("services", {}).get(sid)
            if not definition or not definition.get("enabled", True):
                raise HTTPException(404, "Service is disabled")
            message = parse_message(body)
            authorize_message(message, ticket.get("policy", {}))
            if definition["transport"] == "http":
                headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
                headers.update(definition.get("headers", {}))
                client = httpx.AsyncClient(
                    timeout=httpx.Timeout(30, read=None), follow_redirects=False, trust_env=False
                )
                try:
                    outbound = client.build_request(
                        request.method, definition["url"], headers=headers, content=body
                    )
                    response = await client.send(outbound, stream=True)
                except httpx.HTTPError:
                    await client.aclose()
                    raise HTTPException(503, "Local MCP unavailable") from None

                async def alive():
                    current = agent.vault.read().get("services", {}).get(sid)
                    return bool(
                        current and current.get("enabled", True) and time.time() < ticket["lease_until"]
                    )

                result = await relay_response(response, client.aclose, alive)
                await result(scope, receive, send)
                return
            key = (sid, ticket["uid"], ticket["sub"], ticket.get("connection", "default"))
            if key not in agent.actors:
                limit = agent.settings.data.get("max_adapter_processes", 64)
                if limit and len(agent.actors) >= limit:
                    raise HTTPException(429, f"主机 MCP 适配实例达到上限 {limit}，请在主机设置中调整", headers={"Retry-After": "1"})
                agent.actors[key] = Actor(definition, sid, agent.settings.data.get("max_adapter_sessions", 8))
            actor = agent.actors[key]
            application = await actor.get_app()
            consumed = False

            async def replay():
                nonlocal consumed
                if not consumed:
                    consumed = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            # The SDK sees loopback Host, never public bearer credentials.
            inner_scope = {
                **scope,
                "headers": [
                    (k, v) for k, v in scope["headers"] if k.lower() not in {b"authorization", b"origin"}
                ],
            }
            actor.active += 1
            try:
                await application(inner_scope, replay, send)
            finally:
                actor.active -= 1
                actor.last_used = time.monotonic()
        except HTTPException as error:
            await JSONResponse({"detail": error.detail}, status_code=error.status_code)(scope, receive, send)
        except Exception:
            logging.exception("Host MCP adapter failed: server=%s", sid)
            await JSONResponse({"detail": "Host adapter failed; inspect MCP configuration"}, status_code=502)(
                scope, receive, send
            )
