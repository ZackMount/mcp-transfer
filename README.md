# MCP Transfer

自托管的 MCP 服务共享平台：**Linux 云端 + Windows 主机端 + Windows 客户端**，三端均提供 WebUI。

主机端将本机 HTTP、stdio 或旧 SSE MCP 接入平台，frp 只把主机适配入口映射到云服务器的 loopback。公司客户端向云端发起 HTTP(S) 请求，再向本机 AI 应用提供 MCP 接口；不使用 VPN、虚拟网卡、Tailscale 或公司内网入站连接。

**公司客户端的边界：** `client` 安装角色不下载 frpc，客户端启动、同步、重连和关闭都不创建或加载 frp 运行模块；客户端配置不保存 frp 地址 / 端口 / 机器凭证。它仅访问设置的云端 HTTP(S) 网关（本例 18080），不连接 frps 的接入端口（本例 17000）。frp 只运行在发布 MCP 的主机与云端之间。本边界针对「禁止客户端运行 frp 或直连 frp 端口」；其他网络访问仍需符合公司允许的地址、端口和代理要求。

界面预览：[云端](docs/cloud-ui.jpg) · [主机端](docs/host-ui.jpg) · [客户端](docs/client-ui.jpg)。截图来自本地验证环境，不代表已部署到你的云服务器。

```text
AI 应用 → 本机 stdio 桥 / HTTP 映射 → 云端 HTTP(S) 鉴权网关
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
- Windows 当前用户后台任务、自启动、DPAPI 凭证保护；云端 Docker Compose、PostgreSQL、可选 HTTPS / 显式 HTTP 与自定义公网端口。

## 一行部署

这是公开仓库。下面的下载入口无需 Git、GitHub CLI、GitHub 账号或 Token；目标机器需要能够访问 GitHub 的公开下载地址。也可将源码通过公司批准的方式复制到目标机器，直接运行安装脚本。

云端默认安装到 `/opt/mcp-transfer`；若检测到已有 Compose 部署，会沿用原目录，避免丢失密钥。可以用 `--install-dir /原部署目录` 显式指定。Windows 使用普通用户 PowerShell；本机策略需要允许运行已审阅的安装脚本。安装程序不修改 PowerShell 安全策略。

### 云端 · Linux

```bash
curl -fsSL https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-cloud.sh | sudo bash
```

安装时输入完整入口地址，例如 `https://mcp.example.com`。HTTPS 域名需要先解析到云服务器，也可以预先指定：

```bash
curl -fsSL https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-cloud.sh | sudo bash -s -- --domain mcp.example.com
```

Ubuntu 22.04 / 24.04 会自动补齐 Docker、Compose 和 Buildx；已有 Docker 时只安装缺少的插件，并按现有 Ubuntu / Docker CE 包来源选择对应插件。其他 Linux 发行版需要先安装这些组件。该部署使用 Linux host networking，不适用于 Docker Desktop 的默认网络模式。

安装器会先下载并校验云端 frps，再启动网关并检查 frps 监听端口。frps 下载失败会中止部署，不能只凭 `Cloud initialized` 判断转发已经可用。

如果云主机临时使用 Mac 提供的 HTTP 代理，可以在部署命令最后加 `--download-proxy http://127.0.0.1:17897`。此参数用于源码、镜像构建中的 apt/pip 和 frps 下载，不会保存到长期服务配置。Docker 镜像拉取仍需 Docker 后台服务代理。构建使用主机网络，以访问云主机 loopback 的代理；日常容器不会继承该构建代理。frps 下载到 `data/cloud/bin` 后，日常启动无需再下载。

Python 包下载还可以临时选择 HTTPS 镜像源，例如 `--pip-index-url https://pypi.tuna.tsinghua.edu.cn/simple`。该参数只用于这次构建，不写入系统或容器运行时的 pip 配置。构建依赖 setuptools/wheel 使用约束文件中的固定版本，先安装再构建项目，避免隔离构建子进程另行下载未约束的依赖；下载缓存会被后续构建复用。

