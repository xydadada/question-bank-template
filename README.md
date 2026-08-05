# Question Bank Template

一个面向 Windows 11 + WSL2 的自托管题库流水线模板。它组合 MinerU、MiMo、
WeKnora、Ollama 和官方 WeKnora MCP，不包含任何作者的题库、密钥、账号、域名、
知识库 ID 或运行数据库。

> 当前版本是可复用的工程模板，不是托管服务。上传什么资料、使用什么 API、
> 是否公开 MCP，以及是否删除源文件，都由每位使用者自行决定。

## 它会做什么

```text
inbox 中的文件或压缩包
→ 分类文件类型并跳过视频
→ MinerU 解析
→ MiMo 补充重要图片说明
→ 题目与答案按资料组关联
→ 生成父块、子块和原文三层 Markdown
→ 自动分类并上传三个 WeKnora 知识库
→ 三层向量 + BM25 混合检索
→ 可选：只读 MCP + OAuth + Cloudflare Tunnel + ChatGPT
```

图片描述和不确定分类默认走 MiMo；本地只需运行 Embedding。Wiki、图谱、摘要
与永久删除默认关闭。

## 安全默认值

- 不提交 `.env`、`config.local.yaml`、知识库内容、Markdown、日志或 `state.db`。
- 不自动开机启动，不修改 Windows 计划任务，不开放 WeKnora 的 8080 端口。
- 不自动永久删除视频、压缩包、“其他资料”或已入库源文件。
- 开启任何永久删除选项后，仍需在本机 `.env` 明确写入
  `QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND`。
- MCP 使用 WeKnora 官方只读工具面；OAuth 代理只监听 `127.0.0.1:18081`。

先用可丢弃的小样本确认流程。永久删除无法撤销。

## 前置条件

- Windows 11、WSL2 Ubuntu
- Docker Desktop 或 WSL 中可用的 Docker Engine + Compose
- Git for Windows
- Python 3.11+（由 `uv` 管理项目环境）
- [uv](https://docs.astral.sh/uv/)
- Go 1.26+（只用于编译官方 WeKnora CLI）
- [Ollama](https://ollama.com/download)
- 7-Zip 的 `7z` 命令（仅压缩包输入需要）
- 自己的 MinerU API Key；使用图片补充或模型分类时还需 MiMo API Key

## 最短安装路径

```powershell
git clone https://github.com/xydadada/question-bank-template.git
cd question-bank-template
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -StartWeKnora
```

脚本只在仓库内创建忽略目录、Python 环境、`.runtime/WeKnora` 和
`bin/weknora.exe`。缺少需要管理员权限的系统组件时会停止并给出官方链接，
不会替你静默安装。

然后：

1. 打开 <http://127.0.0.1:8088>，创建或登录你自己的本地 WeKnora 账户。
2. 运行 `powershell -File .\scripts\configure-weknora.ps1`，在官方 CLI 中登录。
3. 把 `.env.example` 复制出的 `.env` 填入自己的 MinerU/MiMo Key。
4. 将少量可丢弃资料放入 `inbox`。
5. 启动处理：

```powershell
powershell -File .\scripts\start.ps1 -Processing
```

查看状态或停止：

```powershell
powershell -File .\scripts\status.ps1
powershell -File .\scripts\stop.ps1
powershell -File .\scripts\stop.ps1 -StopWeKnora
```

只做三层检索：

```powershell
uv run python ingest.py --search "你的检索词"
```

## 目录生命周期

| 目录 | 用途 | 是否提交 |
|---|---|---|
| `inbox/` | 等待处理的用户文件 | 否 |
| `archives/` | 默认保留的已展开压缩包 | 否 |
| `work/` | 下载包、分卷、截图等临时数据 | 否 |
| `markdown/` | 最终分类 Markdown | 否 |
| `failed/` | 失败并保留的源文件 | 否 |
| `outputs/` | 本地报告和人工删除清单 | 否 |
| `.runtime/` | WeKnora 源码、日志和 PID | 否 |
| `profiles/` | 可公开的分类规则模板 | 是 |

源文件删除不是默认行为。开启后，只有三层入库和检索检查完成的资料才进入删除
逻辑；失败资料仍保留。详细开关见 [配置说明](docs/CONFIGURATION.md)。

## ChatGPT 只读检索（可选）

```powershell
powershell -File .\scripts\bootstrap.ps1 -InstallMcpTools
powershell -File .\mcp-public\set-password.ps1
powershell -File .\mcp-public\setup-cloudflare.ps1 `
  -Hostname mcp.your-domain.example -CreateDnsRoute
powershell -File .\mcp-public\start-all.ps1 `
  -ExternalUrl https://mcp.your-domain.example
```

在实际使用的 ChatGPT Workspace 中，以 OAuth 方式添加
`https://mcp.your-domain.example/mcp`。完整步骤、安全边界和验证命令见
[ChatGPT MCP 指南](docs/CHATGPT_MCP.md)。

## 自定义分类

`profiles/physics-question-bank.yaml` 是物理题库示例。机构别名默认空白；下载者
应按自己的资料填写。更换学科时复制该文件、调整类型与模块词表，并在
`config.local.yaml` 修改 `document_classification.taxonomy_file`。

## 项目边界

本项目不重写 MinerU、WeKnora、Ollama、MCP、OAuth、向量数据库、Wiki 或
Neo4j。主要逻辑集中在 `ingest.py`，其余脚本只负责下载官方组件、生成本地配置
和启动停止。组件来源与固定版本见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 贡献与许可

提交代码前运行：

```powershell
powershell -File .\scripts\release-audit.ps1
uv run python -m unittest discover -s tests -v
```

`docs/audit.workflow.example.yml` 是等价的 GitHub Actions 示例。仓库管理员可在
具有 `workflow` 写权限时复制到 `.github/workflows/audit.yml` 启用自动检查。

代码采用 [MIT License](LICENSE)。第三方组件保留各自许可证。
