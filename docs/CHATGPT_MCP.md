# ChatGPT Workspace 只读 MCP

架构：

```text
ChatGPT Workspace
→ Cloudflare Tunnel
→ mcp-auth-proxy (OAuth, 127.0.0.1:18081)
→ 官方 weknora mcp serve
→ 本地 WeKnora
```

模板没有自定义 MCP Server、过滤代理或写操作适配器。后端是官方 CLI 提供的
curated read-only MCP surface。

## 准备

1. `scripts/configure-weknora.ps1` 已完成且 `bin/weknora.exe doctor` 成功。
2. 域名已托管到自己的 Cloudflare 账号。
3. 运行 `scripts/bootstrap.ps1 -InstallMcpTools`。脚本固定并验证
   `mcp-auth-proxy v2.10.2` 的 SHA-256，并验证 cloudflared 的 Authenticode 签名。
4. 运行 `mcp-public/set-password.ps1`。仓库只保存 bcrypt 哈希到忽略目录，
   不保存明文密码。

## 创建 Tunnel

```powershell
powershell -File .\mcp-public\setup-cloudflare.ps1 `
  -Hostname mcp.your-domain.example -CreateDnsRoute
```

`-CreateDnsRoute` 会修改该主机名的 Cloudflare DNS。省略它可先只生成本地配置并
查看提示。脚本不公开 WeKnora 8080，只把公网主机名导向本机 OAuth 代理。

## 启动与本地确认

```powershell
powershell -File .\mcp-public\start-all.ps1 `
  -ExternalUrl https://mcp.your-domain.example
powershell -File .\mcp-public\test-local.ps1
powershell -File .\mcp-public\status.ps1
```

确认公网 `https://mcp.your-domain.example/healthz` 可达后，在实际使用的 ChatGPT
Business/Enterprise Workspace 中创建自定义应用：

- 服务器 URL：`https://mcp.your-domain.example/mcp`
- 身份验证：OAuth
- 完成 OAuth 登录并启用实时访问或索引搜索
- 审查工具列表，确认没有 create/delete/update/upload 等写工具
- 先保存为草稿，真实检索成功后再按 Workspace 范围发布

账号邮箱相同不代表 Workspace 配置共享。应用必须安装到实际使用它的 Workspace。

## 停止

```powershell
powershell -File .\mcp-public\stop.ps1
```

OAuth 数据、Tunnel 凭据、日志和 PID 均位于 Git 忽略目录。不要将这些文件贴到
Issue、日志附件或屏幕截图中。
