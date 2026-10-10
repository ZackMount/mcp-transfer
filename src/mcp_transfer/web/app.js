"use strict";
const state = { role: "", csrf: "", me: null, page: "overview", connection: "primary", renderId: 0 };
const app = document.getElementById("app");
const titles = { cloud: "云端控制中心", host: "主机服务管理", client: "客户端工作台" };
const pages = { overview: "概览", servers: "MCP 服务", devices: "设备与接入", grants: "访问授权", users: "用户管理", exports: "配置导出", audit: "调用审计", settings: "设置", connections: "云端连接" };
function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "text") node.textContent = value;
    else if (value !== false && value != null) node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
function toast(text, error = false) {
  const node = document.getElementById("toast"); node.textContent = (error ? "操作失败：" : "") + text; node.style.display = "block";
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { node.style.display = "none"; }, error ? 12000 : 5500);
}
async function api(path, method = "GET", data, root = false) {
  if (!root && state.role !== "cloud" && state.connection !== "primary") path = "/connections/" + state.connection + path;
  const headers = { "X-CSRF-Token": state.csrf };
  if (data !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 60000);
  let response;
  try { response = await fetch(path, { method, credentials: "same-origin", headers, signal: controller.signal, body: data === undefined ? undefined : JSON.stringify(data) }); }
  catch (error) { throw new Error(error.name === "AbortError" ? "请求超时，请检查连接状态后重试。登记成功但尚未同步时无需重复使用接入码。" : "无法连接服务，请检查网络或服务是否已启动。"); }
  finally { clearTimeout(timeout); }
  let result;
  try { result = await response.json(); } catch { throw new Error(`服务返回异常（HTTP ${response.status}），请检查服务日志。`); }
  if (response.status === 401 && !path.includes("auth/login") && state.me) { state.me = null; state.csrf = ""; loginScreen(); }
  if (!response.ok) throw new Error(typeof result.detail === "string" ? result.detail : JSON.stringify(result.detail || result));
  return result;
}
function action(text, fn, style = "") {
  return el("button", { type: "button", class: style, onclick: async event => {
    const button = event.currentTarget; button.disabled = true;
    try { await fn(); } catch (error) { toast(error.message, true); } finally { button.disabled = false; }
  } }, text);
}
function input(label, name, type = "text", value = "", hint = "") {
  const control = type === "textarea" ? el("textarea", { name }, value) : el("input", { name, type, value, autocomplete: name === "code" || name === "otp" ? "one-time-code" : name === "current_password" ? "current-password" : type === "password" ? "new-password" : "off", required: ["username", "password", "current_password", "name", "cloud_url", "code"].includes(name) });
  if (type === "number") { control.required = true; control.min = "0"; control.max = "4096"; control.step = "1"; }
  return el("label", {}, label, control, hint ? el("span", { class: "small" }, hint) : null);
}
function selectField(label, name, choices) {
  return el("label", {}, label, el("select", { name }, choices.map(([value, text]) => el("option", { value }, text))));
}
function form(title, fields, submitLabel, onsubmit) {
  const node = el("form", { class: "panel stack" }, el("h2", {}, title), el("div", { class: "fieldgrid" }, fields), el("div", {}, el("button", { type: "submit", class: "primary" }, submitLabel)));
  node.addEventListener("submit", async event => {
    event.preventDefault(); const button = node.querySelector("button[type=submit]"); button.disabled = true; node.setAttribute("aria-busy", "true"); node.querySelector(".form-error")?.remove();
    try { await onsubmit(Object.fromEntries(new FormData(node)), node); } catch (error) { node.append(el("p", { class: "form-error", role: "alert" }, error.message)); toast(error.message, true); } finally { button.disabled = false; node.removeAttribute("aria-busy"); }
  });
  return node;
}
function panel(title, ...children) { return el("section", { class: "panel" }, el("h2", {}, title), ...children); }
function badge(value, good) { return el("span", { class: "badge " + (good === true ? "good" : good === false ? "bad" : "") }, value); }
function table(headers, rows) {
  if (!rows.length) return el("div", { class: "empty" }, "这里还没有记录。完成接入或配置后将自动显示。");
  return el("div", { class: "tablewrap" }, el("table", {}, el("thead", {}, el("tr", {}, headers.map(h => el("th", {}, h)))), el("tbody", {}, rows.map(row => el("tr", {}, row.map(cell => el("td", { class: "wrap" }, cell)))))));
}
function check(label, name, checked = false) { return el("label", { class: "check" }, el("input", { type: "checkbox", name, checked }), label); }
function serviceName(id, services) { return services.find(x => x.id === id)?.name || id; }

