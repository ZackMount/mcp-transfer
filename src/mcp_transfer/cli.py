from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import ssl
import sys
from pathlib import Path

import uvicorn
from mcp.server.stdio import stdio_server
from sqlalchemy import delete, select

from .agent import Agent, HostAdapter, create_agent_app
from .cloud import create_cloud, create_frp_plugin
from .config import Settings, default_home, validate_cloud_url
from .database import OAuthCode, User, WebSession, database
from .protocol import mapped_client, proxy_server
from .runtime_lock import profile_lock
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


async def run_cloud(settings: Settings, no_frp=False, bind_host="127.0.0.1", port=17800):
    from .frp import FrpProcess, write_frps

    if settings.data.get("role") != "cloud":
        raise ValueError("Run cloud-init before starting the cloud service")
    if settings.cloud_url != settings.data.get("cloud_url"):
        raise ValueError(
            "Configured origin differs from saved identity; run cloud-init --migrate-origin first"
        )
    if bind_host not in {"127.0.0.1", "localhost", "::1"}:
        if not settings.cloud_url.startswith("http://") or not settings.data.get("allow_insecure_http"):
            raise ValueError("A public listener requires an explicitly configured HTTP deployment")
    if not 1 <= port <= 65535:
        raise ValueError("Cloud listener port must be between 1 and 65535")
    write_frps(settings)
    main = uvicorn.Server(
        uvicorn.Config(
            create_cloud(settings),
            host=bind_host,
            port=port,
            access_log=False,
            proxy_headers=bind_host in {"127.0.0.1", "localhost", "::1"},
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
    # Bind before the ASGI lifespan starts the heartbeat/refresh task. A stale
    # process occupying the port must not cause a second credential rotation.
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", int(settings.data.get("ui_port", 17810 if role == "host" else 17820))))
        listener.listen(socket.SOMAXCONN)
        listener.setblocking(False)
    except OSError:
        listener.close()
        raise ValueError("The local WebUI port is occupied. Stop the old agent or choose another port.") from None
    agent = Agent(settings, role)
    from .connections import Connections

    connections = Connections(agent)
    web = uvicorn.Server(
        uvicorn.Config(
            create_agent_app(agent, connections=connections), host="127.0.0.1", port=agent.port, access_log=False, proxy_headers=False
        )
    )
    adapters = {}

    async def supervise_adapter():
        while not web.should_exit:
            for cid in list(adapters):
                if cid not in connections.agents:
                    server, task, _ = adapters.pop(cid)
                    server.should_exit = True
                    await task
            for cid, current_agent in list(connections.agents.items()):
                profile = current_agent.settings
                cert = profile.home / "device.crt"
                current = cert.stat().st_mtime_ns if cert.exists() else None
                previous = adapters.get(cid)
                if role != "host" or not current or (previous and current == previous[2]):
                    continue
                if previous:
                    previous[0].should_exit = True
                    await previous[1]
                server = uvicorn.Server(
                    uvicorn.Config(
                        HostAdapter(current_agent),
                        host="127.0.0.1",
                        port=int(profile.data.get("adapter_port", 17910)),
                        lifespan="off",
                        access_log=False,
                        proxy_headers=False,
                        ssl_keyfile=str(profile.home / "device.key"),
                        ssl_certfile=str(cert),
                        ssl_ca_certs=str(profile.home / "ca.crt"),
                        ssl_cert_reqs=ssl.CERT_REQUIRED,
                    )
                )
                adapters[cid] = server, asyncio.create_task(server.serve()), current
            await asyncio.sleep(2)

    monitor = asyncio.create_task(supervise_adapter())
    print(f"WebUI: http://127.0.0.1:{agent.port}", file=sys.stderr)
    print(
        "Retrieve the local access password using: mcp-transfer --home <profile-directory> local-password",
        file=sys.stderr,
    )
    try:
        await web.serve(sockets=[listener])
    finally:
        monitor.cancel()
        await asyncio.gather(monitor, return_exceptions=True)
        for server, _, _ in adapters.values():
            server.should_exit = True
        await asyncio.gather(*(task for _, task, _ in adapters.values()), return_exceptions=True)
        listener.close()


async def bridge(settings: Settings, sid: str):
    mapping = Vault(settings.home).read().get("mappings", {}).get(sid)
    if not mapping or not mapping.get("enabled", True):
        raise ValueError("Server is not mapped. Enable it in the Client WebUI first.")
    port = int(settings.data.get("ui_port", 17820))
    async with mapped_client(port, sid, mapping["token"], settings.data.get("route_prefix", "")) as client:
        upstream_name = client.server_info.name if client.server_info else sid
        server = proxy_server(client, upstream_name)
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
    cloud.add_argument("--bind-host", default="127.0.0.1")
    cloud.add_argument("--port", type=int, default=17800)
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
    sub.add_parser("clean-exports")
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
            with profile_lock(settings.home):
                asyncio.run(run_cloud(settings, args.no_frp, args.bind_host, args.port))
        elif args.command in {"host", "client"}:
            with profile_lock(settings.home):
                if settings.data.get("role") not in {None, args.command}:
                    raise ValueError("A profile cannot be used as both host and client")
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
        elif args.command == "clean-exports":
            from .exporters import clean_exports

            print(json.dumps(clean_exports(settings.home), ensure_ascii=False, indent=2))
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
