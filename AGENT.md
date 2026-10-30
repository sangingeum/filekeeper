# Project brief

Static orientation for anyone working in this repository. No run results, no task
state.

## What this is

A local-first filesystem utility: a dependency-free Core library, a `keeper` CLI,
and an MCP server shipped as a separate distribution. Operations are confined to
registered workspaces and whole-file writes are atomic.

## Layout

```
src/filekeeper/
  core/                     domain logic, stdlib only
    errors.py               FilekeeperError + stable ErrorCode values
    models.py               frozen result dataclasses
    paths.py                workspace-bound path resolution
    workspace.py            registry, state directory, .gitignore handling
    config.py               home resolution, limits
    database.py             SQLite connections, two forward-only migration chains
    service.py              Filekeeper (home) and FilekeeperService (workspace)
    operations/             read, write, directory, search, edit
    safety/                 atomic write, hashing, text rules, refusals, VCS warning
    history/                blob store, operation journal, undo, read surfaces
  cli/                      argparse adapter: app, output, exit_codes
packages/filekeeper-mcp/    separate distribution: STDIO MCP server
  src/filekeeper_mcp/       server (start-up), tools (delegation), errors, payloads
tests/                      unit/, integration/, cli/, mcp/, helpers/
docs/decisions.md           design choices the spec left open
```

## Commands

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
uv run keeper --help
```

## Principles

- Dependency direction is `CLI -> Core <- MCP`. Core never imports either adapter
  and never imports anything outside the standard library; a test enforces both.
- The MCP adapter contains no filesystem logic. It validates protocol shape and
  delegates; path resolution, guards, limits and writes stay in Core.
- Every path goes through `core.paths.resolve_path`. Do not add a second way to
  decide which file an operation touches.
- Every whole-file mutation goes through `safety.atomic.atomic_write_bytes`.
- Line numbers are 1-based and inclusive in Core, CLI, docs and tests.
- Mutating methods return `MutationResult`, including `operation_id` and
  `undo_available`, so history can be added without changing the public shape.
- Multi-file effects are recoverable at best, never atomic; say so in user-facing
  text.
- The published repository contains no host-specific paths, no internal
  identifiers, and no plans. The optional audit field is `task_id`, and it stays
  generic.