async function loginScreen() {
  const cloud = state.role === "cloud";
  const node = form(cloud ? "登录控制中心" : "打开本地工作台", [
    ...(cloud ? [input("用户名", "username")] : []),
    input(cloud ? "密码" : "本地访问密码", "password", "password"),
    ...(cloud ? [input("动态验证码（已启用 MFA 时填写）", "otp")] : []),
  ], "登录", async values => {
    await api("/api/auth/login", "POST", values); state.me = await api("/api/me"); state.csrf = state.me.csrf; await render();
  });
  node.className = "stack"; node.querySelector('input[name="password"]').autocomplete = "current-password";
  app.replaceChildren(el("div", { class: "login" }, el("div", { class: "brand" }, "MCP Transfer"), state.insecure_http ? el("div", { class: "message" }, "云端使用明文 HTTP，密码、Token 和调用内容没有传输加密。") : null, node,
    el("p", { class: "small" }, cloud ? "初始管理员密码由云端部署命令生成。建议登录后启用动态验证码。" : "密码在首次安装时显示，也可使用安装目录中的“查看访问密码”命令获取。它只用于本机 WebUI。")));
}

async function overview(main) {
  if (state.role === "cloud") {
    const [devices, services] = await Promise.all([api("/api/devices"), api("/api/servers")]);
    main.append(el("div", { class: "grid" }, [["已登记设备", devices.length], ["在线设备", devices.filter(x => x.active && x.online).length], ["已发布服务", services.filter(x => x.enabled).length]].map(([label, count]) => el("div", { class: "panel" }, el("div", { class: "stat" }, count), el("span", { class: "muted" }, label)))));
    main.append(panel("开始连接你的设备", el("p", {}, "先在设备页生成主机接入码，在主机 WebUI 登记并发布服务；再登记客户端并授予服务访问权限。接入码 5 分钟有效，每台设备独立登记。"), el("div", { class: "row" }, action("登记设备", () => navigate("devices"), "primary"), action("管理授权", () => navigate("grants")))));
    main.append(panel("连接边界", el("p", {}, "客户端通过配置的云端入口访问服务。frp 的业务映射端口只监听云服务器内部地址，原始 MCP 不直接暴露到公网。"), el("p", { class: "small" }, "云端是受信中转，会处理 MCP 调用内容。审计默认记录调用身份、服务、方法及状态，不保存工具参数或结果正文。")));
  } else {
    const status = await api("/api/status");
    main.append(panel("云端连接状态", el("div", { class: "row spread" }, el("div", {}, badge(status.status, status.status === "已连接"), el("p", {}, status.cloud_url || "尚未连接云端"), el("code", {}, status.device_id || "待登记设备")), action("立即同步", async () => { await api("/api/sync", "POST", {}); await render(); }, "primary")), el("p", { class: "small muted" }, "“已连接”表示设备与云端同步成功。请在 MCP 服务页检测工具，确认服务可用。")));
    if (!status.device_id) main.append(form("首次连接云端", [input("云端地址", "cloud_url", "url", "", "例如 https://mcp.example.com"), input("设备名称", "name", "text", state.role === "host" ? "我的 MCP 主机" : "我的工作电脑"), input("一次性接入码", "code", "password"), check("允许明文 HTTP（密码、Token 和调用内容没有传输加密）", "allow_insecure_http", status.allow_insecure_http)], "连接并登记", async data => { data.allow_insecure_http = data.allow_insecure_http === "on"; const result = await api("/api/configure", "POST", data); toast(result.message || "设备已登记"); await render(); }));
    else main.append(panel(state.role === "host" ? "发布你的 MCP 服务" : "把远程服务带到本机", el("p", {}, state.role === "host" ? "在 MCP 服务页添加 HTTP、stdio 或旧 SSE 服务。stdio 测试会启动配置的程序，导入的服务默认停用。" : "同步已授权目录后，可以一次订阅全部服务，生成应用配置。云端令牌由后台管理，不必填入 AI 应用。"), action(state.role === "host" ? "添加服务" : "查看可用服务", () => navigate("servers"), "primary")));
    if (status.device_id) {
      main.append(panel("替换当前登记", el("p", {}, "仅用于当前设备登记失效后的重新接入，将替换当前连接的设备身份。需要同时连接另一台云服务器，请使用“云端连接 → 添加云端连接”。本机主机配置会保留。"),
        form("使用普通接入码", [input("云端地址", "cloud_url", "url", status.cloud_url || ""), input("设备名称", "name", "text", status.name || (state.role === "host" ? "我的 MCP 主机" : "我的工作电脑")), input("一次性接入码", "code", "password"), check("允许明文 HTTP", "allow_insecure_http", status.allow_insecure_http)], "替换当前登记", async data => {
          if (!confirm("替换当前连接的登记？此连接的服务 ID 和映射将重新生成。其他云端连接不受影响。")) return;
          data.allow_insecure_http = data.allow_insecure_http === "on";
          const result = await api("/api/reconnect", "POST", data); toast(result.message); await render();
        })));
    }
    if (state.role === "host") main.append(panel("运行信息", el("p", {}, `frp 进程：${status.frp_running ? "运行中" : "未运行"} · MCP 适配实例：${status.adapter_processes}`), el("p", { class: "small" }, "frp 进程运行不等于隧道一定可用，请结合云端状态和实际调用检查。需要桌面或浏览器的 MCP 应在当前用户会话中运行。")));
  }
  if (location.pathname === "/oauth/authorize" && state.role === "cloud") {
    const params = Object.fromEntries(new URLSearchParams(location.search));
    main.prepend(panel("授权本机 MCP 应用", el("p", { class: "break" }, `请求访问：${params.resource || "未指定资源"}`), el("p", { class: "break" }, `返回地址：${params.redirect_uri || "未指定"}`), action("批准此次访问", async () => { const data = await api("/api/oauth/approve", "POST", params); location.assign(data.redirect); }, "primary")));
  }
}

