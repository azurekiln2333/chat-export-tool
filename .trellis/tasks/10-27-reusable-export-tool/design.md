# Design

Use a Python package `src/duoshan_export` and an argparse CLI. Reuse the verified algorithms in `../_duoshan_work`, adapting their paths and state rather than depending on those scripts at runtime. Python 3.10+ is the baseline. All package imports must succeed without the SQLCipher extension; encrypted reads load it lazily. SQLCipher wheels, Zstandard, Pillow/HEIF and FFmpeg are detected with actionable installation guidance.

## Shared contracts (owned by coordinator)
`config.py`: frozen `ExportConfig` with `source: Path`, `output: Path`, `work: Path`, `accounts: tuple[str, ...] = ()`, `timezone: str = 'Asia/Shanghai'`, `page_size: int = 600`, `download: bool = False`, `workers: int = 6`, `sqlcipher_module_path: Path | None = None`, `ffmpeg: str = 'ffmpeg'`, `ffprobe: str = 'ffprobe'`, `resume: bool = False`. Property `tzinfo` returns the selected timezone. `errors.py`: `ExportError` base user-facing exception. `paths.py`: shared English filename/directory constants and stable ASCII filename helpers.

## Module boundaries
- `source.py`: `SourceLayout(source, app_dir, databases_dir, work_dir)` and `AccountSource(uid, database, contacts_database=None)` dataclasses; `prepare_source(source: Path, work: Path) -> SourceLayout`; `discover_accounts(layout) -> list[AccountSource]`.
- `database.py`: context manager `open_database(source, uid, work_dir, *, password=None, compatibility=3, sqlcipher_module_path=None)`; `decrypt_database(source, uid, destination, work_dir, *, sqlcipher_module_path=None) -> None`. Original files are never opened for writing; fresh private snapshots include matching WAL files.
- `exporter.py`: `export_chats(config: ExportConfig, layout: SourceLayout) -> dict`; it selects accounts from `config.accounts`, writes the standard output tree and uses `ReaderMedia(config.output)` to render final local media. No import-time export or fixed account IDs. Empty/missing contact caches are supported.
- `reader_media.py`: `ReaderMedia(output)` compatible with the current manifest schema and Chinese interface labels, with English paths and machine-readable keys. Its existing non-obscuring animated-bubble rendering is preserved.
- `media.py`: `download_media(config) -> dict`, reading exported JSONL and saving a resumable manifest/journal under `config.work`; `prepare_decorations(config, layout) -> dict`, allowing network only when `config.download` is true.
- `media_index.py`: `index_media(config) -> dict`; write only `export_` tables/views in exported databases, never mutate original tables.
- `validation.py`: `validate_export(output: Path, *, decode_media: bool = False, ffmpeg: str = 'ffmpeg') -> dict`; writes English validation reports and returns a status dictionary. CLI uses a nonzero exit code for failed validation.
- `cli.py`: inspect, export, media, validate and doctor commands; orchestrates source preparation, first export, optional downloads, decorations, final render, SQLite media indexing and validation. A media-only command rerenders from existing export if requested, without requiring the original backup when possible.

## State and output
The default work directory is `<output>/.work`. Reject unsafe source/output/work overlap and nonempty unrelated output. `--resume` is explicit and source-specific. Source identity must include content/file fingerprints, not just a copied filename; two backups sharing filenames cannot reuse stale database/WAL snapshots. Every invocation creates fresh runtime state and clears renderer counters between rendering passes. For media reuse, `--reuse-media PATH` copies or hardlinks verified resources into the new output while preserving integrity and never depending on the old path at reader runtime.

All HTML/CSS media references are local and constrained to the export tree; original web references are explicit links. Original text is HTML-escaped, CSV formula prefixes are neutralized, and raw JSONL/SQLite preserve exact values. Unknown messages keep their raw content and numeric type. Dates/time ranges are computed from data. Supported encrypted derivation is the locally verified Duoshan Android layout and must be documented as a compatibility boundary.

## User-facing language boundary
Directory/file names are English/ASCII. Default HTML reader controls, headings, navigation, generated message-type labels and explanations are Simplified Chinese (`lang="zh-CN"`). This corrects an earlier overbroad interpretation of the naming request. Do not translate source chats/contact names or rename raw database/JSON fields. Developer documentation and command flags may remain English; include a Chinese quick start for the user.

## Parallel ownership
pi p2 owns source.py/database.py; pi p4 owns exporter.py/reader_media.py; pi p5 owns media.py/media_index.py and decoration parsing helpers; pi p6 owns tests, validation.py and README/docs. Coordinator owns config/errors/paths, CLI, packaging, integration, source identity/reuse orchestration and final regression. Agents must not edit another owner's files without coordination.

## Vue database reader extension

Implement the accepted Vue 3 + Node.js + SQLite choice using `web-contract.md`.
`reader.sqlite` stores normalized messages and exact raw JSONL TEXT, plus
revisioned reversible edits in a separate table. The browser uses a bounded
HTTP API rather than directly opening the SQLite file. Only the Node runtime
is required for browsing/editing. Media remains in `attachments/`, with
relative references resolved by the server after a directory or domain move.
The app uses local Vue bundles, Chinese controls, string IDs, authenticated
media streaming and original bubble/inline emoji assets.

For this extension pi p2 owns the store, p4 the Vue UI, p5 media streaming,
and p6 independent fixtures/tests/documentation. Coordinator owns HTTP,
package/build wiring and final real-data verification. Default loopback and
token-protected reverse-proxy deployment share one build. MySQL is deferred
unless the workload requires substantial concurrent writers.
