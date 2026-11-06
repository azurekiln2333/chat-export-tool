# Backend Development Guidelines

> Best practices for backend development in this project.

---

## Overview

This directory contains guidelines for backend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Export Contract](./export-contract.md) | CLI/API, raw data, media and validation contracts | Implemented |
| [Unified Import](./unified-import.md) | Three-app detection, source protection, incremental stages and retained history | Implemented |
| [Database Guidelines](./database-guidelines.md) | Snapshots, SQLCipher, handles and owned indexes | Implemented |
| [Node SQLite Reader](./web-reader.md) | Vue service, exact imports, reversible edits, portable media and HTTP | Implemented |
| [Error Handling](./error-handling.md) | Error types, handling strategies | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Python baseline, checks and forbidden patterns | Implemented |
| [Logging Guidelines](./logging-guidelines.md) | Structured logging, log levels | To fill |

---

## Quality Check

Read the export contract and quality guidelines before reviewing pipeline
changes. Run `python -m compileall -q src tests` and
`python -m unittest discover -s tests -v`. Validate data and local links; use
full decoding for media changes when FFmpeg/Pillow are available.
For `web/` changes, read the Node SQLite reader contract and run
`npm run build` followed by `npm test` from that directory.

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
