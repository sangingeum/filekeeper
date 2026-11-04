# filekeeper-mcp

The Model Context Protocol server for [Filekeeper](../..). It is a thin adapter:
every tool delegates to the `filekeeper` Core library, so an operation behaves
identically whether it arrives through the CLI or through MCP.

This is a **separate distribution**. `pip install filekeeper-cli` installs only
Core and the `keeper` CLI and never pulls the MCP SDK. `pip install
filekeeper-mcp` installs both.

## Install and run

```bash
uv tool install filekeeper-mcp     # or: pip install filekeeper-mcp
filekeeper-mcp --root ~/project
```

The transport is STDIO. There is no network listener in this release, and no
option that binds a socket.

Start-up options:

| Option | Meaning |
| ------ | ------- |
| `--workspace NAME` | serve a registered workspace by name or id |
| `--root PATH` | serve a directory as a transient workspace (nothing registered) |
| `--home PATH` | Filekeeper home directory; defaults to `FILEKEEPER_HOME` or the user data dir |
| `--actor NAME` | identity recorded on every mutation (optional audit metadata) |
| `--task-id ID` | optional external task identifier recorded on every mutation |
| `--log-level LEVEL` | `debug`, `info`, `warning` (default), `error`, `critical`; stderr only |

`--workspace` and `--root` are mutually exclusive. Selection precedence is the
same as the CLI: explicit name, explicit root, then the configured default, then
the single registered workspace. The server never guesses between several.

Allowed roots come from start-up configuration only. No tool argument can widen
the workspace, register a workspace, or point the server at another tree.

Example host configuration:

```json
{
  "mcpServers": {
    "filekeeper": {
      "command": "filekeeper-mcp",
      "args": ["--root", "/home/me/project", "--actor", "editor"]
    }
  }
}
```

## Tools

Read-only:

`workspace_list`, `workspace_info`, `file_read`, `file_read_many`,
`directory_list`, `file_info`, `file_search`, `content_search`, `history_list`,
`history_get`, `history_diff`.

`workspace_info` also returns the server's own package `version`, so a host can
record which build it measured without running `filekeeper-mcp --version`.

Mutating:

`file_create`, `file_write`, `file_copy`, `file_move`, `file_delete`,
`directory_create`, `directory_delete`, `file_replace_text`,
`file_replace_lines`, `file_insert_lines`, `file_delete_lines`,
`file_apply_patch`, `undo_operation`, `undo_last`.

Conventions that hold for every tool:

- Paths are workspace-relative. Absolute paths are accepted only when they
  resolve inside the selected workspace. With `--root /`, relative paths resolve
  from `/`; pass absolute paths or serve the repository root as the workspace.
- Line numbers are **1-based and inclusive**. Out-of-range references are
  refused, never clamped. End-of-file insertion is `position: "end"`.
- Every mutating tool returns the Core `operation_id` and `undo_available`.
  `operation_id` is `null` for a dry run.
- `expected_hash` (whole file, from `file_info` or `file_read`) and
  `expected_content` (the exact text of the edited region) are the guards. Pass
  at least one on content you did not just read.
- `dry_run` validates every guard and writes nothing. It is available on every
  mutating tool.
- `file_replace_text` requires an explicit `match` policy, `"first"` or `"all"`,
  and reports the matching stage (`exact` or `normalised`) in its result.
- `i_accept_secret_write_risk` is the only way past the credential-shaped write
  refusal. The override records a warning in the result. A
  `SENSITIVE_PATH_REFUSED` error names this argument, not Core's keyword or the
  CLI's flag.
- Destructive tools (`file_delete`, `directory_delete`, `file_apply_patch` and
  the no-undo-free content replacements) state their scope in their description
  and never operate outside the selected root. Recursive directory deletion
  requires both `recursive` and `confirm_recursive`.

Every tool carries MCP annotations (`readOnlyHint`, `destructiveHint`,
`idempotentHint`, `openWorldHint`) that describe what it actually does. A whole
file write is not read-only and not idempotent; a directory create is additive;
readers are read-only and idempotent.

## Limits

The adapter refuses oversized requests instead of truncating them silently:

| Bound | Value |
| ----- | ----- |
| paths in `file_read_many` | 32 |
| patch size | 1 MiB |
| requested search results | 200 |
| requested directory entries | 1000 |
| requested history entries | 200 |

Core's own configurable limits (`max_read_bytes`, `max_write_bytes`,
`max_search_results`, `max_directory_entries`, `max_recursion_depth`) still
apply and are described in the main README. A bounded result reports
`truncated` rather than pretending to be complete.

## Errors

Failures are structured data, never a stack trace:

```json
{
  "ok": false,
  "error": {
    "code": "CONTENT_CONFLICT",
    "message": "file content changed since it was read; refusing to overwrite",
    "retryable": false,
    "details": {
      "path": "notes.txt",
      "expected_hash": "sha256:...",
      "actual_hash": "sha256:...",
      "recovery": "re-read the target, then retry against its current hash"
    }
  }
}
```

The envelope is the same one the CLI emits: a failure document has exactly `ok`
and `error` at the top level, and `error` has exactly `code`, `message`,
`retryable` and `details`. The caller's own path is in `error.details.path`;
server-side host paths are not preserved. `retryable` describes the identical
request, so a stale hash is `false`: the caller must re-read the target and write
against its current hash.

## Not in this release

Advisory locks, user-facing snapshots and restore, batch execution, file
watching, HTTP transport, redo, and binary/media reads are deferred. They are not
half-implemented and not advertised as tools.

## Dependency pin

The server is built and tested against `mcp>=2.3,<3`. The SDK's server API
changed between 1.x and 2.x, so the major version is pinned; see
`docs/decisions.md` in the repository for the reasoning.

## License

MIT. See [LICENSE](LICENSE).