function hostServiceForm(existing = null) {
  const definition = existing || {};
  const editor = form(existing ? "编辑 MCP 服务：" + definition.name : "添加 MCP 服务", [
    input("服务名称", "name", "text", definition.name || ""),
    selectField("传输方式", "transport", [["stdio", "stdio 子进程"], ["http", "Streamable HTTP"], ["sse", "旧 HTTP + SSE"]]),
    input("用途说明", "description", "text", definition.description || ""),
    input("HTTP / SSE 地址", "url", "text", definition.url || "", "仅允许明确配置的 localhost / 127.0.0.1 地址"),
    input("stdio 可执行程序", "command", "text", definition.command || "", "例如 python、npx 或完整 exe 路径"),
    input("stdio 参数（JSON 数组）", "args", "textarea", JSON.stringify(definition.args || [], null, 2)),
    input("stdio 工作目录（可选）", "cwd", "text", definition.cwd || ""),
    input("环境变量（JSON 对象，仅主机保存）", "env", "textarea", existing ? "" : "{}", existing ? "留空保留现有秘密，填写 {} 可清除" : ""),
    input("上游请求头（JSON 对象，仅主机保存）", "headers", "textarea", existing ? "" : "{}", existing ? "留空保留现有秘密，填写 {} 可清除" : "")
  ], existing ? "保存修改" : "保存服务", async data => {
    for (const key of ["args", "env", "headers"]) { if (!(key in data)) continue; if (data[key].trim()) data[key] = JSON.parse(data[key]); else delete data[key]; }
    if (existing) data.enabled = definition.enabled;
    await api(existing ? "/api/servers/" + definition.id : "/api/servers", existing ? "PUT" : "POST", data);
    toast("已保存，将在后台同步发布"); await render();
  });
  editor.classList.add("service-editor"); editor.querySelector('select[name="transport"]').value = definition.transport || "stdio";
  const transport = editor.querySelector('select[name="transport"]');
  function updateFields() {
    for (const name of ["url", "headers", "command", "args", "cwd", "env"]) {
      const control = editor.querySelector(`[name="${name}"]`);
      const visible = ["url", "headers"].includes(name) ? transport.value !== "stdio" : transport.value === "stdio";
      control.disabled = !visible; control.closest("label").hidden = !visible;
      if (["url", "command"].includes(name)) control.required = visible;
    }
  }
  transport.addEventListener("change", updateFields); updateFields();
  return editor;
}