如果 Ubuntu 22.04 提示 `docker: unknown command: docker compose`，缺的是 Compose 插件。在 Ubuntu 自带 Docker 的情况下，可以手工执行 `apt-get update && apt-get install -y --no-install-recommends docker-compose-v2 docker-buildx`，然后重新运行部署入口。更新后的入口已自动处理这一情况。Ubuntu 源需要包含 `jammy-updates` 的 `universe`；Docker CE 则使用其官方源中的 `docker-compose-plugin` / `docker-buildx-plugin`。

默认 HTTPS 部署需要配置 DNS，并在云安全组 / 防火墙放行：

- **TCP 80、443**：HTTPS 证书申请、管理界面及客户端连接。
- **TCP 7000**：主机 frpc 接入，需要设备认证及客户端证书。
- **不要放行** 17800、17900、15432、21000–21999；它们仅用于云服务器内部通信。

安装结束会显示初始管理员密码，只在首次初始化时生成。访问输出的控制台地址，登录后启用 MFA。已有部署的地址变更应使用下方迁移参数，不要只手改 `.env`。

### 自定义端口与 HTTP（v0.1.1）

WebUI 和 MCP API 共用一个公网入口端口，frp 使用另一个独立端口。示例选择 **18080** 与 **17000**；换成你要使用的端口即可。内部 17800、17900、15432、21000–21999 不对外开放，也不能拿来作为公网入口。

首次部署，替换示例 IP 后运行：

```bash
curl -fsSL https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-cloud.sh | sudo bash -s -- --public-url http://203.0.113.10:18080 --frp-port 17000 --allow-insecure-http
```

该模式无需域名和 HTTPS 证书，只需放行所选的 TCP 18080、17000，不会额外开启本服务的 80/443，也不会自动跳转 HTTPS。`--allow-insecure-http` 必须显式指定；选择 HTTP 后，后续无参数升级会保留已选择的模式。

**公网 HTTP 会明文传输登录密码、刷新凭证、访问 Token 和 MCP 内容，可能被窃听或篡改。** 更换端口无法消除这个风险；frp TLS 和网关到主机的 mTLS 继续保留，但不能保护客户端到公网 HTTP 入口这一段。此模式不满足标准 OAuth/MCP 授权的 HTTPS 安全要求，部分第三方直连客户端可能拒绝 HTTP；平台自己的客户端代理支持该模式。

已有部署更换地址、协议或公网入口端口：

```bash
curl -fsSL https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-cloud.sh | sudo bash -s -- --public-url http://203.0.113.10:18080 --frp-port 17000 --allow-insecure-http --migrate-origin
```

迁移会备份 `.env`、保留管理员、用户、服务、授权、设备身份和签名 / CA 密钥，清理旧 Web 登录会话和待兑换授权码，并重启入口与云服务。进行中的调用会中断，不会自动重放；初始管理员密码不会重新生成。

两台 Windows 都要更新到 v0.1.1：重新运行下方对应角色的一行下载命令即可，数据与设备身份保留。然后分别在本地 WebUI 的「设置 → 更改云端入口」填写完整新地址，并勾选「允许明文 HTTP」。无需重新生成接入码，原服务定义和本地映射会保留。新登记设备则在首次连接页面勾选同一选项；安装脚本也支持 `-AllowInsecureHttp`。

若仅修改 frp 端口而保持公网 Web 地址不变，使用 `sudo bash scripts/install-cloud.sh --frp-port 17000`；升级后的主机端会在下一次心跳自动获取新端口并重连，客户端不需要修改地址。

原 HTTPS 域名可能已经被浏览器缓存了 HSTS，从而强制升级为 HTTPS。改用 IP 或新主机名可避开旧域名的 HSTS 状态；不要通过关闭整个浏览器的安全保护来解决。云主机上的其他服务及其端口不会由本安装器自动关闭。

只查看最终参数、不执行部署：

```bash
bash scripts/install-cloud.sh --public-url http://203.0.113.10:18080 --frp-port 17000 --allow-insecure-http --print-config
```

### 主机端 · Windows

