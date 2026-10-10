from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import ssl
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
import pyotp
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, or_, select, update

from . import __version__
from .config import Settings, validate_cloud_url
from .database import (
    Audit,
    ConsumedRefresh,
    Device,
    Grant,
    Invite,
    OAuthCode,
    Service,
    User,
    WebSession,
    database,
)
from .http_support import REQUEST_HEADERS, relay_response
from .pki import PKI
from .policy import authorize_message, parse_message
from .security import (
    BoundaryMiddleware,
    RateLimiter,
    Signer,
    bearer,
    digest,
    limited_body,
    new_id,
    passwords,
    require_csrf,
    verify_password,
)


def create_cloud(settings: Settings) -> FastAPI:
    settings.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = database(settings.database_url)
    public = validate_cloud_url(
        settings.cloud_url,
        settings.data.get("development", False),
        settings.data.get("allow_insecure_http", False),
    )
    origin = urlsplit(public)
    signer = Signer(settings.home, public)
    pki = PKI(settings.home)
    pki.local_certificate("gateway", ["gateway.mcp.internal"])
    pki.local_certificate("frps", [origin.hostname])
    rate = RateLimiter()
    sessions: dict[tuple[str, str], tuple[str, str, float]] = {}
    inflight: dict[str, int] = {}
    pending_mfa: dict[str, tuple[str, float]] = {}

    @asynccontextmanager
    async def lifespan(app):
        yield
        db.kw["bind"].dispose()

    app = FastAPI(title="MCP Transfer Cloud", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.signer = signer
    app.state.settings = settings
    app.add_middleware(BoundaryMiddleware, origins={public}, hosts={origin.netloc})
    web = Path(__file__).parent / "web"
    app.mount("/static", StaticFiles(directory=web, check_dir=False), name="static")

    def log(actor: str, action: str, target: str = "", status: str = "ok"):
        with db.begin() as session:
            session.add(
                Audit(id=new_id("a"), actor=actor, action=action[:128], target=target[:128], status=status)
            )

    def client_ip(request: Request):
        return request.client.host if request.client else "unknown"

    def user_session(request: Request, admin: bool = False) -> User:
        value = request.cookies.get("mcpt_session", "")
        with db() as session:
            login = session.get(WebSession, digest(value))
            user = session.get(User, login.user_id) if login and login.expires > time.time() else None
            if not user or not user.active:
                raise HTTPException(401, "Please sign in")
            require_csrf(request, login.csrf)
            if admin and not user.admin:
                raise HTTPException(403, "Administrator permission required")
            return user

    def active_device(session, did: str, role: str | None = None):
        device = session.get(Device, did)
        owner = session.get(User, device.owner) if device else None
        if not device or not device.active or not owner or not owner.active:
            raise HTTPException(401, "Device revoked or unavailable")
        if role and device.role != role:
            raise HTTPException(403, "Wrong device role")
        return device

    def device_request(request: Request, role: str | None = None) -> Device:
        claims = signer.verify(bearer(request), "cloud-api")
        if claims.get("kind") != "device":
            raise HTTPException(403, "Device credential required")
        with db() as session:
            return active_device(session, claims["sub"], role)

    def can_use(session, user_id: str, did: str, server_id: str):
        service = session.get(Service, server_id)
        host = session.get(Device, service.host_id) if service else None
        owner = session.get(User, host.owner) if host else None
        if not service or not service.enabled or not host or not host.active or not owner or not owner.active:
            raise HTTPException(403, "Service unavailable or permission denied")
        if host.owner == user_id:
            return service, host, {"methods": ["*"], "tools": ["*"], "resources": ["*"], "prompts": ["*"]}
        grants = session.scalars(
            select(Grant).where(
                Grant.user_id == user_id,
                Grant.server_id == server_id,
                or_(Grant.device_id.is_(None), Grant.device_id == did),
            )
        ).all()
        if not grants:
            raise HTTPException(403, "Service permission denied")
        # Union of explicit grants for this user/device, each grant has independent scope.
        policy = {}
        for field in ("methods", "tools", "resources", "prompts"):
            values = set()
            for grant in grants:
                values.update(grant.policy.get(field, ["*"]))
            policy[field] = sorted(values)
        return service, host, policy

    @app.get("/")
    @app.get("/oauth/authorize")
    async def index():
        return FileResponse(web / "index.html")

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/api/info")
    async def info():
        return {
            "role": "cloud",
            "version": __version__,
            "cloud_url": public,
            "insecure_http": origin.scheme == "http",
        }

    @app.post("/api/auth/login")
    async def login(request: Request):
        rate.check("login:" + client_ip(request), 10, 60)
        body = json.loads(await limited_body(request, 8192))
        username = str(body.get("username", ""))[:128]
        candidate = str(body.get("password", ""))[:1024]
        with db.begin() as session:
            user = session.scalar(select(User).where(User.username == username))
            if not user or not user.active or not verify_password(user.password, candidate):
                raise HTTPException(401, "Invalid username or password")
            if user.totp and not pyotp.TOTP(user.totp).verify(str(body.get("otp", "")), valid_window=1):
                raise HTTPException(401, "Valid MFA code required")
            value, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
            session.add(
                WebSession(hash=digest(value), user_id=user.id, csrf=csrf, expires=time.time() + 8 * 3600)
            )
            session.execute(delete(WebSession).where(WebSession.expires < time.time()))
        response = JSONResponse({"csrf": csrf, "username": user.username, "admin": user.admin})
        response.set_cookie(
            "mcpt_session",
            value,
            httponly=True,
            secure=origin.scheme == "https",
            samesite="strict",
            max_age=8 * 3600,
        )
        log(user.id, "login")
        return response

    @app.get("/api/me")
    async def me(request: Request):
        user = user_session(request)
        with db() as session:
            login = session.get(WebSession, digest(request.cookies["mcpt_session"]))
        return {
            "id": user.id,
            "username": user.username,
            "admin": user.admin,
            "csrf": login.csrf,
            "mfa": bool(user.totp),
        }

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        user_session(request)
        with db.begin() as session:
            session.execute(
                delete(WebSession).where(WebSession.hash == digest(request.cookies.get("mcpt_session", "")))
            )
        response = JSONResponse({"ok": True})
        response.delete_cookie("mcpt_session")
        return response

    @app.post("/api/auth/password")
    async def change_password(request: Request):
        user = user_session(request)
        body = await request.json()
        value = body.get("password", "")
        if (
            not verify_password(user.password, body.get("current_password", ""))
            or not 12 <= len(value) <= 256
        ):
            raise HTTPException(400, "Current password invalid, or new password must have 12–256 characters")
        with db.begin() as session:
            session.get(User, user.id).password = passwords.hash(value)
            session.execute(delete(WebSession).where(WebSession.user_id == user.id))
        log(user.id, "password_changed")
        return {"ok": True}

    @app.post("/api/auth/mfa/setup")
    async def mfa_setup(request: Request):
        user = user_session(request)
        if user.totp:
            raise HTTPException(409, "MFA already enabled")
        secret = pyotp.random_base32()
        pending_mfa[user.id] = (secret, time.time() + 300)
        return {
            "secret": secret,
            "uri": pyotp.TOTP(secret).provisioning_uri(user.username, issuer_name="MCP Transfer"),
        }

    @app.post("/api/auth/mfa/confirm")
    async def mfa_confirm(request: Request):
        user = user_session(request)
        secret, expires = pending_mfa.get(user.id, ("", 0))
        body = await request.json()
        rate.check("mfa:" + user.id, 5)
        if (
            expires < time.time()
            or not secret
            or not pyotp.TOTP(secret).verify(body.get("otp", ""), valid_window=1)
        ):
            raise HTTPException(400, "Invalid MFA code")
        with db.begin() as session:
            session.get(User, user.id).totp = secret
        pending_mfa.pop(user.id, None)
        return {"ok": True}

    @app.get("/api/users")
    async def users(request: Request):
        user_session(request, admin=True)
        with db() as session:
            return [
                {
                    "id": u.id,
                    "username": u.username,
                    "admin": u.admin,
                    "active": u.active,
                    "mfa": bool(u.totp),
                }
                for u in session.scalars(select(User))
            ]

    @app.post("/api/users")
    async def add_user(request: Request):
        actor = user_session(request, admin=True)
        body = await request.json()
        username, password = str(body.get("username", "")).strip(), str(body.get("password", ""))
        if not 1 <= len(username) <= 128 or not 12 <= len(password) <= 256:
            raise HTTPException(400, "Name required; password must have 12–256 characters")
        with db.begin() as session:
            if session.scalar(select(User).where(User.username == username)):
                raise HTTPException(409, "Username exists")
            user = User(
                id=new_id("u"),
                username=username,
                password=passwords.hash(password),
                admin=bool(body.get("admin", False)),
            )
            session.add(user)
        log(actor.id, "user_created", user.id)
        return {"id": user.id}

    @app.post("/api/users/{uid}/disable")
    async def disable_user(uid: str, request: Request):
        actor = user_session(request, admin=True)
        if uid == actor.id:
            raise HTTPException(400, "Cannot disable your own administrator account")
        with db.begin() as session:
            user = session.get(User, uid)
            if not user:
                raise HTTPException(404, "User not found")
            user.active = False
        log(actor.id, "user_disabled", uid)
        return {"ok": True}

    @app.post("/api/enrollment-codes")
    async def invite(request: Request):
        user = user_session(request)
        body = await request.json()
        role = body.get("role")
        owner = body.get("owner") or user.id
        if role not in {"host", "client"} or (owner != user.id and not user.admin):
            raise HTTPException(403, "Invalid role or owner")
        code = secrets.token_urlsafe(24)
        with db.begin() as session:
            target = session.get(User, owner)
            if not target or not target.active:
                raise HTTPException(400, "Invalid owner")
            session.add(Invite(hash=digest(code), owner=owner, role=role, expires=time.time() + 300))
        log(user.id, "enrollment_code_created", role)
        return {"code": code, "expires_in": 300, "role": role}

    @app.post("/api/devices/enroll")
    async def enroll(request: Request):
        rate.check("enroll:" + client_ip(request), 10)
        body = json.loads(await limited_body(request, 16384))
        if str(body.get("code", "")).count(".") == 2:
            raise HTTPException(400, "请生成普通的一次性接入码")
        refresh = secrets.token_urlsafe(40)
        frp_secret = secrets.token_urlsafe(40)
        certificate = None
        with db.begin() as session:
            code = session.get(Invite, digest(str(body.get("code", ""))))
            if not code or code.used or code.expires < time.time():
                raise HTTPException(401, "Enrollment code expired or already used")
            if body.get("role") and body["role"] != code.role:
                raise HTTPException(403, "Enrollment code role does not match this agent")
            consumed = session.execute(
                update(Invite)
                .where(Invite.hash == code.hash, Invite.used.is_(False), Invite.expires > time.time())
                .values(used=True)
            )
            if consumed.rowcount != 1:
                raise HTTPException(401, "Enrollment code already used")
            user = session.get(User, code.owner)
            if not user or not user.active:
                raise HTTPException(403, "Owner disabled")
            device = Device(
                id=new_id("d"),
                owner=code.owner,
                name=str(body.get("name", "Device"))[:128],
                role=code.role,
                refresh_hash=digest(refresh),
                refresh_expires=time.time() + 30 * 86400,
                frp_secret=digest(frp_secret) if code.role == "host" else None,
            )
            if code.role == "host":
                used = set(session.scalars(select(Device.port).where(Device.port.is_not(None))))
                device.port = next((p for p in range(21000, 22000) if p not in used), None)
                if device.port is None:
                    raise HTTPException(409, "Host capacity reached")
                try:
                    certificate = pki.sign_csr(body.get("csr", ""), device.id, [f"{device.id}.mcp.internal"])
                except (ValueError, TypeError) as error:
                    raise HTTPException(400, "Invalid host CSR") from error
            session.add(device)
        log(device.owner, "device_enrolled", device.id)
        return {
            "device_id": device.id,
            "role": device.role,
            "refresh_token": refresh,
            "ca": pki.ca,
            "certificate": certificate,
            "signing_public_key": signer.public_pem,
            "remote_port": device.port,
            "frp_secret": frp_secret if device.role == "host" else None,
            "frp_server": origin.hostname,
            "frp_port": settings.frp_port,
        }

    def refresh_device(refresh: str, resource: str | None = None, request_id: str | None = None):
        value_hash = digest(refresh)
        if request_id is not None and (not isinstance(request_id, str) or not 16 <= len(request_id) <= 128
                                      or not all(c.isalnum() or c in "-_" for c in request_id)):
            raise HTTPException(400, "Invalid refresh request identifier")
        request_hash = digest(json.dumps([request_id, resource])) if request_id else None
        replacement = (
            hmac.new(signer.private, json.dumps([refresh, request_id, resource]).encode(), hashlib.sha256).hexdigest()
            if request_id else secrets.token_urlsafe(40)
        )
        reused = None
        replay = False
        with db.begin() as session:
            device = session.scalar(select(Device).where(Device.refresh_hash == value_hash))
            if not device:
                consumed = session.get(ConsumedRefresh, value_hash)
                reused = (
                    session.get(Device, consumed.device_id)
                    if consumed and consumed.expires > time.time()
                    else None
                )
                if (reused and request_hash and consumed.request_hash == request_hash
                        and reused.refresh_hash == digest(replacement)):
                    device, replay, reused = reused, True, None
                elif reused:
                    reused.active = False
            if device:
                active_device(session, device.id)
                if device.refresh_expires < time.time() or device.created + 90 * 86400 < time.time():
                    raise HTTPException(401, "Refresh credential expired; enroll again")
                if not replay:
                    result = session.execute(
                        update(Device)
                        .where(Device.id == device.id, Device.refresh_hash == value_hash)
                        .values(
                            previous_refresh=value_hash,
                            refresh_hash=digest(replacement),
                            refresh_expires=time.time() + 30 * 86400,
                        )
                    )
                    if result.rowcount != 1:
                        raise HTTPException(401, "Refresh credential was already rotated")
                    session.add(ConsumedRefresh(
                        hash=value_hash, device_id=device.id, expires=device.created + 90 * 86400,
                        request_hash=request_hash,
                    ))
                session.execute(delete(ConsumedRefresh).where(ConsumedRefresh.expires < time.time()))
                if resource:
                    if not resource.startswith(public + "/mcp/"):
                        raise HTTPException(400, "Invalid resource")
                    sid = resource.removeprefix(public + "/mcp/")
                    can_use(session, device.owner, device.id, sid)
        if not device:
            if reused:
                log(reused.owner, "refresh_reuse_revoked", reused.id)
            raise HTTPException(401, "Invalid refresh credential")
        audience = resource or "cloud-api"
        return {
            "access_token": signer.issue(device.id, audience, kind="device", uid=device.owner),
            "refresh_token": replacement,
            "token_type": "Bearer",
            "expires_in": 300,
        }

    @app.post("/api/token")
    async def token(request: Request):
        rate.check("refresh:" + client_ip(request), 120)
        body = json.loads(await limited_body(request, 8192))
        return refresh_device(str(body.get("refresh_token", "")), request_id=body.get("request_id"))

    @app.post("/api/devices/heartbeat")
    async def heartbeat(request: Request):
        device = device_request(request)
        with db.begin() as session:
            session.get(Device, device.id).last_seen = time.time()
        return {"ok": True}

    @app.get("/api/devices/connection")
    async def connection_details(request: Request):
        device = device_request(request)
        return {
            "device_id": device.id,
            "frp_server": origin.hostname,
            "frp_port": settings.frp_port,
            "remote_port": device.port,
            "signing_public_key": signer.public_pem,
        }

    @app.post("/api/devices/certificate")
    async def certificate(request: Request):
        device = device_request(request, "host")
        body = json.loads(await limited_body(request, 16384))
        try:
            cert = pki.sign_csr(body.get("csr", ""), device.id, [f"{device.id}.mcp.internal"])
        except ValueError as error:
            raise HTTPException(400, "Invalid CSR") from error
        return {"certificate": cert, "ca": pki.ca}

    @app.get("/api/devices")
    async def devices(request: Request):
        user = user_session(request)
        with db() as session:
            query = select(Device) if user.admin else select(Device).where(Device.owner == user.id)
            return [
                {
                    "id": d.id,
                    "name": d.name,
                    "role": d.role,
                    "owner": d.owner,
                    "active": d.active,
                    "online": d.last_seen > time.time() - 40,
                    "last_seen": d.last_seen,
                }
                for d in session.scalars(query)
            ]

    @app.post("/api/devices/{did}/revoke")
    async def revoke(did: str, request: Request):
        user = user_session(request)
        with db.begin() as session:
            device = session.get(Device, did)
            if not device or (device.owner != user.id and not user.admin):
                raise HTTPException(404, "Device not found")
            device.active = False
        log(user.id, "device_revoked", did)
        return {"ok": True}

    def remove_devices(session, ids):
        server_ids = list(session.scalars(select(Service.id).where(Service.host_id.in_(ids))))
        session.execute(delete(Grant).where(or_(Grant.device_id.in_(ids), Grant.server_id.in_(server_ids))))
        session.execute(delete(Service).where(Service.id.in_(server_ids)))
        session.execute(delete(ConsumedRefresh).where(ConsumedRefresh.device_id.in_(ids)))
        session.execute(delete(Device).where(Device.id.in_(ids)))
        for key in list(sessions):
            if key[0] in server_ids or sessions[key][1] in ids:
                sessions.pop(key, None)
        return {"devices": len(ids), "servers": len(server_ids)}

    @app.delete("/api/devices/{did}")
    async def delete_device(did: str, request: Request):
        actor = user_session(request)
        with db.begin() as session:
            device = session.get(Device, did)
            if not device or (device.owner != actor.id and not actor.admin):
                raise HTTPException(404, "Device not found")
            result = remove_devices(session, [did])
        log(actor.id, "device_deleted", did)
        return result

    @app.post("/api/devices/cleanup")
    async def cleanup_devices(request: Request):
        actor = user_session(request)
        with db.begin() as session:
            query = select(Device.id).where(Device.active.is_(False))
            if not actor.admin:
                query = query.where(Device.owner == actor.id)
            result = remove_devices(session, list(session.scalars(query)))
        log(actor.id, "inactive_devices_deleted", status=str(result["devices"]))
        return result

    @app.get("/api/hosts/me/ready")
    async def host_ready(request: Request):
        host = device_request(request, "host")
        context = ssl.create_default_context(cafile=str(pki.home / "ca.crt"))
        context.load_cert_chain(str(pki.home / "gateway.crt"), str(pki.home / "gateway.key"))
        try:
            async with httpx.AsyncClient(verify=context, trust_env=False, timeout=3) as client:
                response = await client.get(
                    f"https://127.0.0.1:{host.port}/healthz",
                    extensions={"sni_hostname": f"{host.id}.mcp.internal"},
                )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict) or payload.get("device_id") != host.id:
                    raise ValueError("Host adapter identity mismatch")
        except (httpx.HTTPError, ValueError) as error:
            raise HTTPException(503, "Host tunnel or TLS adapter is not ready") from error
        return {"ready": True}

    @app.put("/api/hosts/me/servers/{sid}")
    async def publish(sid: str, request: Request):
        host = device_request(request, "host")
        body = json.loads(await limited_body(request, 32768))
        if not sid.startswith("srv_") or len(sid) > 64 or not sid.replace("_", "").isalnum():
            raise HTTPException(400, "Invalid service ID")
        if body.get("transport") not in {"stdio", "http", "sse"}:
            raise HTTPException(400, "Unsupported transport")
        with db.begin() as session:
            service = session.get(Service, sid)
            if service and service.host_id != host.id:
                raise HTTPException(403, "Service belongs to another host")
            if not service:
                service = Service(id=sid, host_id=host.id, revision=0)
                session.add(service)
            service.name = str(body.get("name", sid))[:128]
            service.description = str(body.get("description", ""))[:1000]
            service.transport = body["transport"]
            service.enabled = bool(body.get("enabled", True))
            service.capabilities = body.get("capabilities", {})
            service.revision += 1
        return {"id": sid, "revision": service.revision}

    @app.get("/api/servers")
    async def server_list(request: Request):
        user = user_session(request)
        with db() as session:
            result = []
            for service in session.scalars(select(Service)):
                host = session.get(Device, service.host_id)
                if user.admin or host.owner == user.id:
                    result.append(
                        {
                            "id": service.id,
                            "name": service.name,
                            "host_id": host.id,
                            "enabled": service.enabled,
                            "transport": service.transport,
                            "online": host.active and host.last_seen > time.time() - 40,
                        }
                    )
            return result

    @app.get("/api/catalog")
    async def catalog(request: Request):
        device = device_request(request, "client")
        result = []
        with db() as session:
            for service in session.scalars(select(Service).where(Service.enabled.is_(True))):
                try:
                    _, host, policy = can_use(session, device.owner, device.id, service.id)
                except HTTPException:
                    continue
                result.append(
                    {
                        "id": service.id,
                        "name": service.name,
                        "description": service.description,
                        "transport": service.transport,
                        "revision": service.revision,
                        "online": host.last_seen > time.time() - 40,
                        "policy": policy,
                    }
                )
        return {"servers": result, "revision": digest(json.dumps(result, sort_keys=True))}

    @app.post("/api/access-token")
    async def resource_token(request: Request):
        device = device_request(request, "client")
        sid = (await request.json()).get("server_id", "")
        with db() as session:
            can_use(session, device.owner, device.id, sid)
        return {
            "access_token": signer.issue(device.id, public + "/mcp/" + sid, kind="device", uid=device.owner),
            "expires_in": 300,
        }

    @app.get("/api/grants")
    async def grants(request: Request):
        user = user_session(request)
        with db() as session:
            result = []
            for grant in session.scalars(select(Grant)):
                service = session.get(Service, grant.server_id)
                host = session.get(Device, service.host_id) if service else None
                if user.admin or (host and host.owner == user.id):
                    result.append(
                        {
                            "id": grant.id,
                            "user_id": grant.user_id,
                            "device_id": grant.device_id,
                            "server_id": grant.server_id,
                            "policy": grant.policy,
                        }
                    )
            return result

    @app.post("/api/grants")
    async def grant_service(request: Request):
        user = user_session(request)
        body = await request.json()
        with db.begin() as session:
            service = session.get(Service, body.get("server_id", ""))
            host = session.get(Device, service.host_id) if service else None
            if not host or (host.owner != user.id and not user.admin):
                raise HTTPException(403, "Only the owner or administrator can share this service")
            recipient = session.get(User, body.get("user_id", ""))
            if not recipient or not recipient.active:
                raise HTTPException(400, "Invalid recipient")
            did = body.get("device_id") or None
            if did:
                target = active_device(session, did, "client")
                if target.owner != recipient.id:
                    raise HTTPException(400, "Device belongs to another user")
            policy = body.get("policy", {})
            if not isinstance(policy, dict) or any(
                k not in {"methods", "tools", "resources", "prompts"}
                or not isinstance(v, list)
                or any(not isinstance(x, str) for x in v)
                for k, v in policy.items()
            ):
                raise HTTPException(400, "Invalid policy")
            grant = Grant(
                id=new_id("g"), user_id=recipient.id, device_id=did, server_id=service.id, policy=policy
            )
            session.add(grant)
        log(user.id, "grant_created", grant.id)
        return {"id": grant.id}

    @app.delete("/api/grants/{gid}")
    async def remove_grant(gid: str, request: Request):
        user = user_session(request)
        with db.begin() as session:
            grant = session.get(Grant, gid)
            service = session.get(Service, grant.server_id) if grant else None
            host = session.get(Device, service.host_id) if service else None
            if not grant or not host or (host.owner != user.id and not user.admin):
                raise HTTPException(404, "Grant not found")
            session.delete(grant)
        log(user.id, "grant_revoked", gid)
        return {"ok": True}

    @app.get("/api/audit")
    async def audit(request: Request):
        user = user_session(request)
        with db() as session:
            query = select(Audit).order_by(Audit.at.desc()).limit(200)
            if not user.admin:
                query = query.where(Audit.actor == user.id)
            return [
                {"at": a.at, "actor": a.actor, "action": a.action, "target": a.target, "status": a.status}
                for a in session.scalars(query)
            ]

    @app.get("/.well-known/openid-configuration")
    @app.get("/.well-known/oauth-authorization-server")
    async def authorization_metadata():
        return {
            "issuer": public,
            "jwks_uri": public + "/oauth/jwks",
            "authorization_endpoint": public + "/oauth/authorize",
            "token_endpoint": public + "/oauth/token",
            "response_types_supported": ["code"],
            "subject_types_supported": ["public"],
            "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code", "refresh_token", "client_credentials"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "scopes_supported": ["mcp:invoke"],
        }

    @app.get("/oauth/jwks")
    async def jwks():
        return signer.jwks()

    @app.get("/.well-known/oauth-protected-resource/mcp/{sid}")
    async def resource_metadata(sid: str):
        return {
            "resource": public + "/mcp/" + sid,
            "authorization_servers": [public],
            "scopes_supported": ["mcp:invoke"],
            "bearer_methods_supported": ["header"],
        }

    def validate_oauth(data: dict):
        redirect = urlsplit(data.get("redirect_uri", ""))
        resource = data.get("resource", "")
        if data.get("client_id") != "mcp-transfer" or data.get("code_challenge_method") != "S256":
            raise HTTPException(400, "Use pre-registered client mcp-transfer with PKCE S256")
        if (
            redirect.scheme != "http"
            or redirect.hostname not in {"127.0.0.1", "::1"}
            or redirect.path != "/oauth/callback"
            or redirect.query
            or redirect.fragment
            or redirect.username
            or redirect.password
        ):
            raise HTTPException(400, "Only the registered loopback callback is supported")
        challenge = data.get("code_challenge", "")
        if len(challenge) != 43 or not all(c.isalnum() or c in "-_" for c in challenge):
            raise HTTPException(400, "Invalid PKCE challenge")
        if not resource.startswith(public + "/mcp/"):
            raise HTTPException(400, "Invalid resource")
        return resource.removeprefix(public + "/mcp/")

    @app.post("/api/oauth/approve")
    async def approve_oauth(request: Request):
        user = user_session(request)
        body = await request.json()
        sid = validate_oauth(body)
        code = secrets.token_urlsafe(32)
        with db.begin() as session:
            can_use(session, user.id, "oauth", sid)
            session.add(
                OAuthCode(
                    hash=digest(code),
                    user_id=user.id,
                    challenge=body["code_challenge"],
                    redirect_uri=body["redirect_uri"],
                    resource=body["resource"],
                    expires=time.time() + 60,
                )
            )
        return {
            "redirect": body["redirect_uri"]
            + "?"
            + urlencode({"code": code, "state": body.get("state", ""), "iss": public})
        }

    @app.post("/oauth/token")
    async def oauth_token(request: Request):
        rate.check("oauth:" + client_ip(request), 120)
        form = dict(await request.form())
        grant_type = form.get("grant_type")
        if grant_type == "client_credentials":
            client_id, secret = form.get("client_id", ""), form.get("client_secret", "")
            authorization = request.headers.get("authorization", "")
            if authorization.startswith("Basic "):
                try:
                    from urllib.parse import unquote_plus

                    client_id, secret = [
                        unquote_plus(x)
                        for x in base64.b64decode(authorization[6:], validate=True).decode().split(":", 1)
                    ]
                except (ValueError, UnicodeDecodeError) as error:
                    raise HTTPException(401, "Invalid client authentication") from error
            with db() as session:
                device = active_device(session, client_id, "host")
                if not secrets.compare_digest(device.frp_secret or "", digest(secret)):
                    raise HTTPException(401, "Invalid host credential")
            return {
                "access_token": signer.issue(device.id, "frp-register", ttl=300, kind="frp"),
                "token_type": "Bearer",
                "expires_in": 300,
            }
        if grant_type == "refresh_token":
            return refresh_device(str(form.get("refresh_token", "")), form.get("resource"))
        if grant_type != "authorization_code" or form.get("client_id") != "mcp-transfer":
            raise HTTPException(400, "Unsupported OAuth grant")
        challenge = (
            base64.urlsafe_b64encode(bytes.fromhex(digest(str(form.get("code_verifier", "")))))
            .rstrip(b"=")
            .decode()
        )
        refresh = secrets.token_urlsafe(40)
        with db.begin() as session:
            code = session.get(OAuthCode, digest(str(form.get("code", ""))))
            if (
                not code
                or code.used
                or code.expires < time.time()
                or code.challenge != challenge
                or code.redirect_uri != form.get("redirect_uri")
                or code.resource != form.get("resource")
            ):
                raise HTTPException(400, "Invalid authorization code, resource or verifier")
            user = session.get(User, code.user_id)
            if not user or not user.active:
                raise HTTPException(403, "User disabled")
            result = session.execute(
                update(OAuthCode)
                .where(OAuthCode.hash == code.hash, OAuthCode.used.is_(False))
                .values(used=True)
            )
            if result.rowcount != 1:
                raise HTTPException(400, "Authorization code already used")
            device = Device(
                id=new_id("d"),
                owner=code.user_id,
                name="OAuth application",
                role="client",
                refresh_hash=digest(refresh),
                refresh_expires=time.time() + 30 * 86400,
            )
            session.add(device)
            resource = code.resource
        return {
            "access_token": signer.issue(device.id, resource, kind="device", uid=device.owner),
            "refresh_token": refresh,
            "token_type": "Bearer",
            "expires_in": 300,
        }

    @app.api_route("/mcp/{sid}", methods=["GET", "POST", "DELETE"])
    async def proxy(sid: str, request: Request):
        try:
            claims = signer.verify(bearer(request), public + "/mcp/" + sid)
        except HTTPException as error:
            raise HTTPException(
                error.status_code,
                error.detail,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{public}/.well-known/oauth-protected-resource/mcp/{sid}"'
                },
            ) from error
        with db() as session:
            device = active_device(session, claims["sub"], "client")
            service, host, policy = can_use(session, device.owner, device.id, sid)
        body = await limited_body(request)
        message = parse_message(body)
        authorize_message(message, policy)
        version = request.headers.get("mcp-protocol-version", "")
        if version == "2026-07-28" and request.method != "POST":
            raise HTTPException(405, "This MCP revision uses POST")
        session_id = request.headers.get("mcp-session-id")
        binding = (device.owner, device.id)
        if session_id:
            existing = sessions.get((sid, session_id))
            if not existing or existing[:2] != binding or existing[2] < time.time():
                raise HTTPException(404, "Unknown MCP session")
            sessions[(sid, session_id)] = (*binding, time.time() + 3600)
        if message and "method" not in message and not session_id:
            raise HTTPException(400, "An existing session is required for client responses")
        if inflight.get(device.id, 0) >= 32:
            raise HTTPException(429, "Device concurrent request limit reached")
        inflight[device.id] = inflight.get(device.id, 0) + 1
        connection = request.headers.get("x-mcpt-connection", "default")
        if len(connection) > 64 or not connection.replace("-", "").isalnum():
            raise HTTPException(400, "Invalid connection identifier")
        ticket = signer.issue(
            device.id,
            host.id,
            ttl=30,
            kind="invoke",
            uid=device.owner,
            sid=sid,
            method=request.method,
            body_hash=digest(body),
            policy=policy,
            connection=connection,
            lease_until=min(int(time.time()) + 300, claims["exp"]),
        )
        context = ssl.create_default_context(cafile=str(pki.home / "ca.crt"))
        context.load_cert_chain(str(pki.home / "gateway.crt"), str(pki.home / "gateway.key"))
        client = httpx.AsyncClient(
            verify=context, trust_env=False, timeout=httpx.Timeout(30, read=None), follow_redirects=False
        )

        async def cleanup():
            inflight[device.id] = max(0, inflight.get(device.id, 1) - 1)
            await client.aclose()

        async def alive():
            if claims["exp"] <= time.time():
                return False
            try:
                with db() as session:
                    current = active_device(session, device.id, "client")
                    _, _, current_policy = can_use(session, current.owner, current.id, sid)
                    authorize_message(message, current_policy)
                return current_policy == policy
            except Exception:
                return False

        headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
        headers["authorization"] = "Bearer " + ticket
        try:
            upstream_request = client.build_request(
                request.method,
                f"https://127.0.0.1:{host.port}/mcp/{sid}",
                content=body,
                headers=headers,
                extensions={"sni_hostname": f"{host.id}.mcp.internal"},
            )
            response = await client.send(upstream_request, stream=True)
        except (httpx.HTTPError, OSError):
            await cleanup()
            log(device.owner, message.get("method", request.method), sid, "upstream_unavailable")
            raise HTTPException(
                503, "Host tunnel is unavailable; no tool call was automatically replayed"
            ) from None
        upstream_session = response.headers.get("mcp-session-id")
        if upstream_session and message.get("method") == "initialize":
            key = (sid, upstream_session)
            previous = sessions.get(key)
            if previous and previous[:2] != binding:
                await response.aclose()
                await cleanup()
                raise HTTPException(502, "Upstream reused a session across identities")
            sessions[key] = (*binding, time.time() + 3600)
        if request.method == "DELETE" and session_id:
            sessions.pop((sid, session_id), None)
        if len(sessions) > 10000:
            for key, value in list(sessions.items()):
                if value[2] < time.time():
                    sessions.pop(key, None)
        log(device.owner, message.get("method", request.method), sid, str(response.status_code))
        return await relay_response(response, cleanup, alive, message, policy)

    return app


