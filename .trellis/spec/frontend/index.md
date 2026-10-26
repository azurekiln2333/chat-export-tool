# Frontend Development Guidelines

> Best practices for frontend development in this project.

---

## Overview

This directory contains guidelines for frontend development. Fill in each file with your project's specific conventions.

---

## Guidelines Index

| Guide | Description | Status |
|-------|-------------|--------|
| [Directory Structure](./directory-structure.md) | Module organization and file layout | To fill |
| [Component Guidelines](./component-guidelines.md) | Component patterns, props, composition | To fill |
| [Hook Guidelines](./hook-guidelines.md) | Custom hooks, data fetching patterns | To fill |
| [State Management](./state-management.md) | Local state, global state, server state | To fill |
| [Quality Guidelines](./quality-guidelines.md) | Chinese reader, English paths, preserved content and links | Implemented |
| [Type Safety](./type-safety.md) | Type patterns, validation | To fill |

---

## Quality Check

The offline frontend is generated in `exporter.py` and `reader_media.py`;
the optional database editor is Vue source in `web/src/`. Read
[Reader Quality Guidelines](./quality-guidelines.md) and the shared
[Export Contract](../backend/export-contract.md). Verify Chinese UI, English
paths and original content independently, then run the full Python suite and
export validation. For Vue changes run the production build and Node tests
in `web/`; see [Node SQLite Reader](../backend/web-reader.md).

## How to Fill These Guidelines

For each guideline file:

1. Document your project's **actual conventions** (not ideals)
2. Include **code examples** from your codebase
3. List **forbidden patterns** and why
4. Add **common mistakes** your team has made

The goal is to help AI assistants and new team members understand how YOUR project works.

---

**Language**: All documentation should be written in **English**.
