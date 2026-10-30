# Filekeeper

Safe, predictable filesystem operations for humans, shell scripts and AI agents.
One Core library, a `keeper` command line, and an MCP server that all behave
identically because they call the same code.

Filekeeper confines every operation to a registered **workspace**, replaces whole
files atomically, refuses credential-shaped write targets, and reports structured
results with stable error codes.

## Status

Version 0.1 is the **foundation, operations and reversibility** release. It
includes workspace registration and confinement, read/info/hash/list/search,
file and directory create/copy/move/delete, atomic whole-file replacement, text
detection and size limits, durable operation history with content-addressed
blobs, safe undo with conflict detection, staged literal text editing,
line-range editing, unified-diff application, the CLI, and the MCP server.

Not in this release, and deliberately so:

- redo (undo is the requirement; redo is convenience) — an undone operation is
  recorded, but `redo` itself is not implemented
- explicit snapshots and restore (history blobs already hold the data)
- advisory locks with TTL (the tables exist; the API does not)
- batch execution, file watching, archives, history pruning UI, HTTP transport

## Install

```bash
uv tool install filekeeper      # or: pip install filekeeper
keeper --help
```

Requires Python 3.11 to 3.13. The published `filekeeper` package has **no
third-party dependencies**. The MCP server is a separate distribution, so
installing the CLI never pulls in the MCP SDK:

```bash
uv tool install filekeeper-mcp   # Core + CLI + MCP server
filekeeper-mcp --root ~/project
```

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

## MCP server

`filekeeper-mcp` speaks the Model Context Protocol over STDIO. It is a thin
adapter over Core: no filesystem behaviour lives in it, so a guarded write or a
refusal is identical whichever adapter issued it.

```bash
filekeeper-mcp --workspace notes
filekeeper-mcp --root ~/work/tree --home ~/.local/share/filekeeper
```

`--workspace` and `--root` are mutually exclusive; `--home` overrides
`FILEKEEPER_HOME`; `--log-level` controls stderr only, because stdout carries
protocol messages. `--actor` and `--task-id` are optional audit metadata recorded
on every mutation. There is no network listener in this release.

The allowed roots come from start-up configuration only. No tool argument can
widen them, register a workspace or point the server at another tree.

### Tools

Read-only: `workspace_list`, `workspace_info`, `file_read`, `file_read_many`,
`directory_list`, `file_info`, `file_search`, `content_search`, `history_list`,
`history_get`, `history_diff`.

Mutating: `file_create`, `file_write`, `file_copy`, `file_move`, `file_delete`,
`directory_create`, `directory_delete`, `file_replace_text`,
`file_replace_lines`, `file_insert_lines`, `file_delete_lines`,
`file_apply_patch`, `undo_operation`, `undo_last`.

The same conventions apply as in the CLI: workspace-relative paths, 1-based
inclusive line numbers, `expected_hash` / `expected_content` guards, `dry_run` on
every mutation, and `operation_id` / `undo_available` on every mutation. Each
tool's description states its own semantics, and each carries protocol
annotations (`readOnlyHint`, `destructiveHint`, `idempotentHint`,
`openWorldHint`) that describe what it actually does.

Requests that could exhaust memory are refused with `LIMIT_EXCEEDED`, not
truncated: 32 paths per `file_read_many`, a 1 MiB patch, and at most 200 search
results, 1000 directory entries or 200 history entries per request. Errors are
structured data carrying the stable Core code, `retryable`, the caller's path and
the workspace name, with no stack trace and no host path.

A tool that mutates returns the Core `operation_id` and `undo_available` exactly
as the CLI does, so `history_list`, `history_diff` and `undo_operation` work the
same way from an agent host. The optional audit field is `task_id`; it is
supplied by server configuration rather than trusted from a tool argument.

## Where state lives

**Project-local by default.** A workspace's own state — the operation journal and
content-addressed blobs — lives in `<workspace>/.filekeeper/`, created on the
first mutation that needs it. It travels with the checkout, so a fresh clone does
not silently lose undo history, and deleting a project takes its blobs with it.

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
- **Multiple files are recoverable, not atomic.** Copy, move and recursive
  directory deletion affect more than one path as a sequence of individually safe
  steps. A crash between steps leaves some paths changed and others not. History
  makes the whole operation recoverable: a crash leaves a *pending* record, the
  next run resolves it honestly, and `keeper history undo` can put the previous
  bytes back. It is still not all-or-nothing, and you should not treat a batch of
  changes as a transaction.
