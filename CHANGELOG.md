# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## 0.1.3 — 2026-10-31

### Fixed

- A workspace whose root cannot host `.filekeeper` — most visibly `--root /` —
  can mutate again. 0.1.2 shipped a deployment that could not write at `--root /`:
  the state directory was created with an unguarded `mkdir`, so the first
  journaling mutation failed as `INTERNAL_ERROR` with an absolute host path in
  the message, while `dry_run` still reported success. State-directory
  construction is now a `STORAGE_ERROR` everywhere it happens, and the resolved
  location falls back to the global store — with a warning in the response —
  when the root cannot host a project-local directory and the operator left
  `state_location` at its default. An explicit `state_location` is honoured and
  fails loudly instead. The check runs before any filesystem change, so a
  mutation that cannot journal no longer reports failure after creating
  something, and a dry run verifies the journal it would need before saying yes.
- MCP error documents no longer lose every separator when the workspace root is
  `/`. Scrubbing treated `/` as a host path and replaced it everywhere; a bare
  separator is no longer a location, and a location is replaced only as a whole
  path token. `error.details.state_dir` is reported as the operator chose it,
  because it is the location they must fix.
- A transient workspace (`--root`) derives its id from the canonical root
  instead of drawing one per process, so with global state `history_list`,
  `history_diff` and `undo_operation` see work done by an earlier process, and a
  restart no longer fragments the journal. State directories left by 0.1.2's
  per-process ids are not adopted silently: their existence is reported in the
  response warning.
- A wrong argument name on an MCP tool returns `INVALID_ARGUMENT` naming the
  expected arguments, instead of the SDK's raw validation text with no code.
  The published schema marks unknown arguments as refused, matching what the
  server enforces.

### Changed

- `filekeeper-cli` 0.1.3 and `filekeeper-mcp` 0.1.3 are released together and
  carry the same version number.

## 0.1.2 — 2026-10-31

### Added

