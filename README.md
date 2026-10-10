# MCP Transfer

自托管的 MCP 服务共享平台：**Linux 云端 + Windows 主机端 + Windows 客户端**，三端均提供 WebUI。

主机端将本机 HTTP、stdio 或旧 SSE MCP 接入平台，frp 只把主机适配入口映射到云服务器的 loopback。公司客户端只向云端发起 HTTPS 请求，再向本机 AI 应用提供 MCP 接口；不使用 VPN、虚拟网卡、Tailscale 或公司内网入站连接。

界面预览：[云端](docs/cloud-ui.jpg) · [主机端](docs/host-ui.jpg) · [客户端](docs/client-ui.jpg)。截图来自本地验证环境，不代表已部署到你的云服务器。

```text
AI 应用 → 本机 stdio 桥 / HTTP 映射 → 云端 HTTPS 鉴权网关
                                      ↓
                         frps 的 127.0.0.1 私有映射
                                      ↓ 已建立的反向通道
                              frpc → Host Agent → MCP
```

## 已实现的功能

- 三端中文 WebUI；云端用户管理、TOTP MFA、设备登记/撤销、服务授权和脱敏审计。
- 每台设备独立凭证；5 分钟访问令牌、刷新凭证轮换、接入码单次使用、撤销检查。
- frp 0.68.0 固定版本下载与 SHA-256 核验；逐主机 OIDC 凭证、TLS 双向验证、服务端端口归属插件。
- 网关到主机适配器另有 mTLS 和目标绑定的短期调用票据，云端令牌不会透传到原 MCP。
- HTTP MCP 流式转发；stdio / SSE 使用官方 MCP Python SDK 2.3.0 适配；按用户、客户端和连接隔离适配进程。
- 客户端服务目录同步、批量订阅、单端口多路径映射、后台续期。
- 通用 JSON、Cursor、VS Code、Claude Desktop 和 Codex TOML 配置生成；预览差异、备份、原子写入和可选自动同步。
- stdio 导出不包含云端密钥；HTTP 导出使用独立的本地映射令牌。
- Windows 当前用户后台任务、自启动、DPAPI 凭证保护；云端 Docker Compose、PostgreSQL、Caddy 自动 HTTPS。

## 一行部署

这是私有仓库，因此每台需要下载源代码的机器必须已有 **Git、GitHub CLI** 并通过 `gh auth login` 获得此仓库的读取权限。不要把 GitHub Token 写入部署命令或发送到聊天中。也可将仓库源码通过公司批准的方式复制到目标机器，直接运行安装脚本。

首次部署在目标目录尚不存在的位置执行。Windows 使用普通用户 PowerShell；本机策略需要允许运行已审阅的安装脚本。安装程序不修改 PowerShell 安全策略。

### 云端 · Linux

```bash
gh repo clone ZackMount/mcp-transfer && sudo bash mcp-transfer/scripts/install-cloud.sh
```

安装时输入已解析到云服务器的域名，例如 `mcp.example.com`。也可以预先指定：

```bash
gh repo clone ZackMount/mcp-transfer && sudo bash mcp-transfer/scripts/install-cloud.sh --domain mcp.example.com
```

Ubuntu 24.04 会自动安装 Docker 与 Compose；其他 Linux 发行版需要先安装 Docker Engine 和 Compose v2。该部署使用 Linux host networking，不适用于 Docker Desktop 的默认网络模式。

必须先配置 DNS，并在云安全组 / 防火墙放行：

- **TCP 80、443**：HTTPS 证书申请、管理界面及客户端连接。
- **TCP 7000**：主机 frpc 接入，需要设备认证及客户端证书。
- **不要放行** 17800、17900、15432、21000–21999；它们仅用于云服务器内部通信。

安装结束会显示初始管理员密码，只在首次初始化时生成。访问 `https://你的域名`，登录后启用 MFA。不要在初始化后直接更换公网域名，它同时是令牌 issuer，需要迁移凭证。

### 主机端 · Windows