async function testTools(main, server) {
  const result = await api(`/api/servers/${server.id}/test`, "POST", {});
  main.querySelector(".tool-diagnostic")?.remove();
  const box = panel(`${server.name}：工具检测`, el("p", {}, `${state.role === "client" ? "已通过本机映射、云端和主机读取" : "已从主机上的 MCP 读取"} ${result.tool_count} 个工具。`),
    result.tool_count ? table(["工具名称", "说明"], result.tools.map(tool => [tool.name, tool.description || "—"])) : el("p", { class: "message" }, state.role === "client" ? "没有返回工具。请检查主机的工具检测结果及云端授权中的工具名称列表。服务也可能仅提供资源或提示词。" : "上游没有返回工具。请检查 MCP 程序启动方式；服务也可能仅提供资源或提示词。"),
    el("details", {}, el("summary", {}, "查看协议与能力"), el("pre", {}, JSON.stringify({ server_name: result.server_name, version: result.version, capabilities: result.capabilities }, null, 2))));
  box.classList.add("tool-diagnostic"); main.append(box); box.scrollIntoView({ behavior: "smooth" });
}

async function serversPage(main) {
  const servers = await api("/api/servers");
  if (state.role === "cloud") {
    main.append(table(["服务", "主机", "类型", "状态"], servers.map(x => [el("div", {}, x.name, el("div", { class: "small muted" }, x.id)), x.host_id, x.transport, badge(x.enabled ? (x.online ? "主机在线" : "主机离线") : "已停用", x.enabled && x.online)])));
    return;
  }
  if (state.role === "client") {
    main.append(panel("订阅与映射", el("p", {}, "所有映射均只监听 127.0.0.1。自动订阅仅包含云端已授权给此用户和设备的服务。"), el("div", { class: "row" }, action("订阅全部并保持同步", async () => { await api("/api/subscribe", "POST", { all: true }); toast("自动订阅已启用"); await render(); }, "primary"), action("停止自动订阅新增服务", async () => { await api("/api/subscribe", "POST", { all: false, ids: [] }); toast("已保留现有映射，停止订阅新增服务"); }), action("生成应用配置", () => navigate("exports")))));
    main.append(table(["服务", "远端状态", "本地地址", "操作"], servers.map(x => [x.name, badge(x.online ? "主机在线" : "主机离线", x.online), el("code", {}, x.local_url), el("div", { class: "actions" }, ...(x.mapped ? [action("检测工具", () => testTools(main, x)), action("停用映射", async () => { await api(`/api/servers/${x.id}/toggle`, "POST", {}); await render(); })] : [action("启用映射", async () => { await api("/api/subscribe", "POST", { ids: [x.id] }); await render(); })]))])));
    return;
  }
  main.append(table(["服务", "传输", "状态", "操作"], servers.map(x => [el("div", {}, x.name, el("div", { class: "small muted" }, x.id)), x.transport, badge(x.enabled ? "已启用" : "已停用", x.enabled), el("div", { class: "row actions" }, action("编辑", () => { const editor = hostServiceForm(x); main.querySelector(".service-editor").replaceWith(editor); editor.scrollIntoView({ behavior: "smooth" }); }), action("测试工具", () => testTools(main, x)), action(x.enabled ? "停用" : "启用", async () => { await api(`/api/servers/${x.id}/toggle`, "POST", {}); await render(); }), action("删除服务", async () => { if (!confirm(`删除“${x.name}”的本机配置和云端授权？`)) return; const result = await api(`/api/servers/${x.id}`, "DELETE"); toast(result.message); await render(); }, "danger"))])));
  main.append(hostServiceForm());
  main.append(form("导入现有 MCP 配置", [input("粘贴 mcpServers 或 servers JSON", "content", "textarea", "")], "导入为停用状态", async data => { const result = await api("/api/import", "POST", JSON.parse(data.content)); toast(`已导入 ${result.ids.length} 项，请检查后启用`); await render(); }));
}

