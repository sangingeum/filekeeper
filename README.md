# Filekeeper

Safe, predictable filesystem operations for humans, shell scripts and AI agents.
One Core library, a `keeper` command line, and (later) an MCP server that all
behave identically because they call the same code.

Filekeeper confines every operation to a registered **workspace**, replaces whole
files atomically, refuses credential-shaped write targets, and reports structured
results with stable error codes.

## Status

Version 0.1 is the **foundation and basic operations** release. It includes
workspace registration and confinement, read/info/hash/list/search, file and
directory create/copy/move/delete, atomic whole-file replacement, text detection
and size limits, the CLI, and the SQLite schema that the history layer will use.

Not in this release, and deliberately so:

- operation history recording, undo, redo, snapshots
- exact-text and line-range editing (`replace_text`, `replace_lines`,
  `insert_lines`, `delete_lines`, `apply_patch`) — the API is a published stub that
  raises rather than doing nothing
- unified-diff application
- advisory locks with TTL (the tables exist; the API does not)
- the MCP server (`filekeeper-mcp` is a reserved, separate distribution)
- file watching, archives, HTTP transport, pruning UI

## Install

```bash
uv tool install filekeeper      # or: pip install filekeeper
keeper --help
```

Requires Python 3.11 to 3.13. The published `filekeeper` package has **no
third-party dependencies**.

## Quick start

```bash
keeper init                                  # create the home directory and database
keeper workspace create notes ~/notes        # register a workspace
keeper workspace use notes                   # make it the default
keeper file create todo.md --content "# Todo\n"
keeper file read todo.md
keeper file write todo.md --content "# Todo\n- write tests\n"
keeper file list . --recursive --glob '*.md'
```

Every command accepts `--json` for machine-readable output and `--workspace NAME`
or `--root PATH` to select a workspace. Selection precedence is explicit
`--workspace`, then explicit `--root`, then the configured default; Filekeeper
never guesses when several workspaces exist.

## Where state lives

**Project-local by default.** A workspace's own state — the operation journal and
content-addressed blobs that arrive with the history release — lives in
`<workspace>/.filekeeper/`, created on the first mutation that needs it. It
travels with the checkout, so a fresh clone does not silently lose undo history,
and deleting a project takes its blobs with it.

Workspace *registrations* live in the Filekeeper home directory
(`~/.local/share/filekeeper` on Linux, or `FILEKEEPER_HOME`), because a registry
of many roots cannot live inside one of them.

Set `state_location = "global"` in `config.toml` to keep one shared store under the
home directory instead. The trade-off: history survives deleting the checkout, but
per-project attribution and pruning become harder, and undo history no longer
travels with the tree.

If the workspace is a git repository, Filekeeper adds `.filekeeper/` to
`.gitignore` when it creates the state directory. Outside a repository it writes
nothing.

## Durability guarantees, in plain words

- **One file, replaced at a time, is atomic.** A whole-file write goes to a
  temporary file in the same directory, is flushed and fsynced, then renamed over
  the destination. If the process dies or the disk fills before the rename, the
  file still holds its complete previous content. Never a truncated file, never a
  half-written mix.
- **Multiple files are not atomic.** Copy, move and recursive directory deletion
  affect more than one path as a sequence of individually safe steps. A crash
  between steps leaves some paths changed and others not. This release has no undo,
  so multi-path operations are *not* recoverable either; the history release will
  make them recoverable, but still not atomic. Do not treat a batch of changes as
  all-or-nothing.
- **Deletion is final in this release.** Recursive directory deletion therefore
  requires both `--recursive` and `--confirm-recursive`.

## Line numbers

Line numbers are **1-based and inclusive** everywhere: Core, CLI, JSON output, and
tests. `--from-line 10 --to-line 20` returns lines 10 through 20, both included.
Ranges beyond the end of the file are rejected rather than silently clamped.

## Commands

```
keeper init
keeper workspace create NAME ROOT
keeper workspace list | show NAME | use NAME | remove NAME
keeper file read PATH [--from-line N] [--to-line M] [--max-bytes N] [--encoding E]
keeper file read-many PATH... [--from-line N] [--to-line M] [--max-bytes-per-file N]
keeper file info PATH
keeper file hash PATH
keeper file exists PATH
keeper file list [PATH] [--recursive] [--glob PATTERN] [--max-entries N]
keeper file search PATH GLOB [--max-results N]          # find paths
keeper file grep PATH QUERY [--regex] [--glob PATTERN] [--ignore-case]
keeper file create PATH (--content TEXT | --input FILE | stdin) [--allow-existing] [--dry-run]
keeper file write PATH (--content TEXT | --input FILE | stdin) [--expected-hash HASH] [--dry-run]
keeper file copy SOURCE DESTINATION [--expected-destination-hash HASH] [--dry-run]
keeper file move SOURCE DESTINATION [--overwrite] [--dry-run]
keeper file delete PATH [--expected-hash HASH] [--dry-run]
keeper dir create PATH [--parents] [--dry-run]
keeper dir move SOURCE DESTINATION [--dry-run]
keeper dir delete PATH [--recursive --confirm-recursive] [--dry-run]
```

Global options: `--workspace NAME`, `--root PATH`, `--json`, `--quiet`,
`--verbose`, `--home PATH`.

`keeper file read` writes file content to standard output unchanged (line endings
included). Metadata goes to stderr, or into the JSON object with `--json`.
`--input FILE` reads replacement content from a file, avoiding shell argument
limits; with neither flag, content is read from standard input when it is not a
terminal.

## JSON output

Every command returns one object. Success carries `"ok": true` and the result
fields:

