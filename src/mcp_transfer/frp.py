from __future__ import annotations

import asyncio
import hashlib
import io
import json
import platform
import tarfile
import zipfile
from pathlib import Path

import httpx

from .config import Settings, atomic_write

VERSION = "0.68.0"


async def download_frp(home: Path, binary: str) -> Path:
    system = {"Windows": "windows", "Darwin": "darwin", "Linux": "linux"}.get(platform.system())
    architecture = {
        "x86_64": "amd64",
        "AMD64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
        "ARM64": "arm64",
    }.get(platform.machine())
    if not system or not architecture:
        raise RuntimeError("Unsupported frp platform")
    executable = home / "bin" / (binary + (".exe" if system == "windows" else ""))
    if executable.exists():
        return executable
    extension = "zip" if system == "windows" else "tar.gz"
    archive = f"frp_{VERSION}_{system}_{architecture}.{extension}"
    release = f"https://github.com/fatedier/frp/releases/download/v{VERSION}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
        checksums = await client.get(release + "/frp_sha256_checksums.txt")
        checksums.raise_for_status()
        entries = dict(
            (line.split()[1].lstrip("*"), line.split()[0])
            for line in checksums.text.splitlines()
            if len(line.split()) == 2
        )
        if archive not in entries:
            raise RuntimeError("frp archive is missing from release checksums")
        response = await client.get(release + "/" + archive)
        response.raise_for_status()
    if hashlib.sha256(response.content).hexdigest() != entries[archive]:
        raise RuntimeError("frp checksum mismatch")
    member_name = f"frp_{VERSION}_{system}_{architecture}/{executable.name}"
    if extension == "zip":
        with zipfile.ZipFile(io.BytesIO(response.content)) as package:
            payload = package.read(member_name)
    else:
        with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as package:
            payload = package.extractfile(member_name).read()
    atomic_write(executable, payload, mode=0o700)
    return executable


def q(value):
    return json.dumps(str(value))


def write_frps(settings: Settings):
    directory = settings.home / "pki"
    content = f"""bindAddr = "0.0.0.0"
bindPort = {int(settings.data.get("frp_port", 7000))}
proxyBindAddr = "127.0.0.1"
allowPorts = [{{ start = 21000, end = 21999 }}]
maxPortsPerClient = 1
transport.tls.force = true
transport.tls.certFile = {q(directory / "frps.crt")}
transport.tls.keyFile = {q(directory / "frps.key")}
transport.tls.trustedCaFile = {q(directory / "ca.crt")}
auth.method = "oidc"
auth.oidc.issuer = {q(settings.cloud_url)}
auth.oidc.audience = "frp-register"
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]
log.to = "console"
log.level = "warn"

[[httpPlugins]]
name = "mcp-transfer-policy"
addr = "127.0.0.1:{int(settings.data.get("plugin_port", 17900))}"
path = "/internal/frp"
ops = ["Login", "NewProxy", "Ping", "NewWorkConn", "NewUserConn"]
"""
    atomic_write(settings.home / "frps.toml", content)


def write_frpc(settings: Settings, secrets: dict):
    device = settings.data
    content = f"""serverAddr = {q(device["frp_server"])}
serverPort = {int(device["frp_port"])}
user = {q(device["device_id"])}
loginFailExit = false
transport.tls.enable = true
transport.tls.serverName = {q(device["frp_server"])}
transport.tls.trustedCaFile = {q(settings.home / "ca.crt")}
transport.tls.certFile = {q(settings.home / "device.crt")}
transport.tls.keyFile = {q(settings.home / "device.key")}
auth.method = "oidc"
auth.oidc.clientID = {q(device["device_id"])}
auth.oidc.clientSecret = {q(secrets["frp_secret"])}
auth.oidc.audience = "frp-register"
auth.oidc.tokenEndpointURL = {q(device["cloud_url"] + "/oauth/token")}
auth.additionalScopes = ["HeartBeats", "NewWorkConns"]
log.to = "console"
log.level = "warn"

[[proxies]]
name = "adapter"
type = "tcp"
localIP = "127.0.0.1"
localPort = {int(device.get("adapter_port", 17910))}
remotePort = {int(device["remote_port"])}
"""
    atomic_write(settings.home / "frpc.toml", content)


class FrpProcess:
    def __init__(self, settings: Settings, binary: str):
        self.settings = settings
        self.binary = binary
        self.process = None
        self.log = None
        self.error = None

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        executable = await download_frp(self.settings.home, self.binary)
        if self.log:
            self.log.close()
        self.log = open(self.settings.home / f"{self.binary}.log", "ab", buffering=0)
        self.process = await asyncio.create_subprocess_exec(
            str(executable),
            "-c",
            str(self.settings.home / f"{self.binary}.toml"),
            stdout=self.log,
            stderr=self.log,
        )

    async def stop(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.log:
            self.log.close()
        self.process = None
