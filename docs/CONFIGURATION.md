# 配置说明

公开仓库只保存 `config.example.yaml` 和 `.env.example`。实际运行使用被 Git
忽略的 `config.local.yaml` 与 `.env`。

## 必填项

- `.env`：至少一个 `MINERU_API_TOKEN`。
- 需要图片说明或模型兜底分类时：至少一个 `MIMO_API_KEY`。
- `config.local.yaml`：三个知识库 ID。`configure-weknora.ps1` 会自动创建并填写。

同一提供商、同一账号下的多个 Key 不一定拥有独立限额。流水线会弹性调整并发，
但不会把“Key 数量”直接当作吞吐量保证。

## 三层索引

- `parent`：完整资料组及题答关系，适合获取上下文。
- `child`：按题答和段落边界生成的较细粒度块，不按固定字符硬切题目。
- `raw`：最终合并 Markdown 原文。

三个层级同时使用 WeKnora 的向量与 BM25 混合检索。`layer_weights` 和
`document_type_weights` 可调；修改后无需改 Python 源码。

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

这是一道防误操作门，不是备份。建议始终先复制少量可丢弃文件做首次确认。
