# Architecture

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
vector storage, BM25 and the MCP tool surface. No upstream source is vendored or
modified.

Local state is SQLite (`state.db`) with WAL enabled. It is an execution record,
not a distributable sample database. Every clone starts empty.
