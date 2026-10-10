from __future__ import annotations

import asyncio
import json
import secrets
import ssl
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
from .frp import FrpProcess, write_frpc
from .http_support import REQUEST_HEADERS, relay_response
from .pki import make_csr
from .policy import authorize_message, parse_message
from .protocol import Actor, upstream_client
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
        self.resource_tokens = {}
        self.catalog = []
        self.synced_at = 0.0
        self.status = "未连接云端"
        self.frp = FrpProcess(settings, "frpc")
        self.actors: dict[tuple, Actor] = {}
        self.used_tickets = {}
        self.published = {}
        self.closed = asyncio.Event()
        self.task = None

    @property
    def port(self):
        return int(self.settings.data.get("ui_port", 17810 if self.role == "host" else 17820))

    def cloud_http(self):
        options = {"timeout": httpx.Timeout(20, read=None), "follow_redirects": False}
        if self.settings.data.get("proxy"):
            options["proxy"] = self.settings.data["proxy"]
        ca = self.settings.data.get("enterprise_ca")
        if ca:
            options["verify"] = ssl.create_default_context(cafile=ca)
        return httpx.AsyncClient(**options)

    async def enroll(self, cloud_url: str, code: str, name: str, development=False):
        if self.settings.data.get("device_id"):
            raise ValueError(
                "Device already enrolled. Revoke it in the cloud and use a new profile to re-enroll."
            )
        cloud_url = validate_cloud_url(cloud_url, development)
        payload = {"code": code, "name": name}
        if self.role == "host":
            payload["csr"] = make_csr(self.settings.home, "pending-host")
        async with self.cloud_http() as client:
            response = await client.post(cloud_url + "/api/devices/enroll", json=payload)
            response.raise_for_status()
            result = response.json()
        if result["role"] != self.role:
            raise ValueError("Enrollment code role does not match this agent")
        self.vault.update(refresh_token=result["refresh_token"], frp_secret=result.get("frp_secret"))
        self.settings.data.update(
            {
                k: result[k]
                for k in ("device_id", "role", "remote_port", "frp_server", "frp_port", "signing_public_key")
            }
        )
        self.settings.data.update(cloud_url=cloud_url, name=name, development=development)
        self.settings.save()
        atomic_write(self.settings.home / "ca.crt", result["ca"])
        if result.get("certificate"):
            atomic_write(self.settings.home / "device.crt", result["certificate"])
            write_frpc(self.settings, self.vault.read())
        secure_windows_directory(self.settings.home)

    async def cloud_access(self):
        async with self.refresh_lock:
            if self.access and self.access_expires > time.time() + 30:
                return self.access
            refresh = self.vault.read().get("refresh_token")
            if not refresh:
                raise ValueError("Agent is not enrolled")
            async with self.cloud_http() as client:
                response = await client.post(
                    self.settings.data["cloud_url"] + "/api/token", json={"refresh_token": refresh}
                )
                response.raise_for_status()
                data = response.json()
            # Persist the rotated secret before making another request.
            self.vault.update(refresh_token=data["refresh_token"])
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
                **kwargs,
            )
            response.raise_for_status()
            return response.json()

    async def tick(self):
        if not self.settings.data.get("device_id"):
            return
        await self.api("POST", "/api/devices/heartbeat")
        if self.role == "host":
            from cryptography import x509

            certificate = x509.load_pem_x509_certificate((self.settings.home / "device.crt").read_bytes())
            if certificate.not_valid_after_utc.timestamp() < time.time() + 7 * 86400:
                result = await self.api(
                    "POST",
                    "/api/devices/certificate",
                    json={"csr": make_csr(self.settings.home, self.settings.data["device_id"])},
                )
                atomic_write(self.settings.home / "device.crt", result["certificate"])
                # The adapter's TLS context is refreshed by the CLI supervisor.
                self.settings.data["certificate_revision"] = time.time()
                self.settings.save()
                await self.frp.stop()
            if not self.settings.data.get("disable_frp", False):
                await self.frp.start()
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
                preview = export_preview(target, ids, self.settings.home, mappings, self.port, mode)
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
                    await self.frp.stop()
                    for actor in list(self.actors.values()):
                        await actor.close()
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
        await self.frp.stop()
        for actor in list(self.actors.values()):
            await actor.close()


