# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- Two-distribution layout: `filekeeper` (Core + CLI, no third-party runtime
  dependencies) and the reserved, unimplemented `filekeeper-mcp` distribution.
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
- Published stub module for exact-text and line-range editing that raises rather
  than silently doing nothing.

### Fixed

- Boundary refusals now carry distinct, stable error codes and exit codes:
  `..` traversal keeps `PATH_OUTSIDE_WORKSPACE` (4), an absolute path outside
  the workspace is `ABSOLUTE_PATH_OUTSIDE_WORKSPACE` (6), and a symlink whose
  target escapes is `SYMLINK_ESCAPE_REFUSED` (11).
- A write into an unwritable directory is now reported as `STORAGE_ERROR`
  (exit 9) instead of `INTERNAL_ERROR` (exit 1); the message names only the
  caller's path, never the resolved host path or the temporary name.
- The non-empty-directory refusal now points at the real CLI flags
  (`--recursive --confirm-recursive`) instead of the Core keyword.
- The recursive-delete warning states plainly that the deletion is recoverable
  through history but never atomic across the tree.
- `keeper workspace create` now describes the state directory as it actually
  behaves: created on the first mutation, with a `.gitignore` entry added when
  the workspace is a git repository.

### Notes

- Redo, snapshots, locks, patch application, batch execution and the MCP server
  are not part of this release.
- Multi-file operations are recoverable through history, and are never atomic
  across files; single-file writes are atomic.
