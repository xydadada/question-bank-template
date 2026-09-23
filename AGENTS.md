# Project notes

- `ingest.py` owns the document pipeline and retrieval checks. `model_manager.py` owns role-based model selection and on-demand installs. `scripts/` owns Windows setup and lifecycle; `mcp-public/` owns the read-only ChatGPT connection.
- Read [architecture](docs/ARCHITECTURE.md) and [known limits](docs/KNOWN_LIMITATIONS.md) before changing integration boundaries.
- Keep generated configuration, credentials, documents, state databases, downloaded models, and external binaries out of Git. Use synthetic fixtures for tests.
- Test changes without connecting to a maintainer's live knowledge base or accounts. A passing health endpoint alone does not prove retrieval works.
