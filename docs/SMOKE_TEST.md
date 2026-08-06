# 最小冒烟确认

公开仓库的自动测试会验证语法、安全默认值、可发布文件和 Git 历史，但无法代替
下载者自己的 MinerU、MiMo、WeKnora 和 Ollama 凭据。首次使用只需用一份可丢弃的
小 PDF 确认真实链路，不需要建立大型测试集。

## 准备

1. 完成 README 的最短安装路径并登录本地 WeKnora。
2. 运行 `scripts/configure-weknora.ps1`，确认三个知识库 ID 已写入被忽略的
   `config.local.yaml`。
3. 在被忽略的 `.env` 中填写自己的 MinerU Key；需要补图或模型兜底分类时再填写
   MiMo Key。
4. 保持 `config.local.yaml` 中全部永久删除选项为 `false`，不要填写
   `QUESTION_BANK_ALLOW_PERMANENT_DELETE`。
5. 只把一份可丢弃、无隐私内容的小 PDF 放入 `inbox`。

## 执行

```powershell
powershell -File .\scripts\doctor.ps1
powershell -File .\scripts\start.ps1 -Processing
powershell -File .\scripts\status.ps1
```

等待该文件完成后，执行一个与样本文本明确相关的查询：

```powershell
uv run python ingest.py --search "样本文档中的明确关键词"
```

## 只确认这些结果

- `markdown` 中产生分类后的最终 Markdown；
- 父块、子块和原文三层均成功入库；
- 查询返回至少一条相关结果，并包含可识别的标题或文件名；
- 源 PDF 仍然存在，因为永久删除默认关闭；
- 失败时源文件被保留，日志没有输出 API Key。

完成后可运行：

```powershell
powershell -File .\scripts\stop.ps1 -StopWeKnora
```

只有上述确认完成后，才应自行评估是否启用永久删除。真实云端耗时和费用取决于
下载者的账号、文件页数、图片数量及供应商限额，不属于仓库 CI 的保证范围。
