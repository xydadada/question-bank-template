# MinerU → WeKnora 题库模板

[中文](README.md) | [English](README.en.md)

[![Release](https://img.shields.io/github/v/release/xydadada/question-bank-template)](https://github.com/xydadada/question-bank-template/releases/latest)
[![Audit](https://img.shields.io/github/actions/workflow/status/xydadada/question-bank-template/audit.yml?branch=main&label=audit)](https://github.com/xydadada/question-bank-template/actions/workflows/audit.yml)
[![License](https://img.shields.io/github/license/xydadada/question-bank-template)](LICENSE)
[![Use this template](https://img.shields.io/badge/use_this-template-2ea44f)](https://github.com/new?template_owner=xydadada&template_name=question-bank-template)

我写这个项目，是因为把文件拖进知识库只占很少一部分时间。更多时间花在整理混杂资料、
等待云端解析、补回题图信息、配对题目与答案、保留可恢复状态，以及确认每一层确实能搜到。

这套模板把这些步骤接在一起。MinerU 解析文档，MiMo 描述重要题图，WeKnora 保存并
检索父块、子块和原文。需要时，官方 WeKnora MCP 可以再把三层检索接到 ChatGPT。
它的定位是本机运行的编排模板，直接复用现成组件。

目前维护和验证的环境是 Windows 11、WSL2、Docker Desktop 和 Windows 版 Ollama。
仓库提供代码、空配置和合成示例。题库、密钥、账号、域名、知识库 ID 和运行数据库均由
下载者在自己的环境中创建和管理。

> **项目状态：** 这是仍在演进的 alpha 模板。自动测试覆盖本地状态、安全门、脚本和
> 失败恢复。MinerU、MiMo、WeKnora 和 ChatGPT 账号需要在实际环境中确认。第一次请
> 使用可丢弃的小文件，并将所有永久删除开关保持为 `false`。

![从源文件到三层检索的处理流程](assets/pipeline.png)

## 它实际做什么

```text
inbox 中的文件、文件夹或压缩包
→ 识别文件类型，视频归入忽略分类
→ MinerU 解析文档
→ MiMo 判断并描述重要题图
→ 按资料组关联题目与答案
→ 生成父块、子块和原文三层 Markdown
→ 按资料类型、机构和物理模块分类
→ 上传三个 WeKnora 知识库
→ 向量与 BM25 混合检索
→ 可选：OAuth + Cloudflare Tunnel + 官方 WeKnora MCP + ChatGPT
```

图片说明和疑难分类默认使用 MiMo，本机运行 Embedding。核心检索稳定后可以按需启用
Wiki、图谱、摘要和 Rerank。永久删除开关初始为 `false`。

## 这个模板适合什么情况

- 资料来源杂，既有 PDF，也有 Office 文档、图片、文件夹或压缩包。
- 题目和答案可能分开保存，希望合并后再检索。
- 希望保留整份原文，同时检索较小的题答块和带上下文的父块。
- 希望 ChatGPT 能读取本地题库，并直接复用现成的 OCR、RAG 和 MCP 组件。

MinerU 负责解析，WeKnora 负责知识库和检索，Ollama 负责本地模型，OAuth 负责授权，
Wiki 和 Neo4j 继续使用 WeKnora 的现成功能。`ingest.py` 在这些组件之间传递文件、记录
状态并处理失败恢复。

当前维护范围是 Windows 11 和 WSL2 上的自托管题库处理。资料与模型训练由使用者自行
准备；macOS 和原生 Linux 用户需要改写安装入口。只导入几份普通 PDF 时，直接使用
WeKnora 会更省事。

## 先看输出

准备安装前可以先看[最小结构示例](examples/minimal-physics/README.md)。示例用一份仓库
原创的弹簧振子题，展示分开的题目与答案如何变成父块、子块和原文三层。本地阅读即可
看到完整结构，内容全部来自仓库的合成样本。

## 安全默认值

默认配置优先保留文件，删除动作需要使用者明确授权：

- `.env`、`config.local.yaml`、题库、Markdown、日志和 `state.db` 都被 Git 忽略。
- 所有脚本由用户手动启动，Windows 计划任务保持原状。
- WeKnora 的 8080 和 8088 端口只绑定 `127.0.0.1`。
- Redis 和 Neo4j 使用 `unless-stopped`，手动停止状态会跨 Windows 或 Docker 重启保留。
- 压缩包会检查路径、链接、成员数量和展开量，嵌套压缩包共用同一份安全预算。
- 视频、原压缩包、`其他资料`和已入库源文件默认保留。
- MCP 使用单独的最小权限 Profile，OAuth 代理只监听 `127.0.0.1:18081`。

永久删除需要同时修改配置，并在本机 `.env` 写入：

```dotenv
QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND
```

人工删除级联同步使用另一道确认门：

```dotenv
QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND
```

两类操作各自使用独立确认门。首次运行请使用无隐私、可丢弃的小样本，并保持全部删除
开关为 `false`。具体步骤见[最小冒烟确认](docs/SMOKE_TEST.md)。

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

默认组合是 Docker Desktop、WSL2 Ubuntu 和 Windows Ollama。使用原生 WSL Docker 时，
需要另外配置容器到 Ollama 的网络，并为 `host.docker.internal:11434` 设置可达路径。

## 最短安装路径

先运行 `scripts/doctor.ps1` 可以检查当前克隆是否已经具备检索或处理条件。首次安装仍
从引导脚本开始：

```powershell
git clone https://github.com/xydadada/question-bank-template.git
cd question-bank-template
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -StartWeKnora
```

引导脚本在仓库内创建 Git 忽略目录、Python 环境、`.runtime/WeKnora` 和
`bin/weknora.exe`。遇到需要管理员权限的系统组件时，脚本会停止并给出官方链接，系统
安装仍由使用者决定。WeKnora 固定到经过核验的 Release 提交。

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

| 目录 | 内容 | Git 状态 |
|---|---|---|
| `inbox/` | 等待处理的用户文件 | 忽略 |
| `archives/` | 默认保留的已展开压缩包 | 忽略 |
| `work/` | 下载包、分卷、截图等临时数据 | 忽略 |
| `markdown/` | 最终分类 Markdown | 忽略 |
| `failed/` | 失败并保留的源文件 | 忽略 |
| `outputs/` | 本地报告和人工删除清单 | 忽略 |
| `.runtime/` | WeKnora 源码、日志和 PID | 忽略 |
| `profiles/` | 可公开的分类规则模板 | 跟踪 |

默认配置保留源文件。启用删除后，程序只处理已经完成三层入库和检索检查的资料，失败文件
继续保留。开关说明见[配置说明](docs/CONFIGURATION.md)，触发主动停止的情况见
[已知限制](docs/KNOWN_LIMITATIONS.md)。

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

这个仓库直接使用以下现成项目及其官方版本：

- [MinerU](https://github.com/opendatalab/MinerU) 解析文档。
- [WeKnora](https://github.com/Tencent/WeKnora) 保存知识库、生成索引并提供官方 MCP。
- [Ollama](https://ollama.com/) 在本机运行 Embedding 和可选 OCR 模型。
- MiMo 处理重要图片说明和规则无法确定的资料分类。

公开仓库是一份可复用模板。下载者通过官方 CLI 创建自己的三个知识库和 Profile，并在
本机保存题库、知识库 ID、Cloudflare Tunnel、账号、域名、日志和运行数据库。仓库跟踪
的是代码、空配置、合成示例及公开规则模板。第三方来源和固定版本记录在
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