```powershell
gh repo clone ZackMount/mcp-transfer "$env:LOCALAPPDATA\MCPTransfer-src"; if ($LASTEXITCODE -ne 0) { throw '仓库下载失败' }; & "$env:LOCALAPPDATA\MCPTransfer-src\scripts\install-windows.ps1" -Role host
```

安装程序自动准备 Python 3.12 独立环境、依赖和 frpc，创建当前用户登录时启动的计划任务，显示本地访问密码并打开 `http://127.0.0.1:17810`。没有 Python 时需要 Windows App Installer 提供的 `winget`。

### 客户端 · Windows

```powershell
gh repo clone ZackMount/mcp-transfer "$env:LOCALAPPDATA\MCPTransfer-src"; if ($LASTEXITCODE -ne 0) { throw '仓库下载失败' }; & "$env:LOCALAPPDATA\MCPTransfer-src\scripts\install-windows.ps1" -Role client
```

完成后打开 `http://127.0.0.1:17820`。客户端不安装或运行 frp，不需要开放任何入站端口。同一电脑同时安装两种角色时，第二次直接运行已经下载的安装脚本即可，无需再次 clone。

若已将源码复制到目标 Windows 机器，在源码根目录只需：

```powershell
& .\scripts\install-windows.ps1 -Role host
# 另一台客户端使用 -Role client
```

安装目录为 `%LOCALAPPDATA%\MCPTransfer\host` 或 `client`。以后忘记 WebUI 密码，可双击其中的 `show-access-password.cmd`；不会在应用配置中保存该密码。

## 首次使用

1. 云端「设备与接入」选择 **主机**，生成 5 分钟有效的一次性接入码。
2. 主机 WebUI 填云端地址、名称和接入码。添加 MCP 的本机地址或启动命令；通过「测试」检查后发布。导入的定义默认停用，不会自动执行。
3. 云端为使用者创建用户，生成该用户的 **客户端** 接入码。客户端 WebUI 完成登记。
4. 在云端「访问授权」把服务授予该用户，可限制到某台客户端，并限制工具名称、方法、资源 URI 和提示词。用户默认可以使用自己主机发布的服务。
5. 客户端「MCP 服务」点击「订阅全部并保持同步」，再进入「配置导出」，选择 AI 应用及 stdio 模式，预览后写入。
6. 按目标应用的要求刷新或重启。正常使用不需要手动更新 Token。

可先发布内置无副作用测试服务。主机上选择 stdio，命令填安装目录中的 `runtime\Scripts\python.exe`，参数填：

```json
["-m", "mcp_transfer", "demo-server"]
```

调用 `add` 工具，参数 `{"a":20,"b":22}`，应返回 42。

## 配置与端口

客户端默认使用一个 loopback 端口和多个稳定路径：

```text
http://127.0.0.1:17820/mcp/srv_...
```

WebUI 和 MCP 路由共用监听端口，但使用不同的认证方式：WebUI 使用 HttpOnly 会话与 CSRF，MCP 路由使用独立的本地 Bearer Token。安装时可用 `-Port 17830` 选择整个客户端的端口。当前版本不提供每项服务独立监听端口。

推荐 stdio 配置，由桥接进程通过受认证的本机 HTTP 与 Agent 通信。它读取当前 Windows 用户 DPAPI 保护的映射凭证，不使用配置文件中的云端 Token。配置中使用安装环境的 Python 绝对路径，无需 AI 应用的 PATH 包含 Python。

自动配置只操作固定的目标应用路径；首次需预览、确认写入。普通 JSON 保留其他条目；TOML 保留注释。**现有 JSONC 文件不会被强行重写**，请手工合并或先导出为普通 JSON。只会自动更新用户明确开启自动同步的目标文件。配置保存不代表目标应用已经热加载。

## 协议与能力范围

