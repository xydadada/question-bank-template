# 社区介绍稿

这些文字供维护者发布到上游社区。发布前应重新检查仓库链接、当前版本和实际功能。
介绍时请明确标注社区模板身份，并把速度、准确率和硬件兼容性限定在已经测量的范围内。

## WeKnora Show and tell

建议标题：

```text
用 MinerU 整理题库，再用 WeKnora 做父块、子块和原文三层检索
```

正文：

```text
我整理了一个面向 Windows 11 和 WSL2 的题库处理模板。它直接组合 MinerU、WeKnora、
Ollama 和现成的 OAuth 代理，把解析、整理、入库和检索接成一条可恢复流程。

做这个模板是因为手头的资料并不整齐：PDF、Office 文件、文件夹和压缩包混在一起，
题目与答案有时分成两个文件。只上传整本资料不方便搜题，只切成小块又容易丢掉上下文。

模板会先解析和配对资料，再同时生成三层 Markdown：

1. 父块保存一道完整题目和答案；
2. 子块用于较精确的题目检索；
3. 原文层保留整份资料和来源顺序。

三层分别进入 WeKnora 知识库，通过向量和 BM25 检索。需要连接 ChatGPT 时，模板直接
使用 WeKnora 官方 MCP，并保持 WeKnora 源码与 MCP Server 使用官方版本。

仓库默认保留源文件，永久删除开关初始为 `false`。Wiki、图谱、摘要和 Rerank 可以在
核心检索稳定后启用。本地配置、题库、日志、密钥和数据库由 `.gitignore` 隔离。

仓库：
https://github.com/xydadada/question-bank-template

最小结构示例：
https://github.com/xydadada/question-bank-template/tree/main/examples/minimal-physics

这个项目目前最想确认的是三层资料结构在其他题库中的适用性，以及官方 MCP 在只读检索
场景下还需要补充哪些说明。
```

English version:

```text
I put together a Windows 11 and WSL2 template for turning mixed question-bank material into
three WeKnora retrieval layers. MinerU parses the documents, the template pairs questions
with answers, and WeKnora owns indexing, hybrid retrieval, and the MCP tool surface.

The collection that prompted this work mixed PDFs, Office files, folders, and archives, with
some questions and solutions stored separately. Indexing only the
full documents made individual problems hard to find. Indexing only small chunks removed too
much context.

The template therefore writes three Markdown layers:

1. a parent record containing a complete question and its solution;
2. a focused child record for problem-level retrieval;
3. a raw record that preserves the full source order.

Each layer goes to a separate WeKnora knowledge base and remains available to vector and BM25
retrieval. The optional ChatGPT path uses the official WeKnora MCP and keeps WeKnora on its
reviewed upstream release.

The initial configuration retains source files and sets Wiki, graph extraction, summaries, and
Rerank to `false`. User documents, credentials, logs, local configuration, and runtime databases
stay outside Git.

Repository:
https://github.com/xydadada/question-bank-template

Minimal structure example:
https://github.com/xydadada/question-bank-template/tree/main/examples/minimal-physics

Feedback on the three-layer document model and the documentation around retrieval-only MCP use
would be useful.
```

## MinerU Show and tell

建议标题：

```text
MinerU 解析后的题库整理、题答配对与 WeKnora 三层入库模板
```

正文：

```text
我用 MinerU 做主解析器，整理了一个公开的题库入库模板。模板负责解析前后的衔接工作，
MinerU 继续负责文档解析。

输入可以是普通文档、文件夹或压缩包。脚本先分类文件，视频归入忽略类别，需要解析的
资料提交给 MinerU。解析完成后，模板再处理题目与答案配对、重要题图说明、资料分类，
并生成父块、子块和原文三层 Markdown，最后交给 WeKnora 建立索引。

流程使用 SQLite 记录可恢复状态。大文件可以分卷提交，恢复时会检查分卷结果是否完整且
一一对应。网络超时、429 和服务端错误进入临时失败队列，源文件继续留在待处理目录。

仓库提供代码、空配置和一份虚构的物理题目示例。真实题库、API Key、账号、知识库 ID
和运行数据库由使用者在本机创建和管理。永久删除开关初始为 `false`。

仓库：
https://github.com/xydadada/question-bank-template

处理流程与最小示例：
https://github.com/xydadada/question-bank-template#它实际做什么
https://github.com/xydadada/question-bank-template/tree/main/examples/minimal-physics

这是独立维护的社区模板。希望了解这种解析后整理方式是否适合加入 MinerU 生态的社区
集成列表，以及当前 API 使用说明中还有哪些容易误解的地方。
```

English version:

```text
I use MinerU as the main parser in a public question-bank ingestion template. MinerU owns
document parsing, while the repository handles the work around it.

Input may be a regular document, folder, or archive. The script classifies files first and routes
video to the ignored category. After MinerU returns Markdown, the template pairs questions with
solutions, adds descriptions for important figures, classifies the material, and writes parent,
child, and raw Markdown layers for WeKnora.

SQLite stores resumable processing state. Large files can be split before submission, and the
recovery path checks that every returned part maps to exactly one expected part. Timeouts, HTTP
429 responses, and server errors enter the retry queue while source documents stay in place.

The repository contains code, blank configuration, and one synthetic physics example. Each
user creates and manages their question bank, credentials, accounts, knowledge-base IDs, and
runtime data locally. Permanent deletion starts with a `false` setting.

Repository:
https://github.com/xydadada/question-bank-template

Pipeline and minimal example:
https://github.com/xydadada/question-bank-template#它实际做什么
https://github.com/xydadada/question-bank-template/tree/main/examples/minimal-physics

This is an independently maintained community template. I would be interested in whether this
post-processing pattern belongs in the MinerU ecosystem's community integration list and which
parts of the API usage notes need clearer wording.
```