def validate_service(body: dict) -> dict:
    transport = body.get("transport")
    name = str(body.get("name", "")).strip()
    if transport not in {"http", "stdio", "sse"} or not name or len(name) > 128:
        raise ValueError("Name and a supported transport are required")
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
            not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()
        ):
            raise ValueError("Environment must be a string-to-string object")
        result.update(command=command, args=args, env=env, cwd=body.get("cwd") or None)
    else:
        url = urlsplit(body.get("url", ""))
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


def create_agent_app(agent: Agent, background=True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if background:
            agent.task = asyncio.create_task(agent.run())
        yield
        await agent.close()

    app = FastAPI(title="MCP Transfer " + agent.role, lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.agent = agent
    origins = {f"http://127.0.0.1:{agent.port}", f"http://localhost:{agent.port}"}
    app.add_middleware(
        BoundaryMiddleware, origins=origins, hosts={f"127.0.0.1:{agent.port}", f"localhost:{agent.port}"}
    )
    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web, check_dir=False), name="static")
    sessions = {}
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

    @app.get("/")
    async def index():
        return FileResponse(web / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "role": agent.role, "version": __version__}

    @app.get("/api/info")
    async def info():
        return {"role": agent.role, "version": __version__}

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
            "frp_running": bool(agent.frp.process and agent.frp.process.returncode is None),
            "port": agent.port,
            "subscribe_all": bool(agent.settings.data.get("subscribe_all")),
            "adapter_processes": len(agent.actors),
            "last_sync": agent.synced_at,
            "paths": application_paths(),
            "auto_exports": agent.settings.data.get("auto_exports", {}),
        }

    @app.post("/api/configure")
    async def configure(request: Request):
        local_user(request)
        body = await request.json()
        try:
            await agent.enroll(
                body.get("cloud_url", ""),
                body.get("code", ""),
                body.get("name", "Device"),
                bool(agent.settings.data.get("development")),
            )
            await agent.tick()
        except (ValueError, httpx.HTTPError) as error:
            raise HTTPException(400, str(error)[:300]) from error
        return {"ok": True}

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
                "local_url": f"http://127.0.0.1:{agent.port}/mcp/{item['id']}",
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
        if agent.role != "host":
            raise HTTPException(403, "Host role required")
        definition = agent.vault.read().get("services", {}).get(sid)
        if not definition:
            raise HTTPException(404, "Service not found")
        try:
            async with asyncio.timeout(30), upstream_client(definition) as client:
                return {
                    "ok": True,
                    "version": client.protocol_version,
                    "capabilities": client.server_capabilities.model_dump(by_alias=True, exclude_none=True),
                }
        except Exception as error:
            raise HTTPException(502, "MCP startup/test failed: " + type(error).__name__) from error

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
            result = export_preview(target, ids, agent.settings.home, mappings, agent.port, mode)
        except (ValueError, OSError) as error:
            raise HTTPException(400, str(error)) from error
        key = secrets.token_urlsafe(24)
        previews[key] = {**result, "target": target, "mode": mode, "expires": time.time() + 300}
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
        if body.get("auto_sync"):
            targets = agent.settings.data.setdefault("auto_exports", {})
            targets[preview["target"]] = preview["mode"]
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
            result = await agent.api("POST", "/api/access-token", json={"server_id": sid})
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
                if len(agent.actors) >= 64:
                    raise HTTPException(429, "Host process quota reached")
                agent.actors[key] = Actor(definition, sid)
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
            await JSONResponse({"detail": "Host adapter failed; inspect MCP configuration"}, status_code=502)(
                scope, receive, send
            )