```powershell
$p=Join-Path $env:TEMP 'mcp-transfer-bootstrap.ps1'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-windows.ps1' -ErrorAction Stop -OutFile $p; & $p -Role host
```

安装程序自动准备 Python 3.12 独立环境、依赖和 frpc，创建当前用户登录时启动的计划任务，显示本地访问密码并打开 `http://127.0.0.1:17810`。没有 Python 时需要 Windows App Installer 提供的 `winget`。

### 客户端 · Windows

```powershell
$p=Join-Path $env:TEMP 'mcp-transfer-bootstrap.ps1'; Invoke-WebRequest -UseBasicParsing 'https://raw.githubusercontent.com/ZackMount/mcp-transfer/main/scripts/bootstrap-windows.ps1' -ErrorAction Stop -OutFile $p; & $p -Role client
```

完成后打开 `http://127.0.0.1:17820`。客户端不安装或运行 frp，不需要开放任何入站端口。同一电脑可分别运行两种角色的安装命令。公开源码下载仍使用 HTTPS，和平台入口选择 HTTP 是两回事。

若已将源码复制到目标 Windows 机器，在源码根目录只需：

```powershell
& .\scripts\install-windows.ps1 -Role host
# 另一台客户端使用 -Role client
```

安装目录为 `%LOCALAPPDATA%\MCPTransfer\host` 或 `client`。以后忘记 WebUI 密码，可双击其中的 `show-access-password.cmd`；不会在应用配置中保存该密码。

## 首次使用

1. 云端「设备与接入」选择 **主机**，生成 5 分钟有效的一次性接入码。
2. 主机 WebUI 填云端地址、名称和接入码。添加 MCP 的本机地址或启动命令；通过「测试工具」查看工具列表后发布。导入的定义默认停用，不会自动执行。
3. 云端为使用者创建用户，生成该用户的 **客户端** 接入码。客户端 WebUI 完成登记。
4. 在云端「访问授权」把服务授予该用户，可限制到某台客户端，并限制工具名称、方法、资源 URI 和提示词。用户默认可以使用自己主机发布的服务。
5. 客户端「MCP 服务」点击「订阅全部并保持同步」，再点击「检测工具」检查本机映射 → 云端 → 主机的完整链路。之后进入「配置导出」，选择 AI 应用及 stdio 模式，预览后写入。
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

导出条目包含服务名与唯一 ID，例如 `mcpt_cheat-engine_srv_...`；同名服务不会相互覆盖，旧版平台条目会在写入时迁移。Cursor 的完整文件是 `{"mcpServers":{"服务名称":{"command":"...","args":[]}}}`。`mcpServers` 是顶层容器，不是服务名；合并现有文件时只合并容器里的服务条目，不要重复嵌套。

### 已连接但没有工具

概览的「已连接」只说明设备成功与云端同步，主机在线也不能证明 MCP 程序或穿透链路可用。先在主机「测试工具」，再在客户端「检测工具」；检测会读取工具列表，不执行工具调用。主机显示 0 个工具时检查原 MCP 的启动方式和能力；主机有工具而客户端没有时检查云端工具授权。客户端检测失败时检查本机 `data/agent.log`、云端调用审计及主机的 `data/frpc.log`。

云端没有 `data/cloud/frps.log` 时，先执行 `docker compose logs --tail=80 cloud`，确认 frps 是否下载失败。已有部署可在临时代理连通时单独下载，不需要重建镜像或重置账户：

```bash
cd /opt/mcp-transfer
docker compose run --rm \
  -e HTTP_PROXY=http://127.0.0.1:17897 \
  -e HTTPS_PROXY=http://127.0.0.1:17897 \
  -e NO_PROXY=localhost,127.0.0.1,::1 \
  cloud install-frp --binary frps
docker compose restart cloud
```

下载成功会输出 `/data/bin/frps`。这些代理环境变量只属于这次下载，退出容器后不用额外清理。云端日志和主机 frpc 日志仍需确认连接成功；主机的公网接入端口（本例 TCP 17000）需要在云主机防火墙和安全组放行。

