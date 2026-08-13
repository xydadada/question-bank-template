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

The checked-in `.github/workflows/audit.yml` runs the same checks for pull
requests and pushes to `main`. Action references must remain pinned to full
immutable commit SHAs; Dependabot can propose reviewed updates.

Documentation should describe the current behavior. Commit messages and the
changelog carry the change history. Prefer direct sentences, concrete commands
and links to the component being discussed. Keep performance, compatibility and
security claims within the repository's verified scope. Preserve code blocks,
option names and safety warnings exactly when editing prose.

This privacy-safe template accepts only GitHub-provided noreply addresses in
reachable commit metadata. Before committing, select your GitHub noreply
address locally and enable **Keep my email addresses private** in GitHub email
settings. Do not rewrite another contributor's identity; ask them to amend an
unpublished commit themselves when needed.
