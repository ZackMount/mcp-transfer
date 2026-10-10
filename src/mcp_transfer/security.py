from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from .config import atomic_write

passwords = PasswordHasher()


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def new_id(prefix: str) -> str:
    return prefix + "_" + secrets.token_hex(12)


def verify_password(encoded: str, candidate: str) -> bool:
    try:
        return passwords.verify(encoded, candidate)
    except (VerificationError, ValueError):
        return False


class Signer:
    def __init__(self, home: Path, issuer: str):
        path = home / "signing.key"
        if not path.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
            atomic_write(
                path,
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
        self.private = path.read_bytes()
        self.public = serialization.load_pem_private_key(self.private, None).public_key()
        self.public_pem = self.public.public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()
        self.issuer = issuer
        self.kid = digest(self.public_pem)[:16]

    def issue(self, subject: str, audience: str, ttl: int = 300, **claims) -> str:
        now = int(time.time())
        payload = {
            **claims,
            "iss": self.issuer,
            "sub": subject,
            "aud": audience,
            "iat": now,
            "nbf": now - 3,
            "exp": now + ttl,
            "jti": new_id("tok"),
        }
        return jwt.encode(payload, self.private, algorithm="RS256", headers={"kid": self.kid})

    def verify(self, token: str, audience: str) -> dict:
        return verify_token(token, self.public_pem, self.issuer, audience)

    def jwks(self) -> dict:
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.public))
        return {"keys": [{**key, "kid": self.kid, "use": "sig", "alg": "RS256"}]}


def verify_token(token: str, public: str, issuer: str, audience: str) -> dict:
    try:
        return jwt.decode(
            token,
            public,
            algorithms=["RS256"],
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub", "jti"]},
        )
    except jwt.PyJWTError as error:
        raise HTTPException(401, "Invalid or expired credential") from error


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise HTTPException(401, "Bearer credential required", headers={"WWW-Authenticate": "Bearer"})
    return header[7:]


class RateLimiter:
    def __init__(self):
        self.events: dict[str, deque] = defaultdict(deque)

    def check(self, key: str, limit: int = 20, window: int = 60):
        now = time.monotonic()
        events = self.events[key]
        while events and events[0] < now - window:
            events.popleft()
        if len(events) >= limit:
            raise HTTPException(
                429, "Too many attempts; try again later", headers={"Retry-After": str(window)}
            )
        events.append(now)
        if len(self.events) > 10000:
            self.events = defaultdict(
                deque, {k: v for k, v in self.events.items() if v and v[-1] > now - window}
            )


class BoundaryMiddleware(BaseHTTPMiddleware):
    """Exact origin/host allowlists protect both loopback and cloud surfaces."""

    def __init__(self, app, origins: set[str], hosts: set[str]):
        super().__init__(app)
        self.origins = origins
        self.hosts = hosts

    async def dispatch(self, request: Request, call_next):
        if request.headers.get("host", "") not in self.hosts:
            return JSONResponse({"detail": "Invalid Host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and origin not in self.origins:
            return JSONResponse({"detail": "Invalid Origin"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        response.headers["Cache-Control"] = "no-store"
        return response


def require_csrf(request: Request, expected: str):
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not hmac.compare_digest(
        request.headers.get("x-csrf-token", ""), expected
    ):
        raise HTTPException(403, "CSRF token required")


async def limited_body(request: Request, limit: int = 2 * 1024 * 1024) -> bytes:
    parts = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "MCP message exceeds size limit")
        parts.append(chunk)
    return b"".join(parts)