客户端能检测出工具而 Cursor 仍没有工具时，重新选择 **Cursor** 导出，通过「备份并写入配置」写入 `%USERPROFILE%\.cursor\mcp.json`，然后刷新或重启 Cursor。同时检查项目 `.cursor/mcp.json` 是否还有旧条目，以及 Cursor 的 MCP 输出日志。stdio 桥必须由安装时的同一 Windows 用户运行，才能读取该用户 DPAPI 保护的本地凭证。主机与客户端无需使用相同 Windows 用户。

### 删除旧设备并重新登记

设备页可以直接「删除」设备及其云端服务和授权，或点击「清理所有已撤销设备」批量清理旧记录。主机端和客户端的概览都提供「重新登记」，输入普通的一次性接入码即可，使用方式与首次接入相同。

主机重新登记保留本机 MCP 命令、参数、环境变量和服务名称，生成新的服务 ID 并重新发布。客户端重新同步并订阅授权目录，然后重新生成 AI 应用配置。旧设备和旧授权可以删除，无需恢复码或服务身份迁移。

证书续期先在临时目录生成密钥，签发校验成功后才替换。网络丢失刷新响应时，Agent 使用受保护凭证中的请求标识恢复同一次刷新；其他旧凭证重用仍会撤销设备。每个配置目录只允许运行一个后台 Agent，重复启动和端口冲突不会额外刷新凭证。Windows 更新和卸载会停止当前用户该配置目录的旧 Agent 及主机 frpc。

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
- 公司客户端需获准访问配置的云地址、端口及流式响应；选择自定义 HTTP 端口并不意味着公司出口一定放行。支持显式 HTTP(S) 代理及企业 CA 文件；不自动绕过代理、导入根证书、修改路由或关闭 TLS 证书验证。Windows PAC / 集成认证未自动适配。
- 当前发布为**单云节点、单 Python worker**部署；旧协议会话路由和本机会话位于内存，云端重启会中断这些会话。不要自行增加 Uvicorn workers。

## 更新、日志与卸载

云端升级：先备份，再重新运行公开下载入口；会自动沿用已有部署目录和配置，不重置管理员。若旧容器已被删除，请用 `--install-dir` 指向原目录。发现已有数据库卷但找不到相应 CA / 签名密钥时，下载入口会停止，避免用新身份覆盖原部署。

已有 Git checkout 会通过公开 HTTPS 地址执行快进更新，不要求 GitHub 登录，也不会强制覆盖本地改动。全新安装通过压缩包下载，完全不需要 Git。

```bash
docker compose ps
docker compose logs -f --tail=100 cloud caddy
sudo tail -n 100 data/cloud/frps.log
```

Windows 更新：重新运行对应角色的公开下载命令；保留数据、配置和 DPAPI 凭证。下载源码缓存在 `%LOCALAPPDATA%\MCPTransfer-source`，与运行数据分开。宿主重启需重新登录用户，后台任务才会启动。

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

集成测试下载固定版本 frp，启动真实 mTLS、frps/frpc 和 MCP 子进程，验证新旧协议、工具过滤、调用与设备撤销。另有显式 HTTP 模式、地址迁移和端口更新用例。GitHub Actions 在 Windows/Linux 运行测试，在 Linux 额外验证公网侧 HTTPS、Caddy 自定义 HTTP 监听与 Docker 构建。Windows/macOS 的测试专用 HTTPS 场景改用 loopback HTTP，因为 Go 的系统证书库不会读取测试的 SSL_CERT_FILE；frp 和内部适配连接仍验证 mTLS。不要把未通过这些测试的修改直接部署到生产机器。

本机试运行云端：

```bash
.venv/bin/mcp-transfer --home ./data/dev-cloud cloud-init --public-url http://127.0.0.1:17800 --development
.venv/bin/mcp-transfer --home ./data/dev-cloud cloud --no-frp
```

`--development` 仍然只放行 loopback HTTP。其他 HTTP 入口需要独立的 `--allow-insecure-http`，不能借开发模式隐式放开。