async function devicesPage(main) {
  const devices = await api("/api/devices");
  const users = state.me.admin ? await api("/api/users") : [state.me];
  main.append(form("生成设备接入码", [selectField("设备角色", "role", [["host", "主机：发布 MCP 服务"], ["client", "客户端：使用 MCP 服务"]]), selectField("设备所属用户", "owner", users.filter(x => x.active !== false).map(x => [x.id, x.username]))], "生成一次性接入码", async (data, node) => {
    const result = await api("/api/enrollment-codes", "POST", data);
    node.querySelector(".generated")?.remove();
    const field = el("input", { type: "text", value: result.code, readonly: true, "aria-label": "一次性接入码", onclick: event => event.currentTarget.select() });
    const expires = Date.now() + result.expires_in * 1000;
    const hint = el("p", {}, `仅可使用一次，有效至 ${new Date(expires).toLocaleTimeString()}。请在对应角色的本地 WebUI 中登记。`);
    const copy = action("复制接入码", async () => { toast(await copyText(result.code, field) ? "接入码已复制" : "浏览器限制自动复制，已选中接入码，请按 Ctrl+C（Mac 使用 ⌘C）。"); });
    const generated = el("div", { class: "generated message" }, hint, field, copy);
    node.append(generated);
    setTimeout(() => { if (generated.isConnected) { field.value = ""; copy.disabled = true; hint.textContent = "此接入码已过期，请重新生成。"; } }, result.expires_in * 1000);
  }));
  main.append(action("清理所有已撤销设备", async () => {
    if (!confirm("删除所有已撤销设备及其云端服务和授权？本机 MCP 启动配置保留。")) return;
    const result = await api("/api/devices/cleanup", "POST", {}); toast(`已删除 ${result.devices} 台旧设备`); await render();
  }, "danger"));
  main.append(table(["设备", "角色", "所属用户", "状态", "操作"], devices.map(x => [el("div", {}, x.name, el("div", { class: "small muted" }, x.id)), x.role, users.find(u => u.id === x.owner)?.username || x.owner, badge(!x.active ? "已撤销" : x.online ? "在线" : "离线", x.active && x.online), action("删除", async () => {
    if (!confirm(`删除“${x.name}”及其云端服务和授权？删除后可用普通接入码重新登记。`)) return;
    await api(`/api/devices/${x.id}`, "DELETE"); toast("旧设备已删除"); await render();
  }, "danger")])));

}

async function grantsPage(main) {
  const [grants, servers, devices] = await Promise.all([api("/api/grants"), api("/api/servers"), api("/api/devices")]);
  const users = state.me.admin ? await api("/api/users") : [state.me];
  main.append(el("p", {}, "用户默认可以访问自己主机发布的服务。共享给其他用户时，在这里创建授权。工具、资源和提示词支持精确名称列表；* 表示该类别全部开放。"));
  if (servers.length) main.append(form("授予服务访问权限", [selectField("MCP 服务", "server_id", servers.map(x => [x.id, x.name])), state.me.admin ? selectField("授权给用户", "user_id", users.filter(x => x.active !== false).map(x => [x.id, x.username])) : input("接收者用户 ID", "user_id"), selectField("限制到指定客户端（可选）", "device_id", [["", "该用户所有已登记客户端"], ...devices.filter(x => x.role === "client" && x.active).map(x => [x.id, x.name])]), input("工具名称（每行一个，默认 *）", "tools", "textarea", "*"), input("允许方法（每行一个，默认 *）", "methods", "textarea", "*"), input("资源 URI（每行一个，默认 *）", "resources", "textarea", "*"), input("提示词名称（每行一个，默认 *）", "prompts", "textarea", "*")], "创建授权", async data => {
    const policy = {}; for (const field of ["tools", "methods", "resources", "prompts"]) { policy[field] = data[field].split("\n").map(x => x.trim()).filter(Boolean); delete data[field]; }
    await api("/api/grants", "POST", { ...data, policy }); toast("授权已创建"); await render();
  }));
  main.append(table(["服务", "用户", "设备范围", "工具权限", "操作"], grants.map(x => [serviceName(x.server_id, servers), users.find(u => u.id === x.user_id)?.username || x.user_id, x.device_id || "该用户所有设备", (x.policy.tools || ["*"]).join(", "), action("撤销授权", async () => { await api(`/api/grants/${x.id}`, "DELETE"); await render(); }, "danger")])));
}