```json
{
  "ok": true,
  "workspace": "notes",
  "path": "todo.md",
  "changed": true,
  "before_hash": "sha256:...",
  "after_hash": "sha256:...",
  "summary": "Wrote todo.md.",
  "operation_id": null,
  "undo_available": false,
  "dry_run": false,
  "warnings": [],
  "items": [{"path": "todo.md", "change": "modified", "before_hash": "sha256:...", "after_hash": "sha256:..."}]
}
```

Failure carries a structured error and no traceback:

```json
{
  "ok": false,
  "error": {
    "code": "CONTENT_CONFLICT",
    "message": "file content changed since it was read; refusing to overwrite",
    "retryable": true,
    "details": {"path": "todo.md"}
  }
}
```

`operation_id` is `null` and `undo_available` is `false` in this release: the field
shape is already the one the history layer will fill. A mutation is crash-safe on
disk, but it is not yet undoable.

## Exit codes

| Code | Meaning |
| ---- | ------- |
| 0 | success |
| 1 | unexpected error |
| 2 | invalid arguments or validation error |
| 3 | path or workspace not found |
| 4 | permission or workspace-boundary violation |
| 5 | concurrency conflict (stale expected hash) |
| 6 | absolute path resolves outside the workspace |
| 7 | patch could not be applied (reserved) |
| 8 | partial rollback or recovery (reserved) |
| 9 | storage or database error |
| 10 | write refused: the target looks like credential material |
| 11 | symlink target resolves outside the workspace |

`keeper file exists` exits 3 when the path does not exist, which makes it usable as
a shell predicate.

## Error codes

`INVALID_ARGUMENT`, `PATH_NOT_FOUND`, `NOT_A_FILE`, `NOT_A_DIRECTORY`,
`PATH_OUTSIDE_WORKSPACE`, `ABSOLUTE_PATH_OUTSIDE_WORKSPACE`,
`SYMLINK_ESCAPE_REFUSED`, `PATH_IS_STATE_PATH`, `SYMLINK_WRITE_REFUSED`,
`SPECIAL_FILE_REFUSED`, `SENSITIVE_PATH_REFUSED`, `ALREADY_EXISTS`,
`CONTENT_CONFLICT`, `BINARY_CONTENT_REFUSED`, `LIMIT_EXCEEDED`,
`INVALID_PATTERN`, `WORKSPACE_NOT_FOUND`, `WORKSPACE_EXISTS`,
`NO_WORKSPACE_SELECTED`, `STORAGE_ERROR`, `MIGRATION_ERROR`,
`NOT_IMPLEMENTED_YET`, `INTERNAL_ERROR`.

Codes are stable; messages are not a contract.

## Safety rules

- **Confinement.** Relative paths resolve inside the workspace root. Absolute paths
  are accepted only when they land inside it after canonical resolution. Paths
  containing `..` are rejected with `PATH_OUTSIDE_WORKSPACE` (exit 4). An
  absolute path that resolves outside is refused with
  `ABSOLUTE_PATH_OUTSIDE_WORKSPACE` (exit 6), and a symlink whose target leaves
  the workspace with `SYMLINK_ESCAPE_REFUSED` (exit 11). A symlink is never a
  write target (the link itself would be replaced, not its target). Device
  files, sockets and FIFOs are refused.
- **Credential-shaped targets are refused.** Writes to `.env`, `.env.*`, `*.pem`,
  `*.key`, `id_rsa` and its siblings, `*.p12`, `*.pfx`, `authorized_keys`,
  `.netrc`, and anything under `.ssh/`, `.gnupg/` or `.aws/` fail with
  `SENSITIVE_PATH_REFUSED` (exit code 10). Override deliberately with
  `--i-accept-secret-write-risk` (Core: `allow_sensitive_write=True`). The flag is
  long because the mistake is expensive.
- **Version-control-ignored targets warn.** Writing a path that git ignores still
  succeeds, but the result carries a warning that the change will not be committed.
- **No shell.** Filekeeper never interpolates paths into a shell command.
- **Own state is off limits.** Paths inside `.filekeeper/` are refused, so the tool
  can never record its own state files in its own history.

## Text, binary and size limits

A file is treated as binary when its first 8 KiB contains a NUL byte. Text
operations refuse binary content and undecodable bytes instead of returning
mojibake; `--encoding` names an encoding explicitly. A UTF-8 byte-order mark is
preserved across rewrites, and line endings are **never** normalised — the detected
convention is reported, not applied.

Reads and writes are bounded (5 MiB and 20 MiB by default), as are search results,
directory entries and recursion depth. Bounded reads report `truncated`,
`truncation_reason` and `next_start_line` instead of quietly returning a prefix.
Adjust the limits in `config.toml` under `[filekeeper]` in the home directory.

## Python API

```python
from filekeeper import Filekeeper

app = Filekeeper()  # or Filekeeper(home="/path/to/home")
app.init()
workspace = app.create_workspace("notes", "/home/me/notes")
service = app.workspace(name="notes")  # or root="/path" for a transient workspace

read = service.read_file("todo.md", start_line=1, end_line=20)
service.write_file("todo.md", "# Todo\n", expected_hash=read.content_hash)
```

Mutating methods return `MutationResult` with `operation_id`, `before_hash`,
`after_hash`, `undo_available`, `summary`, `warnings` and per-path `items`.

Core imports only the Python standard library. `packages/filekeeper-mcp/` is a
separate, currently unimplemented distribution so that installing the CLI never
pulls in the MCP SDK.

## Development

```bash
uv sync
uv run pytest
uv run ruff check .
uv run mypy
```

`docs/decisions.md` records the design choices this specification left open.

## License

MIT. See `LICENSE`.
