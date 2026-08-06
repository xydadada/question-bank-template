# Privacy model

The repository contains code and empty configuration templates only. A normal
installation creates user-owned data under ignored local directories.

Data may be sent to services selected by the operator:

- MinerU receives source documents selected for parsing.
- MiMo receives selected page images or classification excerpts when enabled.
- WeKnora stores generated knowledge locally unless the operator changes its
  storage or model providers.
- A configured MCP tunnel exposes the official WeKnora MCP tools to the
  connected ChatGPT workspace after OAuth authorization. Eight tools read or
  retrieve content; `chat` and `session_ask` create conversation records and
  should be disabled in the Workspace for strict retrieval-only use.

Review provider terms and the sensitivity of documents before supplying keys.
No telemetry is added by this template.