async function usersPage(main) {
  const users = await api("/api/users");
  main.append(form("新增用户", [input("用户名", "username"), input("初始密码（至少 12 个字符）", "password", "password"), check("管理员", "admin")], "创建用户", async data => { data.admin = data.admin === "on"; await api("/api/users", "POST", data); toast("用户已创建"); await render(); }));
  main.append(table(["用户名", "用户 ID", "角色", "MFA", "状态", "操作"], users.map(x => [x.username, el("code", {}, x.id), x.admin ? "管理员" : "成员", x.mfa ? "已启用" : "未启用", badge(x.active ? "有效" : "已停用", x.active), x.id !== state.me.id && x.active ? action("停用", async () => { if (!confirm(`停用用户 ${x.username} 及其设备访问？`)) return; await api(`/api/users/${x.id}/disable`, "POST", {}); await render(); }, "danger") : "—"])));
}

async function exportsPage(main) {
  main.append(el("p", {}, "stdio 配置通过本地代理取凭证，不包含云端密钥。HTTP 配置包含仅适用于本机映射的令牌，请保护配置文件。先预览，再写入；已有文件会备份。"));
  main.append(el("p", {}, "Cursor 导出的是完整 mcp.json 文件，mcpServers 是顶层容器，下面每一项才是一台 MCP 服务。请选择 Cursor 并使用“备份并写入配置”；粘贴到已有文件时请合并容器内的条目，避免重复嵌套 mcpServers。"));
  const resultBox = el("div");
  main.append(form("生成应用配置", [selectField("服务范围", "scope", [["all", "所有云端连接"], ["current", "当前云端连接"]]), selectField("目标应用", "target", [["generic", "通用 mcpServers JSON"], ["cursor", "Cursor"], ["vscode", "VS Code"], ["claude", "Claude Desktop"], ["codex", "Codex TOML"]]), selectField("本地接入方式", "mode", [["stdio", "stdio 桥（推荐）"], ["http", "本机 HTTP"]])], "生成预览", async data => {
    const all = data.scope === "all";
    const result = await api("/api/exports/preview", "POST", data, all);
    const auto = check("以后自动同步此配置文件中的平台条目", "auto_sync");
    resultBox.replaceChildren(panel("配置预览", el("p", { class: "break" }, result.path), el("pre", {}, result.content), action("复制配置", async () => { toast(await copyText(result.content) ? "配置已复制" : "已选中配置，请按 Ctrl+C（Mac 使用 ⌘C）。"); }), el("details", {}, el("summary", {}, "查看与原文件的差异"), el("pre", {}, result.diff || "无变化")), auto, el("div", { class: "row" }, action("备份并写入配置", async () => {
      const applied = await api("/api/exports/apply", "POST", { preview_id: result.preview_id, auto_sync: auto.querySelector("input").checked }, all); toast("已写入配置。请按目标应用要求刷新或重启。" + (applied.backup ? " 原文件已备份。" : ""));
    }, "primary"), action("下载配置", () => {
      const url = URL.createObjectURL(new Blob([result.content], { type: "text/plain;charset=utf-8" })); const a = el("a", { href: url, download: data.target === "codex" ? "mcp-transfer.toml" : "mcp-transfer.json" }); a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    }))));
  }), resultBox);
}

async function auditPage(main) {
  const rows = await api("/api/audit");
  main.append(el("p", {}, "最近 200 条记录。默认不记录工具参数、返回正文或 Token。"), table(["时间", "用户", "操作", "对象", "结果"], rows.map(x => [new Date(x.at * 1000).toLocaleString(), x.actor, x.action, x.target, x.status])));
}

