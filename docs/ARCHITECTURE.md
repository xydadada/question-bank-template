# Architecture

The first-run entry point is `启动题库设置.cmd` → `scripts/wizard.ps1`. The wizard collects local configuration and calls the existing bootstrap, WeKnora setup, model selection, input staging, lifecycle, and MCP scripts. `ingest.py` remains responsible for document processing; WeKnora, Ollama, and the official MCP components retain their respective roles.

The wizard has separate checks for command prerequisites and completed runtime configuration. During WeKnora setup, the local Ollama Embedding dimension is checked first; then WeKnora calls that model through its configured route before any of the three knowledge bases are created. This setup path currently accepts Ollama Embedding only.

The template keeps orchestration separate from user data and upstream systems:

```text
input discovery / archive classification
  → selected MinerU local or cloud parser
  → page-image recovery and selected OCR / vision provider
  → question-answer grouping
  → taxonomy classification
  → parent / child / raw Markdown
  → three WeKnora knowledge bases
  → weighted hybrid retrieval
```

`ingest.py` owns orchestration, resumable state and guarded cleanup. `model_manager.py`
resolves role selections and installs only the selected local runtimes. MinerU owns
document parsing; Ollama, RapidOCR or MiMo own optional model inference; WeKnora owns
indexing, vector storage, BM25 and the MCP tool surface. Upstream components stay on
their official sources and reviewed releases.

Local state is SQLite (`state.db`) with WAL enabled. Each clone creates an empty,
local execution record.
