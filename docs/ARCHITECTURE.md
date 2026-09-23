# Architecture

The first-run entry point is `启动题库设置.cmd` → `scripts/wizard.ps1`. The wizard collects local configuration and calls the existing bootstrap, WeKnora setup, model selection, input staging, lifecycle, and MCP scripts. `ingest.py` remains responsible for document processing; WeKnora, Ollama, and the official MCP components retain their respective roles.

The template keeps orchestration separate from user data and upstream systems:

```text
input discovery / archive classification
  → MinerU parsing
  → page-image recovery and MiMo descriptions
  → question-answer grouping
  → taxonomy classification
  → parent / child / raw Markdown
  → three WeKnora knowledge bases
  → weighted hybrid retrieval
```

`ingest.py` owns orchestration, resumable state and guarded cleanup. MinerU owns
document parsing; MiMo owns cloud image understanding; WeKnora owns indexing,
vector storage, BM25 and the MCP tool surface. Upstream components stay on their
official sources and reviewed releases.

Local state is SQLite (`state.db`) with WAL enabled. Each clone creates an empty,
local execution record.
