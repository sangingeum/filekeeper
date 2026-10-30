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
- Published stub module for exact-text and line-range editing that raises rather
  than silently doing nothing.

### Notes

- Operation history, undo, redo, snapshots, locks, patch application and the MCP
  server are not part of this release.
- Multi-file operations are recoverable only once history lands, and are never
  atomic across files; single-file writes are atomic now.
