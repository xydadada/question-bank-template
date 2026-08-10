# Security policy

## Secrets and private data

Never commit `.env`, `config.local.yaml`, API keys, OAuth data, knowledge-base
exports, source documents, generated Markdown, `state.db`, logs, or files under
`mcp-public/secrets/`. The repository ignores these locations by default.

If a secret was committed, revoke it at the provider first. Rewriting Git
history does not make a still-valid credential safe.

Use a GitHub-provided noreply address for both author and committer metadata.
The release audit rejects other reachable commit emails so a public merge does
not silently expose a personal address. Also enable **Keep my email addresses
private** and **Block command line pushes that expose my email** in your GitHub
account email settings.

## Destructive processing

All permanent-deletion options are disabled in the example configuration.
Enabling any of them also requires the exact local acknowledgement
`QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND`. Keep backups outside this
project until the workflow has been verified with disposable documents.

Explorer-style deletion propagation is a separate destructive capability. It
is disabled by default and requires
`QUESTION_BANK_ALLOW_MANUAL_DELETION_SYNC=I_UNDERSTAND`; enabling video or
source cleanup does not implicitly authorize it.

## Reporting

Report security issues privately through GitHub's security-advisory feature.
Do not include live credentials or private documents in an issue.