async function settingsPage(main) {
  if (state.role === "cloud") {
    if (state.me.admin) {
      const limits = await api("/api/settings/limits");
      main.append(form("每设备并发限制", [input("同时进行的 MCP 请求", "request_concurrency", "number", limits.request_concurrency, "POST / DELETE 请求；0 表示不限制，最大 4096"), input("长期 SSE 连接", "stream_concurrency", "number", limits.stream_concurrency, "GET 流连接单独计算，不占工具请求额度；0 表示不限制")], "保存并立即生效", async data => {
        for (const key of Object.keys(data)) data[key] = Number(data[key]);
        await api("/api/settings/limits", "PUT", data); toast("并发限制已更新，无需重启。已在执行的请求不会中断。");
      }));
    }
    main.append(form("修改登录密码", [input("当前密码", "current_password", "password"), input("新密码（至少 12 个字符）", "password", "password")], "更新密码并退出登录", async data => { await api("/api/auth/password", "POST", data); state.me = null; state.csrf = ""; await loginScreen(); }));
    const box = panel("动态验证码 MFA", el("p", {}, state.me.mfa ? "已启用动态验证码。登录时需要填写验证器中的六位验证码。" : "建议管理员启用 MFA。可使用支持 TOTP 的验证器，密钥只在设置过程中显示。"));
    if (!state.me.mfa) box.append(action("设置 MFA", async () => {
      const result = await api("/api/auth/mfa/setup", "POST", {});
      box.append(el("pre", {}, result.secret), form("确认验证器", [input("六位动态验证码", "otp")], "启用 MFA", async data => { await api("/api/auth/mfa/confirm", "POST", data); state.me.mfa = true; toast("MFA 已启用"); await render(); }));
    }, "primary"));
    main.append(box);
  } else {
    const status = await api("/api/status");
    if (status.device_id) main.append(form("更改云端入口", [input("云端完整地址（含协议与端口）", "cloud_url", "url", status.cloud_url || ""), check("允许明文 HTTP（密码、Token 和调用内容没有传输加密）", "allow_insecure_http", status.allow_insecure_http)], "保存地址并重新连接", async data => {
      data.allow_insecure_http = data.allow_insecure_http === "on";
      await api("/api/connection", "PUT", data); toast("连接已更新，设备身份与服务映射保留"); await render();
    }));
    if (state.role === "host") {
      const limits = await api("/api/settings/limits");
      main.append(form("主机适配并发限制", [input("MCP 适配实例数", "adapter_processes", "number", limits.adapter_processes, "当前云端连接可同时保留的适配实例；0 表示不限制"), input("每个适配实例的会话数", "sessions_per_adapter", "number", limits.sessions_per_adapter, "0 表示不限制。修改后新建实例生效，已有调用保持运行")], "保存主机限制", async data => {
        for (const key of Object.keys(data)) data[key] = Number(data[key]);
        await api("/api/settings/limits", "PUT", data); toast("已保存。实例数限制立即生效，会话限制对新建实例生效。");
      }));
    }
    main.append(form("企业网络设置", [input("批准使用的 HTTP(S) 代理（可选）", "proxy", "text", status.proxy || "", "例如 http://proxy.company.example:8080"), input("企业 CA 文件完整路径（可选）", "enterprise_ca", "text", status.enterprise_ca || "", "保留证书校验；不会自动绕过企业代理或安全策略。")], "保存网络设置", async data => { await api("/api/network", "POST", data); toast("设置已保存，下一次连接生效"); }));
    main.append(panel("本地运行", el("p", {}, "客户端和主机端都以当前 Windows 用户后台运行。重启后随登录启动；本机 WebUI 与映射端口不会监听局域网。"), el("p", {}, "如需不同映射端口，可在首次启动时使用 --port 参数。每个服务在该端口下有独立的 /mcp/{server_id} 路径。")));
  }
}

