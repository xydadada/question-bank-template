# 本地与云端模型

模型按用途选择。仓库保存模型目录和选择结果，权重由 Ollama、MinerU 或云端
提供商管理。选中本地预设后再执行安装，未选中的模型不会下载。

## 角色

| 角色 | 作用 | 当前运行时 |
|---|---|---|
| `parser` | PDF、Office、图片转 Markdown | MinerU 本地 CLI / MinerU API |
| `ocr` | 图片文字的低成本转写 | RapidOCR / Ollama OCR 模型 / 关闭 |
| `vision` | 题图、几何图、电路图和实验图说明 | Ollama VLM / MiMo |
| `classification` | 规则无法确定时分类资料 | Ollama 文本模型 / MiMo |
| `embedding` | WeKnora 向量化与查询 | Ollama / WeKnora 支持的远端模型 |
| `rerank` | 候选重排 | 默认关闭；可使用 WeKnora 支持的远端模型 |
| `chat` | Wiki、图谱或本地问答候选 | Ollama / 云端；基础检索可关闭 |

Rerank 保持独立角色。Ollama 模型目录中的生成模型和 Embedding 模型不能自动
当作 Reranker 使用；目录只允许把模型放入它声明支持的角色。

## 选择和按需安装

列出预设与模型：

```powershell
uv run python model_manager.py list
```

轻量本地方案：

```powershell
uv run python model_manager.py select local-light
uv run python model_manager.py install
uv run python model_manager.py status
uv run python model_manager.py hardware
```

`select` 只生成被 Git 忽略的 `models.local.yaml`。`install` 随后完成三类动作：

- 通过 Ollama 官方 Pull API 下载已选模型；
- 需要 RapidOCR 时安装 `ocr` 可选依赖；
- 需要本地 MinerU 时，在 `.runtime/mineru` 创建隔离环境并安装官方
  `mineru[all]` 包。

`status` 同时显示当前预设、Ollama 已安装模型和本机内存、显存、磁盘概况。
安装器会在明确低于上游最低资源要求时停止，并保留五个百分点的硬件标称容量误差。

首次本地 MinerU 解析可能继续下载所选后端需要的模型权重。所有下载均位于本地
运行时目录，仓库不会保存或提交权重。Windows 下隔离环境固定使用 Python 3.12，
与 MinerU 当前的 Windows 支持范围一致；安装前还会检查 20GB 磁盘余量。

## 自带预设

- `local-light`：MinerU `pipeline`、RapidOCR、0.8B 视觉、0.6B 分类、
  0.6B Embedding；本地语言模型较小。MinerU `pipeline` 仍需要至少 16GB 内存和
  20GB 可用磁盘，适合满足该条件且希望纯 CPU 解析的机器。
- `local-balanced`：本地 MinerU VLM、2B 视觉、0.6B Embedding；需要 CUDA
  环境，速度和显存占用取决于 MinerU 后端。
- `local-quality`：4B 视觉、4B Embedding 和 8B 文本模型；面向显存、内存与
  磁盘更充足的机器，Embedding 维度为 2560，需要新建知识库。
- `hybrid`：MinerU 云端解析，其余关键环节留在本机。
- `cloud`：保留 MinerU API 与 MiMo 的既有路径，Embedding 仍可在本地运行。

预设只是起点。复制一个 YAML 到 `models/presets`，再从
`models/catalog.yaml` 中为每个角色挑选兼容条目即可组合自己的方案。

已经下载或新发布的 Ollama 模型可以加入用户目录，再替换单个角色：

```powershell
uv run python model_manager.py use-ollama vision vendor/model:tag
uv run python model_manager.py install
```

如果该标签已经位于内置目录，命令会直接复用它的能力声明；陌生标签会
自动写入本地目录。命令只更改选择，已下载模型立即可用，未下载模型等到
`install` 时再拉取。

自定义 Embedding 还要声明实际输出维度：

```powershell
uv run python model_manager.py use-ollama embedding vendor/embed:tag --dimension 1024
```

这些自定义项写入被 Git 忽略的 `models.catalog.local.yaml`。目录可以容纳任意数量
的模型标签，选择器仍只下载当前角色引用的条目。角色能力由使用者显式声明，避免
根据名字猜测模型用途。

## Embedding 换模

文档建库和查询必须使用同一 Embedding 模型。切换模型或维度后，应创建新的
WeKnora 知识库并重建索引。已有知识库在包含文档时会锁定 Embedding 配置；
配置脚本也会拒绝静默覆盖不一致的模型 ID。

## 本地 MinerU

本地解析使用官方 CLI：

```text
mineru -p <input> -o <output> -b <backend>
```

模板把本地与云端结果归一为同一种 `full.md + content_list.json` 输入，再继续
图片补充、题答合并、分类和三层索引。`local-light` 使用 `pipeline`；
`local-balanced` 使用 `vlm-auto-engine`。本地模式不会读取 MinerU API Key，也
不会把文件提交到云端队列。

官方资料：

- [MinerU Quick Start](https://opendatalab.github.io/MinerU/quick_start/)
- [MinerU Docker Compose](https://github.com/opendatalab/MinerU/blob/master/docker/compose.yaml)
- [Ollama Pull API](https://docs.ollama.com/api/pull)
- [Ollama Vision](https://docs.ollama.com/capabilities/vision)
- [Ollama Embeddings](https://docs.ollama.com/capabilities/embeddings)
- [WeKnora Model API](https://github.com/Tencent/WeKnora/blob/main/docs/api/model.md)

## 资源与隐私

- 本地模型端点继续绑定回环地址。
- 云端角色只有在预设中明确选择后才会接收资料。
- 本地模型一次只保留必要数量，显存不足时由 Ollama 换出；公开模板不强制拉满
  并发。
- 模型目录记录来源、角色和维度；具体模型许可仍以模型发布页为准。
- 体积和显存提示属于规划值。量化版本、上下文和图片尺寸会改变实际占用。
