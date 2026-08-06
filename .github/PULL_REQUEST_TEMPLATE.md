## Summary

Describe the focused change and why it is needed.

## Verification

- [ ] `uv run python -m unittest discover -s tests -v`
- [ ] `powershell -File .\scripts\release-audit.ps1`
- [ ] I did not include credentials, private documents, generated Markdown,
      local configuration, database files, OAuth data or unredacted logs.
- [ ] Destructive defaults remain disabled.
- [ ] GitHub Actions remain pinned to full commit SHAs.
- [ ] My reachable commit author and committer addresses use GitHub noreply.
