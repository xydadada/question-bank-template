# ChatGPT Workspace 受限检索 MCP

架构：

```text
ChatGPT Workspace
→ Cloudflare Tunnel
→ mcp-auth-proxy (OAuth, 127.0.0.1:18081)
→ 官方 weknora mcp serve
→ 本地 WeKnora
```

模板没有自定义 MCP Server、过滤代理或写操作适配器。后端是官方 CLI 提供的
固定 10 工具面：`kb_list`、`kb_view`、`doc_list`、`doc_view`、`doc_download`、
`search_chunks`、`chunk_list`、`agent_list` 是读取/检索工具；`chat` 与
`session_ask` 会创建会话或消息记录。它不暴露建库、上传、修改或删除文档的工具，
但不能把全部 10 项统称为“零写入”。

## 准备

1. `scripts/configure-weknora.ps1` 已完成且 `bin/weknora.exe doctor` 成功。
2. 域名已托管到自己的 Cloudflare 账号。
3. 运行 `scripts/bootstrap.ps1 -InstallMcpTools`。脚本固定并验证
   `mcp-auth-proxy v2.10.2` 的 SHA-256，并验证 cloudflared 的 Authenticode 签名。
4. 运行 `mcp-public/set-password.ps1`。密码至少10个字符且不超过72个UTF-8字节；
   仓库只保存 bcrypt 哈希到忽略目录，不保存明文密码。
5. 在 WeKnora 中创建独立、最小权限的 API Key：只选择 `retrieve` 能力，只勾选
   准备公开给 ChatGPT 的知识库；不要授予 `ingest`、`manage_kbs` 或其他管理能力。
   随后运行：

```powershell
powershell -File .\mcp-public\configure-readonly-profile.ps1
```

Key 交给官方 CLI 的凭据机制，不写入仓库、参数、脚本或日志。CLI 会优先使用操作
系统 Keyring；不可用时按其官方行为回退到当前用户配置目录中的权限受限文件。不要
复制该目录，也不要复用建库、入库或管理员 Profile；`kb_list` 和 `doc_download`
能够读取这个 Key 范围内的全部内容。

## 创建 Tunnel

```powershell
powershell -File .\mcp-public\setup-cloudflare.ps1 `
  -Hostname mcp.your-domain.example -CreateDnsRoute
```

`-CreateDnsRoute` 会修改该主机名的 Cloudflare DNS。省略它可先只生成本地配置并
查看提示。无论是否修改 DNS，脚本都可能打开浏览器完成 Cloudflare 登录、在
`%USERPROFILE%\.cloudflared\cert.pem` 保存账户证书，并创建一个持久 Tunnel；如果
同名 Tunnel 已存在，脚本会停止；确认归属后必须显式增加 `-ReuseExistingTunnel`
才能复用。脚本会把 `.cloudflared` 凭据目录收紧为当前用户和 SYSTEM 可访问。共享
机器仍应使用唯一的 `-TunnelName`。脚本不公开 WeKnora 8080，只把公网主机名导向
本机 OAuth 代理。

## 可选 Compose Profile 的端口

模板默认只启动 WeKnora 核心服务，并把 8080/8088 绑定到 `127.0.0.1`。不要在未
检查上游 `docker-compose.yml` 的情况下直接启用 Neo4j、MinIO、Qdrant、Milvus、
Weaviate、Doris、Dex、Langfuse 或 Python MCP 等可选 Profile：其中部分 Profile
会把额外端口绑定到所有网卡。需要启用时，应先在本地 Compose override 中把每个
宿主端口显式绑定到 `127.0.0.1`，并更换所有默认密码。

## 启动与本地确认

```powershell
powershell -File .\mcp-public\start-all.ps1 `
  -ExternalUrl https://mcp.your-domain.example
powershell -File .\mcp-public\test-local.ps1
powershell -File .\mcp-public\status.ps1
```

启动脚本不会只看代理端口。公开 Tunnel 之前，它会用专用的 `mcp-readonly`
Profile 重新执行 CLI doctor、确认父块、子块和原文三个已配置知识库都对该 Key
可见，并在父块库完成一次限制为1条结果的真实混合检索。凭据失效、任一层权限
丢失或 Embedding 链路不可用时启动会直接失败，不会把 `/healthz` 正常误报为
“ChatGPT 已可检索”。

确认公网 `https://mcp.your-domain.example/healthz` 可达后，在实际使用的 ChatGPT
Business/Enterprise Workspace 中创建自定义应用：

- 服务器 URL：`https://mcp.your-domain.example/mcp`
- 身份验证：OAuth
- 完成 OAuth 登录并启用实时访问或索引搜索
- 审查工具列表，确认没有 create/delete/update/upload 等知识库写工具
- 严格检索模式下，由 Workspace 管理员禁用 `chat` 和 `session_ask`
- 先保存为草稿，真实检索成功后再按 Workspace 范围发布

账号邮箱相同不代表 Workspace 配置共享。应用必须安装到实际使用它的 Workspace。

## 停止

```powershell
# 停止由 start-all.ps1 启动的公网层、WeKnora 和本次手动 WSL 保活。
# 脚本会自动读取启动时记录的 WSL 发行版。
powershell -File .\scripts\stop.ps1 -StopWeKnora

# 若只想断开 ChatGPT 公网连接、保留本地 WeKnora，则仅运行：
powershell -File .\mcp-public\stop.ps1
```

OAuth 数据、Tunnel 凭据、日志和 PID 均位于 Git 忽略目录。不要将这些文件贴到
Issue、日志附件或屏幕截图中。
