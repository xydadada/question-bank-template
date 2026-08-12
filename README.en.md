# MinerU → WeKnora question bank template

[中文](README.md) | [English](README.en.md)

[![Release](https://img.shields.io/github/v/release/xydadada/question-bank-template)](https://github.com/xydadada/question-bank-template/releases/latest)
[![Audit](https://img.shields.io/github/actions/workflow/status/xydadada/question-bank-template/audit.yml?branch=main&label=audit)](https://github.com/xydadada/question-bank-template/actions/workflows/audit.yml)
[![License](https://img.shields.io/github/license/xydadada/question-bank-template)](LICENSE)
[![Use this template](https://img.shields.io/badge/use_this-template-2ea44f)](https://github.com/new?template_owner=xydadada&template_name=question-bank-template)

I started this project because the slow part of building a question bank was rarely the
upload itself. The work around it kept growing: sorting mixed files, waiting for cloud
parsing, restoring information from figures, pairing separate questions and solutions,
keeping enough state to resume, and checking that each index could retrieve its own data.

This template connects those steps. MinerU parses documents, MiMo describes important
figures, and WeKnora stores parent records, child records, and full Markdown. The official
WeKnora MCP can expose those three retrieval layers to ChatGPT when needed. This is an
orchestration project, not a new RAG framework or a hosted service.

The maintained setup is Windows 11 with WSL2, Docker Desktop, and Ollama on Windows. The
repository contains code, blank configuration, and synthetic examples. It does not ship
the maintainer's documents, keys, accounts, domains, knowledge-base IDs, or runtime
database. Each user supplies their own documents and service accounts.

> **Project status:** this is an alpha template under active development. Automated tests
> cover local state, safety interlocks, scripts, and failure recovery. They cannot validate
> your MinerU, MiMo, WeKnora, or ChatGPT account. Start with a disposable file and leave all
> permanent-deletion settings disabled.

![Pipeline from source files to three retrieval layers](assets/pipeline.png)

## What it does

```text
files, folders, or archives placed in inbox
→ classify file types and keep video out of document parsing
→ parse documents with MinerU
→ use MiMo to decide which figures matter and describe them
→ pair questions with answers at the document-group level
→ create parent, child, and raw Markdown layers
→ classify documents by type, institution, and physics module
→ upload the three layers to separate WeKnora knowledge bases
→ retrieve with vectors and BM25
→ optional: OAuth + Cloudflare Tunnel + official WeKnora MCP + ChatGPT
```

MiMo handles figure descriptions and classifications that the deterministic rules cannot
settle. The local machine only needs an Embedding model. Wiki generation, knowledge graphs,
summaries, and Rerank are not part of the default pipeline. Permanent deletion is off by
default.

## When this template fits

- Your source collection mixes PDFs, Office files, images, folders, and archives.
- Questions and answers may live in separate files and should be paired before indexing.
- You want full source text as well as smaller question-answer records and parent context.
- You want ChatGPT to retrieve from a local question bank without writing a new OCR, RAG,
  or MCP service.

The project does not replace MinerU, WeKnora, Ollama, OAuth, a vector database, Wiki, or
Neo4j. `ingest.py` moves data between those existing components, records resumable state,
and handles failure recovery.

It is also deliberately narrow. It is not a hosted web application, does not provide a
question-bank corpus, and does not train models. There is no maintained installer for macOS
or native Linux. If you only need to import a few ordinary PDFs, using WeKnora directly is
probably simpler.

## See the output first

The [minimal structure example](examples/minimal-physics/README.md) shows how a separate
question and solution become parent, child, and raw records. The oscillator problem was
written for this repository. Reading the example does not call a cloud API or require a real
question bank.

## Safe defaults

The default configuration preserves files rather than guessing what can be removed:

- Git ignores `.env`, `config.local.yaml`, documents, generated Markdown, logs, and
  `state.db`.
- The scripts do not create startup tasks or change Windows Task Scheduler.
- WeKnora ports 8080 and 8088 bind to `127.0.0.1` only.
- Redis and Neo4j use `unless-stopped`, so a manual stop survives a Windows or Docker
  restart.
- Archive inspection checks paths, links, member count, and expanded size. Nested archives
  share one safety budget.
- Video, source archives, documents classified as `other`, and successfully indexed source
  files are retained by default.
- MCP uses a separate least-privilege profile. The OAuth proxy listens on
  `127.0.0.1:18081`.

Any permanent deletion also requires this local acknowledgement in `.env`:

```dotenv
QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND
```

Explorer-style deletion propagation has a separate acknowledgement:

```dotenv
QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND
```

One acknowledgement cannot authorize the other. Start with a disposable document that
contains no private data and leave every deletion option set to `false`. Follow the
[smoke test](docs/SMOKE_TEST.md) before using real material.

## Requirements

- Windows 11 with WSL2 Ubuntu
- Docker Desktop, preferably with WSL integration enabled for Ubuntu
- Git for Windows
- Python 3.11 or newer, with the project environment managed by `uv`
- [uv](https://docs.astral.sh/uv/)
- Go 1.26 or newer, used only to build the official WeKnora CLI
- [Ollama](https://ollama.com/download)
- the `7z` command from 7-Zip, needed only for archive input
- your own MinerU API key
- your own MiMo API key when figure descriptions or model-assisted classification are used

The documented setup uses Docker Desktop, WSL2 Ubuntu, and Ollama on Windows. Native Docker
inside WSL can work, but you must configure container access to Ollama yourself. Do not
assume that `host.docker.internal:11434` is available in that setup.

## Shortest installation path

`scripts/doctor.ps1` checks whether an existing clone is ready for retrieval or ingestion.
For a first installation, start with the bootstrap script:

```powershell
git clone https://github.com/xydadada/question-bank-template.git
cd question-bank-template
powershell -ExecutionPolicy Bypass -File .\scripts\bootstrap.ps1 -StartWeKnora
```

The bootstrap script creates ignored directories, the Python environment,
`.runtime/WeKnora`, and `bin/weknora.exe` inside the repository. If a missing system
component needs administrator access, the script stops and prints the official installation
link instead of installing it silently. WeKnora is pinned to a reviewed release commit.

After bootstrap:

1. Open <http://127.0.0.1:8088> and create or sign in to a local WeKnora account.
2. Run `powershell -File .\scripts\configure-weknora.ps1` and sign in through the official
   CLI.
3. Copy `.env.example` to `.env` and add your MinerU and MiMo keys.
4. Put a few disposable files in `inbox`.
5. Start processing.

```powershell
powershell -File .\scripts\start.ps1 -Processing
```

Check status or stop the stack:

```powershell
powershell -File .\scripts\status.ps1
powershell -File .\scripts\stop.ps1
powershell -File .\scripts\stop.ps1 -StopWeKnora
```

Run a direct three-layer search:

```powershell
uv run python ingest.py --search "your search terms"
```

Local OCR is optional. Install the extra dependencies only when you intend to enable it,
then set `ollama.ocr_enabled` to `true` in `config.local.yaml`.

```powershell
uv sync --extra ocr
uv run python -c "import rapidocr, onnxruntime; print('OCR extra ready')"
```

## Local directories

| Directory | Contents | Tracked by Git |
|---|---|---|
| `inbox/` | source files waiting for processing | no |
| `archives/` | extracted source archives, retained by default | no |
| `work/` | downloads, split files, screenshots, and temporary data | no |
| `markdown/` | final classified Markdown | no |
| `failed/` | failed source files that must be retained | no |
| `outputs/` | local reports and manual-deletion manifests | no |
| `.runtime/` | WeKnora source, logs, and PID files | no |
| `profiles/` | publishable classification templates | yes |

Source deletion is not a default behavior. When enabled, it applies only after the three
index layers pass their upload and retrieval checks. Failed files remain on disk. See
[configuration](docs/CONFIGURATION.md) for the switches and
[known limitations](docs/KNOWN_LIMITATIONS.md) for cases where the program stops instead of
guessing.

## Connect ChatGPT, optional

```powershell
powershell -File .\scripts\bootstrap.ps1 -InstallMcpTools
powershell -File .\mcp-public\configure-readonly-profile.ps1
powershell -File .\mcp-public\set-password.ps1
powershell -File .\mcp-public\setup-cloudflare.ps1 `
  -Hostname mcp.your-domain.example -CreateDnsRoute
powershell -File .\mcp-public\start-all.ps1 `
  -ExternalUrl https://mcp.your-domain.example
```

Add `https://mcp.your-domain.example/mcp` to the ChatGPT Workspace that will use it and
select OAuth authentication. The official WeKnora MCP exposes ten tools. `chat` and
`session_ask` create conversation records, so a Workspace administrator should disable
them for strict retrieval-only use. The remaining eight tools read or retrieve content.
See the [ChatGPT MCP guide](docs/CHATGPT_MCP.md) for setup and verification.

## Classification profile

`profiles/physics-question-bank.yaml` is a physics example. Its institution aliases are
blank on purpose. Fill them with names from your own collection. To use another subject,
copy the profile, edit its document types and module vocabulary, and set
`document_classification.taxonomy_file` in `config.local.yaml`.

## Components and project boundary

The template uses these existing projects without maintaining private forks:

- [MinerU](https://github.com/opendatalab/MinerU) parses documents.
- [WeKnora](https://github.com/Tencent/WeKnora) stores the knowledge bases, builds indexes,
  and provides the official MCP.
- [Ollama](https://ollama.com/) runs the local Embedding model and optional OCR model.
- MiMo describes important figures and classifies documents that the rules cannot settle.

This public repository is not a copy of the maintainer's private runtime directory. Each
user creates their own three knowledge bases and CLI profiles. The repository contains no
prepared question bank, live knowledge base ID, Cloudflare Tunnel, user account, domain,
log, runtime database, or privately built WeKnora CLI. Third-party sources and pinned
versions are listed in [third-party notices](THIRD_PARTY_NOTICES.md).

## Contributing and license

Run these checks before submitting a change:

```powershell
powershell -File .\scripts\release-audit.ps1
uv run python -m unittest discover -s tests -v
```

GitHub Actions repeats them for pull requests and pushes to `main`. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the repository rules. The project uses the
[MIT License](LICENSE); third-party components retain their own licenses.