- HTTP 模式保持上游版本语义并转发流；`2026-07-28` 与旧版的会话、方法、取消行为由上游与客户端协商。
- stdio / SSE 适配使用官方 SDK 进行版本协商，并提供工具、资源、提示词、补全和进度；不会声明尚未桥接的订阅能力。
- 适配模式默认不提供 sampling、roots、elicitation / MRTR 用户交互等跨客户端能力。需要这些能力的服务应先通过兼容性验证，使用 HTTP 模式或扩展适配器。
- HTTP 上游仍可能使用同一组服务凭证。平台授权不会自动赋予该上游多用户隔离；应为不同信任域部署独立实例 / 凭证。
- 第三方 MCP 自己需要的 Node、浏览器、数据库客户端等，不会随本平台自动安装。主机 WebUI 的「测试」用于验证本机依赖。

## 安全与运行边界

核心安全模型及接口说明见 [docs/architecture.md](docs/architecture.md)。

- 云端是受信中转，可见 MCP 明文。没有「云端不可见」的端到端加密承诺。
- 每个请求检查当前设备 / 用户状态与授权，流式连接定期复查；已经执行的副作用无法撤销。断线不会自动重放工具调用。
- 刷新凭证每次轮换，默认 30 天闲置期限及 90 天设备授权绝对期限。过期需重新登记；并非永久万能 Token。
- Windows 私密数据使用当前用户 DPAPI，运行任务也是同一用户；frpc 使用的证书、私钥和客户端凭证配置必须落盘，安装器将目录 ACL 限制到此用户和 SYSTEM。Unix 开发环境依靠文件权限。
- 本机管理员或当前用户已经失陷时，代理不能保护其所有能力。原 MCP 应使用最小文件 / 数据库 / 进程权限。
- 公司客户端需获准访问云域名的 HTTPS / SSE。支持显式 HTTP(S) 代理及企业 CA 文件；不自动绕过代理、导入根证书、修改路由或关闭 TLS 验证。Windows PAC / 集成认证未自动适配。
- 当前发布为**单云节点、单 Python worker**部署；旧协议会话路由和本机会话位于内存，云端重启会中断这些会话。不要自行增加 Uvicorn workers。

## 更新、日志与卸载

云端升级：先备份，再在源码目录 `git pull --ff-only`，重新运行 `sudo bash scripts/install-cloud.sh`。初始化不会重置已有管理员。

```bash
docker compose ps
docker compose logs -f --tail=100 cloud caddy
sudo tail -n 100 data/cloud/frps.log
```

Windows 更新：在源码目录 `git pull --ff-only` 后重新运行相应角色安装脚本；保留数据、配置和 DPAPI 凭证。宿主重启需重新登录用户，后台任务才会启动。

```powershell
Get-ScheduledTask -TaskName 'MCPTransfer-*'
& .\scripts\uninstall-windows.ps1 -Role client
```

卸载脚本只停止任务，不删除数据。先在云端撤销设备，再按需删除本机目录和导出的配置。

备份需要包含 PostgreSQL、`data/cloud` 下的 CA / 签名密钥及 `.env`。将备份加密并限制访问；只备份数据库无法恢复完整身份系统。

## 开发与验证

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -c requirements.lock '.[dev]'
.venv/bin/pytest -q
MCPT_E2E=1 .venv/bin/pytest tests/test_e2e.py -q
.venv/bin/ruff check src tests
.venv/bin/python -m build
```

集成测试下载固定版本 frp，启动真实 mTLS、frps/frpc 和 MCP 子进程，验证新旧协议、工具过滤、调用与设备撤销。GitHub Actions 在 Windows/Linux 运行测试，在 Linux 额外验证公网侧 HTTPS 和 Docker 构建。Windows/macOS 的测试专用云端入口使用 loopback HTTP，因为 Go 的系统证书库不会读取测试的 SSL_CERT_FILE；frp 和内部适配连接仍验证 mTLS。生产部署始终使用 HTTPS。不要把未通过这些测试的修改直接部署到生产机器。

本机试运行云端：

```bash
.venv/bin/mcp-transfer --home ./data/dev-cloud cloud-init --public-url http://127.0.0.1:17800 --development
.venv/bin/mcp-transfer --home ./data/dev-cloud cloud --no-frp
```

开发模式 HTTP 只允许 loopback；生产设备接入必须使用 HTTPS。
