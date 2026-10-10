# 架构与维护说明

## 三端

- `cloud.py`：WebUI API、账户及 TOTP、登记码、设备 Token、目录、ACL、审计、OAuth 元数据与资源入口。
- `agent.py`：主机 / 客户端控制台、后台同步、客户端映射、Host Adapter、凭证轮换。
- `protocol.py`：官方 SDK 驱动的 stdio / SSE 适配。独立 actor 绑定 `(server_id, user_id, device_id, connection_id)`。
- `frp.py`：固定版本下载校验、frps / frpc 配置与进程管理。客户端机器不调用此模块启动 frp。
- `pki.py` / `security.py` / `vault.py`：RSA JWT、私有 CA、mTLS、边界校验、密码哈希、Windows DPAPI。
- `exporters.py`：目标应用格式、条目归属检查、差异预览、文件备份和替换。

## 公网与私有监听

Caddy 公网 80/443，转发到同 Linux network namespace 的 `127.0.0.1:17800`。frps 公网 7000 强制 mTLS 与 OIDC，业务映射只监听 `127.0.0.1:21000–21999`。frp 策略插件单独监听 `127.0.0.1:17900`，不挂到公网 FastAPI 路由。数据库容器仅映射 `127.0.0.1:15432`。

不要把 Docker Compose 中的 host networking 改成 bridge 后继续使用旧的 localhost 地址。若要拆到多机器，需重新设计私网、TLS 身份和状态共享。

## 身份与授权

管理员首次部署随机初始化，密码用 Argon2id 保存，Web 会话保存在数据库。Cookie 为 HttpOnly / SameSite，公网 HTTPS 时标记 Secure；写操作使用 CSRF Token。支持 TOTP MFA。

接入码绑定用户与设备角色，5 分钟有效，数据库条件更新保证单次消费。设备登记返回独立刷新凭证；主机另有 frp OIDC 机器凭证及客户端证书。

Agent 使用 `cloud-api` audience 的短期令牌请求目录和换取资源令牌。MCP 资源令牌 audience 为具体的 `https://域名/mcp/{server_id}`。网关对每条请求检查用户、设备、服务状态及授权，不依赖目录隐藏实现访问控制。

frp 的 OIDC access token audience 是 `frp-register`。Login / Ping / NewWorkConn 插件校验 JWT 与主机身份，NewProxy 限制 `tcp`、指定代理名和已分配端口。插件服务不可达时注册必须失败，不能移除插件后继续运行。

Gateway 到 Host Adapter 通过 frp TCP 承载独立 mTLS：网关验证 `{device_id}.mcp.internal` 的主机证书；主机验证客户端 CA。内部调用票据绑定 host、service、user、device、HTTP 方法、请求正文摘要及 nonce。主机限制票据使用次数并重新执行服务权限检查。原 MCP 只收到主机定义的上游凭证。

OAuth 接口支持预注册公共客户端 `mcp-transfer`、PKCE S256 和严格的 loopback callback `/oauth/callback`，以及给 frpc 的机器 client_credentials。不是通用多租户身份提供方，不开放动态注册或任意回调 URL；外部企业 IdP 联邦属于后续扩展。

## 数据与一致性

云端 SQLAlchemy 同时支持 PostgreSQL 和单机开发 SQLite。持久对象为 User、Device、Invite、Service、Grant、Audit、WebSession、OAuthCode。生产默认 PostgreSQL。

网关旧版 MCP 会话绑定身份并保存在内存；仅允许一个网关 worker。旧会话不能在不同调用主体之间复用。云端重启后客户端应重新建立旧协议会话，不重放未知结果的工具调用。

服务目录每 10 秒同步，客户端超过 30 秒无法验证目录则拒绝本地调用。授权最终仍在网关逐请求执行。流转发每约 2 秒复核授权和令牌期限；没有流量也会定期检查。实际撤销延迟受调度和网络影响，不能承诺已经执行的外部操作被撤回。

stdio actor 有数量和空闲回收限制；HTTP upstream 不跟随重定向，主机配置只接入明确的 loopback 地址。请求正文和单个 SSE 事件有大小限制，响应逐段转发。

## 升级与恢复

第一版 schema 在启动时建立；后续对数据库结构的改变必须提供迁移，不能仅依赖 `create_all`。先备份数据库、CA、签名密钥和环境配置，再升级镜像。密钥遗失后的恢复需要重新登记设备。

证书续期由主机心跳触发；更新叶证书后重启本机适配 TLS listener 与 frpc，不自动重放中断中的操作。根 CA、云端 origin 或签名密钥更换需单独的迁移流程。

第三方 MCP 的工具描述、只读标记、资源路径等不构成可靠的操作系统安全边界。对高权限 MCP，应额外使用受限服务账户、容器或 VM，并限制其网络与文件系统访问。