- **The order is fixed.** Before anything is touched, the previous and new
  contents are written and fsynced as immutable blobs, then a pending operation
  record is committed with SQLite in full-synchronous mode. Only then is the file
  replaced, verified, and the operation marked committed.
- **History has a size cap.** A file above `max_history_blob_bytes` (4 MiB by
  default) is still recorded, but its bytes are not stored: the result says
  history was skipped for it and `undo_available` is false. One large edit cannot
  silently fill the store.

## Line numbers

Line numbers are **1-based and inclusive** everywhere: Core, CLI, JSON output, and
tests. `--from-line 10 --to-line 20` returns lines 10 through 20, both included.
Ranges beyond the end of the file are rejected rather than silently clamped.

## Editing

Every edit goes through the same journal sequence as a whole-file write: blobs,
pending record, atomic replacement, commit. Each application therefore returns a
real `operation_id` and is undoable with `keeper history undo`.

### Literal text replacement

`keeper file replace-text PATH --old TEXT --new TEXT (--first | --all)`

This is the primary edit. Matching is staged, and the result always names the
stage that matched:

1. `exact` — the literal text appears exactly as given;
2. `normalised` — indentation and trailing whitespace are tolerated, and the
   replacement takes the indentation of the line it replaces. A stage-2 match is
   never applied silently;
3. `none` — no match: the command fails with `NO_MATCH` and the nearest line
   numbers and their text.

The policy is explicit: `--first` or `--all`. `match_count` reports how many
matches were found. `--expected-match-count N` refuses a different count, and
`--expected-content TEXT` guards the exact text of a single target region.
`--expected-hash` guards the whole file. `--dry-run` prints a unified diff and
writes nothing. Replacement is always literal: the search and replacement text
are never interpreted as a regular expression or a regex template.

### Line ranges

```
keeper file replace-lines PATH --start N --end M --replacement TEXT
keeper file insert-lines PATH --position before|after|end [--line N] --content TEXT
keeper file delete-lines PATH --start N --end M
```

Line numbers are 1-based and inclusive. Out-of-range references are rejected,
never clamped. End-of-file insertion is the explicit `--position end`; it never
overloads a line number. The file's newline convention and byte-order mark are
preserved, and the same hash/content guards and dry-run apply.

### Unified diffs

`keeper file apply-patch [PATCH]` reads a patch file, or patch text from standard
input. The whole patch is parsed and every hunk is checked against its file
before anything is written. A failed or ambiguous hunk rejects the patch; no
partial patch is applied silently. `--dry-run` validates and reports every file
and hunk without touching the tree. The result carries per-file and per-hunk
status, and `--json` carries the same data.

