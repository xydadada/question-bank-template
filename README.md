# MinerU → WeKnora 题库模板

[中文](README.md) | [English](README.en.md)

[![Release](https://img.shields.io/github/v/release/xydadada/question-bank-template)](https://github.com/xydadada/question-bank-template/releases/latest)
[![Audit](https://img.shields.io/github/actions/workflow/status/xydadada/question-bank-template/audit.yml?branch=main&label=audit)](https://github.com/xydadada/question-bank-template/actions/workflows/audit.yml)
[![License](https://img.shields.io/github/license/xydadada/question-bank-template)](LICENSE)
[![Use this template](https://img.shields.io/badge/use_this-template-2ea44f)](https://github.com/new?template_owner=xydadada&template_name=question-bank-template)

这不是另一套 RAG 框架。这个仓库把题库入库前后的杂活接成一条可恢复的流水线：
MinerU 负责解析，MiMo 补题图说明，WeKnora 保存并检索父块、子块和原文，官方
WeKnora MCP 可以把只读检索接到 ChatGPT。

模板面向 Windows 11 和 WSL2。仓库里只有代码与空配置，不附带作者的题库、密钥、
账号、域名、知识库 ID 或运行数据库。它也不是托管服务。资料和服务账号始终由使用者
自己管理。

## 处理流程

```text
inbox 中的文件、文件夹或压缩包
→ 识别文件类型，视频不进入解析
→ MinerU 解析文档
→ MiMo 判断并描述重要题图
→ 按资料组关联题目与答案
→ 生成父块、子块和原文三层 Markdown
→ 按资料类型、机构和物理模块分类
→ 上传三个 WeKnora 知识库
→ 向量与 BM25 混合检索
→ 可选：OAuth + Cloudflare Tunnel + 官方 WeKnora MCP + ChatGPT
```

图片说明和不确定分类默认使用 MiMo。本机只需要运行 Embedding。Wiki、图谱、摘要
和 Rerank 不在默认流程中，永久删除也默认关闭。

## 这个模板适合什么情况

- 资料来源杂，既有 PDF，也有 Office 文档、图片、文件夹或压缩包。
- 题目和答案可能分开保存，希望合并后再检索。
- 希望保留整份原文，同时检索较小的题答块和带上下文的父块。
- 希望 ChatGPT 能读取本地题库，但不想自己重写 OCR、RAG 或 MCP 服务。

它没有替换 MinerU、WeKnora、Ollama、OAuth、向量数据库、Wiki 或 Neo4j。
`ingest.py` 只负责在这些现成组件之间传递文件、记录状态并处理失败恢复。

## 安全默认值

模板默认采用保守设置：

- `.env`、`config.local.yaml`、题库、Markdown、日志和 `state.db` 都被 Git 忽略。
- 脚本不会创建开机启动项，也不会修改 Windows 计划任务。
- WeKnora 的 8080 和 8088 端口只绑定 `127.0.0.1`。
- Redis 和 Neo4j 使用 `unless-stopped`，手动停止后不会因 Windows 或 Docker 重启而自行恢复。
- 压缩包会检查路径、链接、成员数量和展开量，嵌套压缩包共用同一份安全预算。
- 视频、原压缩包、`其他资料`和已入库源文件都不会被自动永久删除。
- MCP 使用单独的最小权限 Profile，OAuth 代理只监听 `127.0.0.1:18081`。

永久删除需要同时修改配置，并在本机 `.env` 写入：

```dotenv
QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND
```

人工删除级联同步使用另一道确认门：

```dotenv
QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND
```

两种确认不能互相代替。首次运行请使用无隐私、可丢弃的小样本，并保持全部删除开关为
`false`。具体步骤见[最小冒烟确认](docs/SMOKE_TEST.md)。

## 运行前需要准备

- Windows 11 和 WSL2 Ubuntu
- Docker Desktop，建议启用 Ubuntu 的 WSL integration
- Git for Windows
- Python 3.11+，项目环境由 `uv` 管理
- [uv](https://docs.astral.sh/uv/)
- Go 1.26+，只用于编译官方 WeKnora CLI
- [Ollama](https://ollama.com/download)
- 7-Zip 的 `7z` 命令，仅在处理压缩包时需要
- 自己的 MinerU API Key
- 使用图片说明或模型兜底分类时，还需要自己的 MiMo API Key

默认组合是 Docker Desktop、WSL2 Ubuntu 和 Windows Ollama。原生 WSL Docker 也能
使用，但需要自行处理容器到 Ollama 的网络，不能假定
`host.docker.internal:11434` 一定可达。

## 最短安装路径

```powershell
git clone https://github.com/xydadada/question-bank-template.git
cd question-bank-template
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -StartWeKnora
```

引导脚本只在仓库内创建 Git 忽略目录、Python 环境、`.runtime/WeKnora` 和
`bin/weknora.exe`。如果缺少需要管理员权限的系统组件，脚本会停止并给出官方链接，
不会替你静默安装。WeKnora 固定到经过核验的 Release 提交。

安装完成后：

1. 打开 <http://127.0.0.1:8088>，创建或登录本地 WeKnora 账户。
2. 运行 `powershell -File .\scripts\configure-weknora.ps1`，在官方 CLI 中登录。
3. 从 `.env.example` 复制出 `.env`，填入自己的 MinerU 和 MiMo Key。
4. 先把少量可丢弃资料放入 `inbox`。
5. 启动处理。

```powershell
powershell -File .\scripts\start.ps1 -Processing
```

查看状态或停止：

```powershell
powershell -File .\scripts\status.ps1
powershell -File .\scripts\stop.ps1
powershell -File .\scripts\stop.ps1 -StopWeKnora
```

直接测试三层检索：

```powershell
uv run python ingest.py --search "你的检索词"
```

本地 OCR 是可选项。只有明确启用它时才需要安装额外依赖，并在
`config.local.yaml` 中把 `ollama.ocr_enabled` 改为 `true`。

```powershell
uv sync --extra ocr
uv run python -c "import rapidocr, onnxruntime; print('OCR extra ready')"
```

## 本地目录

| 目录 | 内容 | 是否提交 |
|---|---|---|
| `inbox/` | 等待处理的用户文件 | 否 |
| `archives/` | 默认保留的已展开压缩包 | 否 |
| `work/` | 下载包、分卷、截图等临时数据 | 否 |
| `markdown/` | 最终分类 Markdown | 否 |
| `failed/` | 失败并保留的源文件 | 否 |
| `outputs/` | 本地报告和人工删除清单 | 否 |
| `.runtime/` | WeKnora 源码、日志和 PID | 否 |
| `profiles/` | 可公开的分类规则模板 | 是 |

源文件删除不是默认行为。启用后，程序也只会处理已完成三层入库和检索检查的资料。
失败文件仍会保留。开关说明见[配置说明](docs/CONFIGURATION.md)，程序会停止而不是
猜测处理的情况见[已知限制](docs/KNOWN_LIMITATIONS.md)。

## 接入 ChatGPT，可选

```powershell
powershell -File .\scripts\bootstrap.ps1 -InstallMcpTools
powershell -File .\mcp-public\configure-readonly-profile.ps1
powershell -File .\mcp-public\set-password.ps1
powershell -File .\mcp-public\setup-cloudflare.ps1 `
  -Hostname mcp.your-domain.example -CreateDnsRoute
powershell -File .\mcp-public\start-all.ps1 `
  -ExternalUrl https://mcp.your-domain.example
```

然后在实际使用的 ChatGPT Workspace 中，通过 OAuth 添加
`https://mcp.your-domain.example/mcp`。官方 WeKnora MCP 提供十个工具，其中
`chat` 和 `session_ask` 会创建会话记录。严格只读检索时应由 Workspace 管理员禁用
这两项，只保留八个读取和检索工具。完整步骤见[ChatGPT MCP 指南](docs/CHATGPT_MCP.md)。

## 分类规则

`profiles/physics-question-bank.yaml` 是物理题库示例，机构别名表有意保持空白。使用者
应按自己的资料填写。更换学科时可以复制该文件，修改类型和模块词表，再在
`config.local.yaml` 中设置 `document_classification.taxonomy_file`。

## 组件与项目边界

这个仓库使用以下现成项目，不维护它们的分叉版本：

- [MinerU](https://github.com/opendatalab/MinerU) 解析文档。
- [WeKnora](https://github.com/Tencent/WeKnora) 保存知识库、生成索引并提供官方 MCP。
- [Ollama](https://ollama.com/) 在本机运行 Embedding 和可选 OCR 模型。
- MiMo 处理重要图片说明和规则无法确定的资料分类。

公开模板不是作者私人运行目录的副本。下载者需要通过官方 CLI 创建自己的三个
知识库和 Profile。仓库不会提供现成题库、真实知识库 ID、Cloudflare Tunnel、账号、
域名、日志、运行数据库或私人构建的 WeKnora CLI。第三方来源和固定版本记录在
[第三方声明](THIRD_PARTY_NOTICES.md)中。

## 贡献与许可

提交修改前运行：

```powershell
powershell -File .\scripts\release-audit.ps1
uv run python -m unittest discover -s tests -v
```

GitHub Actions 会在 pull request 和推送到 `main` 时重复这些检查。贡献要求见
[CONTRIBUTING.md](CONTRIBUTING.md)。代码采用 [MIT License](LICENSE)，第三方组件
保留各自许可证。
