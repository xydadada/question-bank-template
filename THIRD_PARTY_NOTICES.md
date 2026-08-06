# Third-party components

This repository does not redistribute the following projects. Bootstrap scripts
download or build them from their official locations; their own licenses apply.

- [Tencent WeKnora](https://github.com/Tencent/WeKnora), pinned to `v0.7.1`
  commit `c64a48647cd6f7eb8b0fb020b2e8fec74ee375fb`
- [MinerU API](https://mineru.net/doc/docs/)
- [Ollama](https://ollama.com/)
- [qwen3-embedding](https://ollama.com/library/qwen3-embedding)
- [sigbit/mcp-auth-proxy](https://github.com/sigbit/mcp-auth-proxy), pinned to
  `v2.10.2`
- [Cloudflare cloudflared](https://github.com/cloudflare/cloudflared), pinned to
  `2026.7.3` by the current bootstrap script
- [7-Zip](https://www.7-zip.org/)

Direct Python dependencies are downloaded from PyPI under their own licenses:

- bcrypt — Apache-2.0
- Pillow — HPND
- pypdf — BSD-3-Clause
- pypdfium2 — BSD-3-Clause and Apache-2.0; its bundled PDFium components retain
  their upstream licenses and notices
- python-dotenv — BSD-3-Clause
- PyYAML — MIT
- Requests — Apache-2.0
- optional ONNX Runtime — MIT
- optional RapidOCR — Apache-2.0

Resolved versions and artifact hashes are recorded in `uv.lock`. This project
does not use or require PyMuPDF.
