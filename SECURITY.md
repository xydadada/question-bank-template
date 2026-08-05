# Security policy

## Secrets and private data

Never commit `.env`, `config.local.yaml`, API keys, OAuth data, knowledge-base
exports, source documents, generated Markdown, `state.db`, logs, or files under
`mcp-public/secrets/`. The repository ignores these locations by default.

If a secret was committed, revoke it at the provider first. Rewriting Git
history does not make a still-valid credential safe.

## Destructive processing

All permanent-deletion options are disabled in the example configuration.
Enabling any of them also requires the exact local acknowledgement
`QUESTION_BANK_ALLOW_PERMANENT_DELETE=I_UNDERSTAND`. Keep backups outside this
project until the workflow has been verified with disposable documents.

## Reporting

Report security issues privately through GitHub's security-advisory feature.
Do not include live credentials or private documents in an issue.