A multi-file patch is recorded as **one logical operation**, and undo restores
every file. Application itself is **recoverable, not atomic across files**: if a
later file fails after earlier ones were written, the written files are rolled
back to their pre-patch bytes and the failure names what was restored. A crash is
resolved from the pending journal record, never from an all-or-nothing commit.

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
keeper file replace-text PATH --old TEXT --new TEXT (--first | --all) [guards] [--dry-run]
keeper file replace-lines PATH --start N --end M --replacement TEXT [guards] [--dry-run]
keeper file insert-lines PATH --position before|after|end [--line N] --content TEXT [guards] [--dry-run]
keeper file delete-lines PATH --start N --end M [guards] [--dry-run]
keeper file apply-patch [PATCH] [--dry-run]
keeper dir create PATH [--parents] [--dry-run]
keeper dir move SOURCE DESTINATION [--dry-run]
keeper dir delete PATH [--recursive --confirm-recursive] [--dry-run]
keeper history list [--path PATH] [--actor NAME] [--task-id ID] [--limit N]
keeper history show OPERATION_ID
keeper history diff OPERATION_ID
keeper history undo OPERATION_ID
keeper history undo-last [--actor NAME]
```

Global options: `--workspace NAME`, `--root PATH`, `--json`, `--quiet`,
`--verbose`, `--home PATH`, `--actor NAME`, `--task-id ID`.

`--actor` and `--task-id` are recorded with a mutation and act as filters for
`history list`; they are optional and carry no host-specific meaning.

## History and undo

Every mutation is recorded as an operation with its affected paths, before and
after content hashes, file modes, existence on both sides, a timestamp, and the
optional `actor` / `task_id`. Blob content is content-addressed, so identical
content is stored once.

```bash
keeper history list                    # newest first, this workspace only
keeper history show op_1a2b3c4d        # operation, status and per-path hashes
keeper history diff op_1a2b3c4d        # unified diff, never the whole file body
keeper history undo op_1a2b3c4d        # restore the state before that operation
keeper history undo-last               # the most recent undoable operation here
```

Undo is safe, not best-effort. Before changing anything it compares every
affected path with the state the operation recorded:

- if the path still matches the recorded **after** state, undo restores the
  recorded **before** bytes;
- if it already matches the **before** state (a partially applied multi-file
  operation), undo leaves it alone;
- if it matches neither, someone changed it afterwards: the undo is refused with
  `UNDO_CONFLICT` (exit code 12) and **nothing is modified**.

Undoing a create removes the file only while it still matches what was created.
Undoing a delete restores the file only while the destination is still absent.
Undoing a move checks both ends. A directory operation validates every path in
the tree first. `undo-last` only ever looks at the selected workspace.

The undo is itself recorded as an operation, so `history list` remains a
truthful ledger. **Redo is deferred** — it is not half-implemented; undoing an
undo is the supported way to go forward again.

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
  "operation_id": "op_1a2b3c4d5e6f7a8b",
  "undo_available": true,
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

`operation_id` is `null` only for a dry run (nothing was recorded).
`undo_available` is `false` when an affected file was above the history size cap,
so its previous bytes were deliberately not stored; `history_skipped` then names
the paths.

Editing results add `match_stage` and `match_count` (text replacement),
`preview_diff` (dry run), and, for patches, `patch_files` with per-hunk status.
A failure that was rolled back reports the restored paths in the error details.

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
| 7 | patch could not be applied; the pre-patch state is intact |
| 8 | a failed patch could not be fully rolled back |
| 9 | storage or database error |
| 10 | write refused: the target looks like credential material |
| 11 | symlink target resolves outside the workspace |
| 12 | undo refused: a path changed after the operation |
| 13 | history needed for restore is missing, corrupt or was skipped |

`keeper file exists` exits 3 when the path does not exist, which makes it usable as
a shell predicate.

## Error codes

`INVALID_ARGUMENT`, `PATH_NOT_FOUND`, `NOT_A_FILE`, `NOT_A_DIRECTORY`,
`PATH_OUTSIDE_WORKSPACE`, `ABSOLUTE_PATH_OUTSIDE_WORKSPACE`,
`SYMLINK_ESCAPE_REFUSED`, `PATH_IS_STATE_PATH`, `SYMLINK_WRITE_REFUSED`,
`SPECIAL_FILE_REFUSED`, `SENSITIVE_PATH_REFUSED`, `ALREADY_EXISTS`,
`CONTENT_CONFLICT`, `NO_MATCH`, `MATCH_COUNT_MISMATCH`, `PATCH_FAILED`,
`PARTIAL_RECOVERY`, `BINARY_CONTENT_REFUSED`, `LIMIT_EXCEEDED`,
`INVALID_PATTERN`, `WORKSPACE_NOT_FOUND`, `WORKSPACE_EXISTS`,
`NO_WORKSPACE_SELECTED`, `STORAGE_ERROR`, `MIGRATION_ERROR`,
`OPERATION_NOT_FOUND`, `UNDO_CONFLICT`, `HISTORY_UNAVAILABLE`,
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
convention is reported, not applied. Line-range edits and patch hunks write
caller content using the file's existing convention, so editing a CRLF file does
not silently convert it to LF.

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
Editing methods add `match_stage`, `match_count`, `preview_diff` and
`patch_files`.

The same history is available from Python:

```python
record = service.get_operation(operation_id)
recent = service.list_history(path="todo.md", actor="editor", limit=10)
diff = service.diff_operation(operation_id)
service.undo(operation_id, actor="editor")
service.undo_last(actor="editor")
```

Core imports only the Python standard library. `packages/filekeeper-mcp/` is a
separate distribution so that installing the CLI never pulls in the MCP SDK; it
ships the STDIO server described above.

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
