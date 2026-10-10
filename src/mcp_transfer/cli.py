from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import ssl
import sys
import uuid
from pathlib import Path

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.stdio import stdio_server
from sqlalchemy import delete, select

from .agent import Agent, HostAdapter, create_agent_app
from .cloud import create_cloud, create_frp_plugin
from .config import Settings, default_home, validate_cloud_url
from .database import OAuthCode, User, WebSession, database
from .protocol import proxy_server
from .security import new_id, passwords
from .vault import Vault


def initialize_cloud(
    settings: Settings,
    public_url: str,
    username: str = "admin",
    password: str | None = None,
    development=False,
    allow_insecure_http=False,
    frp_port: int | None = None,
    migrate_origin=False,
):
    from .frp import write_frps

    settings.home.mkdir(parents=True, exist_ok=True, mode=0o700)
    public_url = validate_cloud_url(public_url, development, allow_insecure_http)
    origin_changed = bool(settings.data.get("cloud_url") and settings.data["cloud_url"] != public_url)
    if origin_changed and not migrate_origin:
        raise ValueError(
            "Changing the public origin requires --migrate-origin; then update both agents' cloud URLs"
        )
    if frp_port is not None:
        if not 1 <= frp_port <= 65535:
            raise ValueError("frp port must be between 1 and 65535")
        settings.data["frp_port"] = frp_port
    settings.data.update(
        role="cloud", cloud_url=public_url, development=development, allow_insecure_http=allow_insecure_http
    )
    settings.save()
    db = database(settings.database_url)
    generated = None
    with db.begin() as session:
        if origin_changed:
            session.execute(delete(WebSession))
            session.execute(delete(OAuthCode))
        if not session.scalar(select(User).limit(1)):
            generated = password or secrets.token_urlsafe(24)
            if len(generated) < 12:
                raise ValueError("Administrator password must have at least 12 characters")
            session.add(
                User(id=new_id("u"), username=username, password=passwords.hash(generated), admin=True)
            )
    create_cloud(settings)
    write_frps(settings)
    return generated


async def run_cloud(settings: Settings, no_frp=False):
    from .frp import FrpProcess, write_frps

    if settings.data.get("role") != "cloud":
        raise ValueError("Run cloud-init before starting the cloud service")
    if settings.cloud_url != settings.data.get("cloud_url"):
        raise ValueError(
            "Configured origin differs from saved identity; run cloud-init --migrate-origin first"
        )
    write_frps(settings)
    main = uvicorn.Server(
        uvicorn.Config(
            create_cloud(settings),
            host="127.0.0.1",
            port=17800,
            access_log=False,
            proxy_headers=True,
            forwarded_allow_ips="127.0.0.1",
        )
    )
    plugin = uvicorn.Server(
        uvicorn.Config(
            create_frp_plugin(settings),
            host="127.0.0.1",
            port=int(settings.data.get("plugin_port", 17900)),
            access_log=False,
            proxy_headers=False,
        )
    )
    frp = FrpProcess(settings, "frps")

    async def supervise():
        while not main.should_exit:
            if not no_frp:
                try:
                    await frp.start()
                except Exception as error:
                    logging.warning("frps unavailable: %s", type(error).__name__)
            await asyncio.sleep(3)
        plugin.should_exit = True

    jobs = [
        asyncio.create_task(main.serve()),
        asyncio.create_task(plugin.serve()),
        asyncio.create_task(supervise()),
    ]
    try:
        await jobs[0]
    finally:
        plugin.should_exit = True
        jobs[2].cancel()
        await frp.stop()
        await asyncio.gather(*jobs[1:], return_exceptions=True)


async def run_agent(settings: Settings, role: str, no_frp=False):
    existing = settings.data.get("role")
    if existing and existing != role:
        raise ValueError("A profile cannot be used as both host and client; select another --home")
    settings.data["role"] = role
    settings.data["disable_frp"] = no_frp
    settings.save()
    agent = Agent(settings, role)
    web = uvicorn.Server(
        uvicorn.Config(
            create_agent_app(agent), host="127.0.0.1", port=agent.port, access_log=False, proxy_headers=False
        )
    )
    adapter_server = None
    adapter_task = None

    async def supervise_adapter():
        nonlocal adapter_server, adapter_task
        revision = None
        while not web.should_exit:
            cert = settings.home / "device.crt"
            current = cert.stat().st_mtime_ns if cert.exists() else None
            if role == "host" and current and current != revision:
                if adapter_server:
                    adapter_server.should_exit = True
                    await adapter_task
                adapter_server = uvicorn.Server(
                    uvicorn.Config(
                        HostAdapter(agent),
                        host="127.0.0.1",
                        port=int(settings.data.get("adapter_port", 17910)),
                        lifespan="off",
                        access_log=False,
                        proxy_headers=False,
                        ssl_keyfile=str(settings.home / "device.key"),
                        ssl_certfile=str(cert),
                        ssl_ca_certs=str(settings.home / "ca.crt"),
                        ssl_cert_reqs=ssl.CERT_REQUIRED,
                    )
                )
                adapter_task = asyncio.create_task(adapter_server.serve())
                revision = current
            await asyncio.sleep(2)

    monitor = asyncio.create_task(supervise_adapter())
    print(f"WebUI: http://127.0.0.1:{agent.port}", file=sys.stderr)
    print(
        "Retrieve the local access password using: mcp-transfer --home <profile-directory> local-password",
        file=sys.stderr,
    )
    try:
        await web.serve()
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        if adapter_server:
            adapter_server.should_exit = True
            await adapter_task


