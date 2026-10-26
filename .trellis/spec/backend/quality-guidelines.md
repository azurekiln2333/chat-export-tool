# Quality Guidelines

## Quality Check

Read [Export Contract](./export-contract.md), then run:

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -v
python -m duoshan_export doctor --json
```

The baseline is Python 3.10+. Do not use newer-only APIs such as
`hashlib.file_digest` without a fallback. No standalone linter or type checker
is configured; do not claim those checks ran. Decoder cases may skip only when
real optional dependencies are missing, never because another agent has not
delivered a module.

## Required Patterns

- Pass immutable `ExportConfig`; discover accounts and application version.
- Keep personal backups, messages, IDs, paths and media outside the repository.
  Generate fixtures in temporary directories; keep validation independent of
  the exporter where feasible.
- Hash full content before reusing snapshots or downloaded media.
- Check resolved ownership/containment before recursive deletion or copying.
- Preserve successful local resources during offline rerenders.
- Close SQLite/file handles explicitly; test same-process cleanup.
- Use atomic JSON state writes and durable per-download journals.
- Assert raw data equality and meaningful negative/corruption cases.

## Forbidden Patterns

- Fixed output/source paths, accounts, client versions, dates or shared runtime
  configuration inherited from the original analysis scripts.
- Editing source databases, auto-downloading without opt-in, accepting arbitrary
  redirect hosts, or reusing files based only on length.
- Changing original rows or removing attachments to make validation pass.
- Treating English filenames as a request to translate the reader UI.

For releases, build wheel/source distributions, inspect members for private
data/unrelated scaffolding, and smoke-test the wheel from another directory.
Record only observed verification results.