- A tool-annotation regression test for the MCP server. It spawns the server over
  STDIO, completes the `initialize` handshake and reads `tools/list`, then asserts
  for every tool that an `annotations` object is present, that all four protocol
  hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`)
  arrived as explicit JSON booleans, and that `readOnlyHint` and `destructiveHint`
  never contradict each other. The exact tool count is pinned at twenty-five, and
  each tool's values are pinned against its handler, so a tool added without an
  annotation — the shape directory reviewers reject — fails the suite.

### Changed

- `filekeeper-cli` 0.1.2 and `filekeeper-mcp` 0.1.2 are released together and now
  carry the same version number. Code, metadata and behaviour are unchanged from
  `filekeeper-cli` 0.1.1.
- `filekeeper-mcp` 0.1.2 is the server's first release under the shared version
  number. Its earlier publication was 0.1.0, which remains installable and is not
  withdrawn; 0.1.2 carries the same code with the version aligned to
  `filekeeper-cli` 0.1.2.

## 0.1.1 — 2026-10-31

### Changed

- Metadata-only release: no behaviour, API or dependency change. The
  `Requires-Python` requirement moves from `>=3.11,<3.14` to `>=3.12,<3.13`, and
  the sole Python classifier is 3.12. Version 0.1.0 remains installable on
  Python 3.11.
- The README no longer states a Python version. The package metadata is the single
  home for the interpreter requirement, so a prose copy cannot drift from it.

## Unreleased

### Added

- Two-distribution layout: `filekeeper-cli` (Core + CLI, no third-party runtime
  dependencies; the import package stays `filekeeper`) and `filekeeper-mcp` (the
  MCP server, depending on `filekeeper-cli>=0.1,<0.2` and the pinned MCP SDK).
- Workspace registration and selection, with precedence `--workspace`, `--root`,
  then the configured default. Removal never touches the workspace's files.
- Project-local per-workspace state at `<workspace>/.filekeeper/` (opt-in global
  mode via `state_location` in `config.toml`), with a `.gitignore` entry added for
  git repositories.
- Path resolution with workspace boundaries: canonical resolution, `..`
  rejection, symlink handling, symlink-write refusal, special-file refusal, and
  refusal of Filekeeper's own state paths.
- SQLite schema and forward-only migrations for the registry (`workspaces`,
  `settings`) and per-workspace store (`operations`, `operation_items`, `blobs`,
  `locks`).
- Content-addressed blob store foundation (`BlobStore`).
- Atomic single-file replacement: temporary file in the target directory, flush,
  `fsync`, permission preservation, atomic rename, parent-directory `fsync` where
  supported, and final hash verification.
- Read operations: bounded file reads with 1-based inclusive line ranges and
  truncation metadata, multi-file reads, recursive and glob directory listing,
  file info, SHA-256 hashing, existence checks, path search by glob, and content
  search by literal or regular expression with a result cap.
- Mutations: create, write (with optional `expected_hash`), copy, move, delete,
  create/move/delete directory, with recursive deletion requiring explicit
  confirmation.
- Text/binary detection, BOM preservation, encoding selection, newline reporting,
  and configurable size limits.
- Credential-shaped write refusal with its own error code and an explicit
  override, plus a warning for version-control-ignored targets.
- `keeper` CLI for every operation above, with `--json` and documented stable exit
  codes.
- `MutationResult` already carries `operation_id` and `undo_available` so the
  history layer can fill them without changing the public API shape.
- Durable operation history: every mutation records an operation and its
  affected items with relative paths, before/after blob hashes, existence and
  modes on both sides, timestamp, status, optional `actor` and `task_id`, and a
  summary. Mutations return a real `operation_id`.
- Content-addressed blobs are staged for every affected file, deduplicated by
  hash, verified on read, and capped by `max_history_blob_bytes`; oversize files
  are recorded with history skipped and reported in the result.
- Durability sequence: fsynced blobs, then a pending record on a
  full-synchronous SQLite connection, then the atomic replace, then hash
  verification, then the committed mark. Startup reconciliation resolves pending
  operations against the recorded before/after states without discarding blobs.
- Safe undo: `undo` and `undo_last` validate every affected path before changing
  anything, refuse later changes with `UNDO_CONFLICT` (exit 12), and record the
  undo itself as an operation. Missing or corrupt blobs raise
  `HISTORY_UNAVAILABLE` (exit 13).
- History surfaces: `get_operation`, `list_history` (path, actor, task-id and
  limit filters), `diff_operation`, and `keeper history list|show|diff|undo|
  undo-last`, all with `--json`.
- `replace_text`, the primary edit: literal-only replacement with an explicit
  `first` / `all` policy, a returned match count, an optional expected count,
  whole-file hash and target-region content guards, and a dry-run diff preview.
- Staged matching that names the stage in every result: `exact`, then
  whitespace-normalised (`normalised`, with the replacement taking the
  indentation of the line it replaces), then a `NO_MATCH` refusal with the
  nearest candidate line numbers and text.
- Line-range editing: `replace_lines`, `insert_lines` and `delete_lines`, with
  1-based inclusive ranges, out-of-range rejection, explicit end-of-file
  insertion, newline-convention and BOM preservation, and the same guards and
  dry run.
- `apply_patch` for unified diffs: syntax and preimage validation before any
  write, failed and ambiguous hunks rejected, per-file and per-hunk results,
  one logical operation for a multi-file patch, and honest rollback to the
  pre-patch state when a later file fails. Multi-file application is
  recoverable through history, never atomic across files.
- CLI commands `file replace-text`, `file replace-lines`, `file insert-lines`,
  `file delete-lines` and `file apply-patch`, each with `--dry-run` and `--json`,
  printing the `operation_id` and, for replacement, the matching stage.
- MCP server (`filekeeper-mcp`) over STDIO: a thin protocol adapter with no
  filesystem logic of its own. Start-up options `--workspace`, `--root`, `--home`,
  `--actor`, `--task-id` and `--log-level`; the allowed root is fixed at start-up
  and no tool argument can widen it.
- Twenty-five MCP tools covering workspace inspection, read/info/list/search,
  file and directory mutation, staged text and line-range editing, unified-diff
  application, history and undo. Descriptions state the 1-based inclusive line
  convention, the guard contract, dry-run support and undoability; every tool
  carries accurate `readOnlyHint`, `destructiveHint`, `idempotentHint` and
  `openWorldHint` annotations.
- Structured MCP errors carrying the stable Core code, `retryable`, the caller's
  path and the workspace name, with server-side host paths scrubbed and no stack
  traces returned to clients.
- Adapter-side request ceilings refused with `LIMIT_EXCEEDED` rather than
  truncated: 32 paths per `file_read_many`, a 1 MiB patch, and at most 200
  search results, 1000 directory entries or 200 history entries per request.
- The MCP SDK is pinned to `>=2.3,<3`, the major version this server is built and
  tested against; the pin and its reason are recorded in `docs/decisions.md`.
- Wheel-level dependency-isolation tests: installing `filekeeper` alone neither
  declares nor can import the MCP SDK, while installing `filekeeper-mcp` brings
  Core, the SDK and both console scripts.

### Changed

- `filekeeper-mcp` has its own release train. A `v*` tag publishes
  `filekeeper-cli` and an `mcp-v*` tag publishes `filekeeper-mcp`; one tag never
  uploads both distributions, so a version number is burned for exactly one
  project at a time. A manual workflow run only builds and inspects.
- Operation identity is `operation_id` on every surface. `history list` and
  `history show` items use the same key as mutations, diff and undo; there is no
  legacy `id` alias.
- One error envelope for the CLI and the MCP server: a failure document has
  exactly `ok` and `error`, `error.details.path` carries the caller's path, and
  no field is duplicated at the top level.
- `CONTENT_CONFLICT` reports `retryable: false` and states the recovery path in
  `error.details.recovery`: re-read the target, then write against its current
  hash.
- `history_skipped` is reported only when the bytes were genuinely not stored.
  An undo record no longer claims skipped history.
- The README exit-code row for code 5 now names all three conflict shapes: stale
  expected hash, no matching text, and a match count other than the expected one.
- `keeper file search` on an unreadable root now reports `STORAGE_ERROR` (exit 9)
  instead of returning an empty result. Path search and content search share one
  walker, so the same rule applies to both: an unreadable requested root is a
  failure, while unreadable nested entries are still skipped and the readable
  results are returned.

### Fixed

- A read of an unreadable target is now `STORAGE_ERROR` (exit 9) instead of
  `INTERNAL_ERROR` (exit 1), and its message names the caller's workspace-relative
  path only. The same handling covers file reads, multi-file reads, file info,
  hashing, directory listing and content search, matching the write path's
  existing mapping. A genuine internal fault still returns `INTERNAL_ERROR`.
- `keeper file read-many` now exits with the first failed entry's mapped exit code
  (for example 9 for an unreadable file) instead of always exiting 3.
- A content search whose requested root cannot be listed reports `STORAGE_ERROR`
  rather than returning an empty, misleadingly successful result. Unreadable
  nested entries are still skipped.
- Boundary refusals now carry distinct, stable error codes and exit codes:
  `..` traversal keeps `PATH_OUTSIDE_WORKSPACE` (4), an absolute path outside
  the workspace is `ABSOLUTE_PATH_OUTSIDE_WORKSPACE` (6), and a symlink whose
  target escapes is `SYMLINK_ESCAPE_REFUSED` (11).
- `apply-patch` failures now report the workspace-relative path of the file that
  could not be changed, and the `PARTIAL_RECOVERY` (exit 8) and `PATCH_FAILED`
  (exit 7) `error.details.cause` value never carries the resolved host path or a
  raw `[Errno ...]` repr. Errors raised by the guarded step keep their own
  caller-facing message.
- A write into an unwritable directory is now reported as `STORAGE_ERROR`
  (exit 9) instead of `INTERNAL_ERROR` (exit 1); the message names only the
  caller's path, never the resolved host path or the temporary name.
- Copy, move and undo over an unreadable file report `STORAGE_ERROR` (exit 9)
  with the caller's workspace-relative path in the message and in
  `error.details.path`. The operating system's message no longer carries the
  resolved host path, and hashing never falls back to it.
- The non-empty-directory refusal now points at the real CLI flags
  (`--recursive --confirm-recursive`) instead of the Core keyword.
- The recursive-delete warning states plainly that the deletion is recoverable
  through history but never atomic across the tree.
- `keeper workspace create` now describes the state directory as it actually
  behaves: created on the first mutation, with a `.gitignore` entry added when
  the workspace is a git repository.
- `keeper history diff` no longer concatenates two content lines when a file has
  no trailing newline. Every rendered line, including the last, ends with a
  newline, and a missing final newline is stated with the conventional
  `\ No newline at end of file` marker. The human and JSON views come from the
  same string.

### Notes

- Redo, snapshots, advisory locks, batch execution, file watching, HTTP
  transport and binary/media reads are not part of this release.
- Multi-file operations are recoverable through history, and are never atomic
  across files; single-file writes are atomic.