async def bridge(settings: Settings, sid: str):
    mapping = Vault(settings.home).read().get("mappings", {}).get(sid)
    if not mapping or not mapping.get("enabled", True):
        raise ValueError("Server is not mapped. Enable it in the Client WebUI first.")
    port = int(settings.data.get("ui_port", 17820))
    headers = {"Authorization": "Bearer " + mapping["token"], "X-MCPT-Connection": uuid.uuid4().hex}
    async with httpx2.AsyncClient(
        headers=headers, trust_env=False, follow_redirects=False, timeout=httpx2.Timeout(30, read=None)
    ) as http:
        transport = streamable_http_client(f"http://127.0.0.1:{port}/mcp/{sid}", http_client=http)
        async with Client(transport, cache=None) as client:
            server = proxy_server(client, "MCP Transfer " + sid)
            async with stdio_server() as (read, write):
                await server.run(read, write, server.create_initialization_options())


def main():
    parser = argparse.ArgumentParser(description="MCP Transfer cloud / host / client")
    parser.add_argument("--home", type=Path, default=default_home())
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("cloud-init")
    init.add_argument("--public-url", required=True)
    init.add_argument("--username", default="admin")
    init.add_argument("--development", action="store_true")
    init.add_argument("--allow-insecure-http", action="store_true")
    init.add_argument("--frp-port", type=int)
    init.add_argument("--migrate-origin", action="store_true")
    cloud = sub.add_parser("cloud")
    cloud.add_argument("--no-frp", action="store_true")
    for role in ("host", "client"):
        cmd = sub.add_parser(role)
        cmd.add_argument("--port", type=int)
        cmd.add_argument("--development", action="store_true")
        cmd.add_argument("--allow-insecure-http", action="store_true", default=None)
        cmd.add_argument("--no-frp", action="store_true")
    connect = sub.add_parser("connect")
    connect.add_argument("--server", required=True)
    install = sub.add_parser("install-frp")
    install.add_argument("--binary", choices=["frpc", "frps"], required=True)
    sub.add_parser("local-password")
    sub.add_parser("doctor")
    sub.add_parser("demo-server")
    args = parser.parse_args()
    settings = Settings(args.home)
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    try:
        if args.command == "cloud-init":
            password = initialize_cloud(
                settings,
                args.public_url,
                args.username,
                os.environ.get("MCPT_ADMIN_PASSWORD"),
                args.development,
                allow_insecure_http=args.allow_insecure_http,
                frp_port=args.frp_port,
                migrate_origin=args.migrate_origin,
            )
            print("Cloud initialized: " + args.public_url)
            if password:
                print("Administrator: " + args.username)
                print("Initial password (save securely): " + password)
        elif args.command == "cloud":
            asyncio.run(run_cloud(settings, args.no_frp))
        elif args.command in {"host", "client"}:
            if args.port:
                if not 1024 <= args.port <= 65535:
                    raise ValueError("Port must be 1024–65535")
                settings.data["ui_port"] = args.port
            settings.data["development"] = args.development
            if args.allow_insecure_http is not None:
                settings.data["allow_insecure_http"] = args.allow_insecure_http
            settings.save()
            asyncio.run(run_agent(settings, args.command, args.no_frp))
        elif args.command == "connect":
            asyncio.run(bridge(settings, args.server))
        elif args.command == "local-password":
            agent = Agent(settings, settings.data.get("role", "client"))
            print(agent.vault.read()["local_password"])
        elif args.command == "install-frp":
            if settings.data.get("role") == "client":
                raise ValueError("Client profiles do not support installing or running frp")
            from .frp import download_frp

            print(asyncio.run(download_frp(settings.home, args.binary)))
        elif args.command == "doctor":
            print(
                json.dumps(
                    {
                        "python": sys.version,
                        "role": settings.data.get("role"),
                        "enrolled": bool(settings.data.get("device_id")),
                        "cloud_url": settings.data.get("cloud_url"),
                        "home": str(settings.home),
                    },
                    indent=2,
                )
            )
        elif args.command == "demo-server":
            from .demo import main as demo

            demo()
    except KeyboardInterrupt:
        pass
    except Exception as error:
        print(f"MCP Transfer: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
