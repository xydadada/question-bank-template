# 配置说明

公开仓库只保存 `config.example.yaml` 和 `.env.example`。实际运行使用被 Git
忽略的 `config.local.yaml` 与 `.env`。

## 必填项

- 云端 MinerU：`.env` 中至少一个 `MINERU_API_TOKEN`；本地 MinerU 无需 Key。
- 选择 MiMo 图片说明或云端分类时：至少一个 `MIMO_API_KEY`。
- `config.local.yaml`：三个知识库 ID。`configure-weknora.ps1` 会自动创建并填写。

## 模型选择

`model_selection.file` 默认指向被 Git 忽略的 `models.local.yaml`。该文件存在时，
其角色选择会覆盖 `mineru`、`ollama` 和 `weknora.models` 中对应的旧式字段；文件
缺失时现有配置行为保持不变。预设、可选模型和换 Embedding 的规则见
[本地与云端模型](LOCAL_MODELS.md)。

同一提供商、同一账号下的多个 Key 不一定拥有独立限额。流水线会弹性调整并发，
但不会把“Key 数量”直接当作吞吐量保证。

## 三层索引

- `parent`：完整资料组及题答关系，适合获取上下文。
- `child`：按题答和段落边界生成的较细粒度块，不按固定字符硬切题目。
- `raw`：最终合并 Markdown 原文。

三个层级同时使用 WeKnora 的向量与 BM25 混合检索。`layer_weights` 和
`document_type_weights` 可调，但它们只影响 `ingest.py --search` 的本地三层
聚合与排序，不会改写官方 WeKnora MCP 的服务端排序。ChatGPT 侧仍会看到三层
知识库，并根据工具描述、查询词和返回结果决定继续搜索哪一层。修改本地权重无需
改 Python 源码。

`chunk_sizes` 为 `0` 时接受用户在 WeKnora 中配置的分块大小。填正整数时，启动
预检会要求服务器配置完全匹配。

## 资源控制

默认值面向 16GB 内存的单机，初始并发较保守，并会根据可用内存收缩。先观察
一个小样本，再提高：

- `resource_control.local_processing_workers_max`
- `resource_control.prequeue_workers_max`
- `ollama.mimo.parallel_cap`
- `ollama.mimo.parallel_per_key`

不要把 Wiki、图谱重建与大批量入库同时开启。

`mineru.max_mb` 同时是单个非 PDF 源文件和直接文本文件的本地处理上限。
程序会在整文件哈希或读入前拒绝超限文件，避免用单个异常大的 Markdown/
TXT 耗尽内存。MinerU 结果 ZIP 还有固定的下载量、成员数和展开量上限，
解压前会保留至少512MB磁盘安全余量。

## 永久删除

以下开关在示例中全部为 `false`：

- `classification.delete_videos`
- `classification.delete_archives_after_extract`
- `document_classification.delete_other_source_after_markdown`
- `cleanup.permanently_delete_source_after_search`

只要其中任意一个为 `true`，程序还会要求 `.env` 中存在：

```dotenv
QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND
```

非演练模式的 `--sync-manual-deletions` 和
`manual_deletions.auto_sync: true` 使用独立确认门：

```dotenv
QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND
```

`--manual-deletion-dry-run` 只统计范围，不要求确认，也不会执行删除。开启视频或
源文件清理不会顺带授权人工删除级联。

这道确认门负责防误操作；资料保护仍应使用你自己的备份。建议始终先复制少量可丢弃
文件做首次确认。
