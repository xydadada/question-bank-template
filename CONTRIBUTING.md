# Contributing

Keep pull requests focused. Do not attach real source documents, generated
knowledge bases, API responses, credentials, database files, or logs.

Before opening a pull request:

```powershell
powershell -File .\scripts\release-audit.ps1
uv run python -m unittest discover -s tests -v
```

Changes that enable destructive behavior by default, add telemetry, expose a
write-capable MCP surface, or bypass upstream authentication will not be
accepted.

The checked-in `.github/workflows/audit.yml` runs the same checks for every
push and pull request. Action references must remain pinned to full immutable
commit SHAs; Dependabot can propose reviewed updates.