def create_frp_plugin(settings: Settings) -> FastAPI:
    """Served by a separate loopback-only listener, never mounted on the public app."""
    db = database(settings.database_url)
    signer = Signer(settings.home, settings.cloud_url.rstrip("/"))
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.post("/internal/frp")
    async def handler(request: Request):
        op = request.query_params.get("op")
        data = (await request.json()).get("content", {})
        user = data.get("user")
        identity = user if isinstance(user, str) else (user or {}).get("user", "")
        try:
            with db() as session:
                device = session.get(Device, identity)
                owner = session.get(User, device.owner) if device else None
                if not device or not device.active or device.role != "host" or not owner or not owner.active:
                    raise ValueError("Host revoked")
                if op in {"Login", "NewWorkConn", "Ping"}:
                    token = signer.verify(data.get("privilege_key", ""), "frp-register")
                    if token["sub"] != identity or token.get("kind") != "frp":
                        raise ValueError("Host identity mismatch")
                if op in {"NewProxy", "NewUserConn"}:
                    if data.get("proxy_type") != "tcp" or data.get("proxy_name") != identity + ".adapter":
                        raise ValueError("Proxy name/type not allocated")
                if op == "NewProxy" and data.get("remote_port") != device.port:
                    raise ValueError("Port not allocated to this host")
                if op not in {"Login", "NewProxy", "Ping", "NewWorkConn", "NewUserConn"}:
                    raise ValueError("Unsupported hook")
        except Exception:
            return {"reject": True, "reject_reason": "Host authentication or tunnel policy denied"}
        return {"reject": False, "unchange": True}

    return app