async function connectionsPage(main) {
  const rows = await api("/api/connections", "GET", undefined, true);
  main.append(panel("同时连接多台云服务器", el("p", {}, "每个连接独立登记、保存凭证、同步和发布服务。切换页面仅改变正在查看的连接，其他连接持续运行。")));
  main.append(table(["连接", "地址", "状态", "操作"], rows.map(item => [item.name, item.cloud_url || "待接入", badge(item.status, item.status === "已连接"), el("div", { class: "row" }, action("管理", async () => { state.connection = item.id; await navigate("overview"); }), ...(item.id !== "primary" ? [action("移除此连接", async () => {
    if (!confirm(`移除“${item.name}”？将撤销其云端设备并清理此连接写入的应用配置（原文件备份）。其他连接继续运行，本机配置保留在归档目录。`)) return;
    const result = await api(`/api/connections/${item.id}`, "DELETE", undefined, true); if (state.connection === item.id) state.connection = "primary"; toast(result.message); await render();
  }, "danger")] : []))])));
  main.append(form("添加云端连接", [input("连接名称", "label", "text", "", "例如个人云、测试云"), input("云端地址", "cloud_url", "url"), input("设备名称", "name", "text", state.role === "host" ? "我的 MCP 主机" : "我的工作电脑"), input("新云端的一次性接入码", "code", "password"), check("允许此连接使用明文 HTTP", "allow_insecure_http"), ...(state.role === "host" ? [check("复制默认连接的服务定义（保持停用，检查后启用）", "copy_services")] : [])], "添加并保留现有连接", async data => {
    data.allow_insecure_http = data.allow_insecure_http === "on";
    data.copy_services = data.copy_services === "on";
    const result = await api("/api/connections", "POST", data, true); state.connection = result.id; state.page = "overview"; toast(result.message); await render();
  }));
}

async function navigate(page) { state.page = page; await render(); }
async function render() {
  const renderId = ++state.renderId;
  if (!state.me) return loginScreen();
  const connections = state.role !== "cloud" ? await api("/api/connections", "GET", undefined, true) : [];
  if (state.role !== "cloud" && !connections.some(item => item.id === state.connection)) state.connection = "primary";
  const connectionInfo = await api("/api/info");
  const available = state.role === "cloud" ? ["overview", "servers", "devices", "grants", ...(state.me.admin ? ["users"] : []), "audit", "settings"] : ["overview", "connections", "servers", ...(state.role === "client" ? ["exports"] : []), "settings"];
  if (!available.includes(state.page)) state.page = "overview";
  const main = el("main", { class: "main" }, el("header", { class: "topbar" }, el("div", {}, el("h1", {}, pages[state.page]), el("span", { class: "muted" }, titles[state.role])), el("div", { class: "row" }, action("刷新", render), action("退出", async () => { await api("/api/auth/logout", "POST", {}, true); state.connection = "primary"; state.me = null; state.csrf = ""; await render(); }))));
  if (renderId !== state.renderId) return;
  app.replaceChildren(el("div", { class: "shell" }, el("aside", { class: "sidebar" }, el("div", {}, el("div", { class: "brand" }, "MCP Transfer"), el("div", { class: "subtitle" }, titles[state.role])), el("nav", { class: "nav" }, available.map(page => action(pages[page], () => navigate(page), page === state.page ? "active" : ""))), el("footer", {}, "Private connections. Explicit access.", el("br"), "v" + state.version)), main));
  if (state.role !== "cloud") {
    const selector = selectField("当前云端连接", "active_connection", connections.map(item => [item.id, `${item.name} · ${item.cloud_url || "待接入"}`]));
    selector.querySelector("select").value = state.connection;
    selector.querySelector("select").addEventListener("change", async event => { state.connection = event.target.value; state.page = "overview"; await render(); });
    app.querySelector(".sidebar").insertBefore(selector, app.querySelector(".nav"));
  }
  if (connectionInfo.insecure_http) main.append(el("div", { class: "message" }, "当前云端入口使用明文 HTTP。登录密码、Token 和 MCP 内容可被网络中间方读取或修改；frp 的内部加密不能保护这一段连接。"));
  const handlers = { overview, connections: connectionsPage, servers: serversPage, devices: devicesPage, grants: grantsPage, users: usersPage, exports: exportsPage, audit: auditPage, settings: settingsPage };
  try { await handlers[state.page](main); } catch (error) { main.append(el("div", { class: "message" }, error.message)); }
}
async function start() {
  try {
    const info = await api("/api/info"); state.role = info.role; state.version = info.version; state.insecure_http = info.insecure_http;
    try { state.me = await api("/api/me"); state.csrf = state.me.csrf; } catch { state.me = null; }
    await render();
  } catch (error) { app.replaceChildren(el("div", { class: "login" }, el("h1", {}, "无法连接本地服务"), el("p", {}, error.message), action("重试", start, "primary"))); }
}
start();
