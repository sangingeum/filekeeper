# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

### Added

- `file_apply_patch` accepts `expected_hash` (CLI `--expected-hash`, MCP
  `expected_hash`), checked against the patch's first file section before any
  write. A stale hash is a `CONTENT_CONFLICT` refusal and the tree is untouched.
- `workspace_info` reports `root_writable` for the selected workspace, so a
  caller can tell a misconfigured root from a real permission failure before
  attempting a write.
- A refusal that involves path resolution names `resolved_path` and
  `workspace_root` in `details` — at minimum a write whose parent is missing, a
  permission failure on a mutation, and an absolute path outside the workspace.
  With a root of `/` a caller can see that `runs/x` resolved to `/runs/x`
  without deriving it.

### Changed

- `apply-patch` no longer relocates a hunk. A hunk whose preimage does not match
  at its declared line is refused with `reason: preimage_elsewhere`, naming the
  declared start and the lines where the context does occur. A hunk that
  matches only elsewhere used to be applied there and reported as success.

### Fixed

- `apply-patch` exits 7 with `PATCH_FAILED` and the position it would have used,
  instead of silently moving the edit and exiting 0.

## 0.1.8 — 2026-10-31

### Added

- `workspace_list` and `workspace_info` now report `root_exists` for every
  registration, so a workspace whose root is gone is visible instead of looking
  like an ordinary default. A listing that contains one also carries a warning
  naming that workspace. `keeper workspace remove` already removed a stale
  registration without touching files and is now covered by a test.
- `file_replace_text`'s `match` parameter is a two-value enum in the published
  MCP tool schema and is described as the replacement policy, so a caller sees
  `first` and `all` before the call instead of learning them from a refusal.

### Fixed

- Reported paths are unambiguous when the workspace root is `/`. Read and info
  results, read-many entries, search results, directory listings, copy/move
  paths, and history target and item paths all keep their leading `/`, so the
  reported string is exactly the absolute path. Every other root is unchanged,
  and history rows are rendered when read rather than migrated.

## 0.1.7 — 2026-10-31

### Fixed

- A preview no longer contradicts itself. When a write would change nothing,
  every item it returns now says `unchanged` rather than `modified`, so the item
  list agrees with the operation-level verdict instead of describing a change
  the preview just said would not happen.
- Undoing a refused undo no longer writes another refusal row. The coded error
  and exit code are unchanged; the journal simply stops growing when a caller
  retries an undo that was already refused.

### Added

- The MCP server reports its own package version through `workspace_info`, so a
  caller can record which build it measured without running the binary.

### Changed

- The README now states the distinction between `changed` and `would_change`
  where the response fields are documented: `changed` says an operation executed
  and was journalled, `would_change` says the bytes would differ.

## 0.1.6 — 2026-10-31

### Fixed

- A dry run now reports itself as a preview. It returns `changed: false` with no
  operation id, no after-hash and no size for a write it did not perform, and
  carries the digest the target would have plus a `would_change` flag saying
  whether the operation would change anything. A caller can tell a preview from a
  completed write without reading prose, and a preview is never mistaken for
  durable state.
- A copy's journal row now records its source, so history names where the copied
  content came from as well as where it went. An undo that is refused because the
  target no longer matches its recorded state leaves a record of that refusal, and
  that record cannot itself be undone: a failed undo is evidence, not a new
  operation to reverse.
- Error hints now name the interface the caller is actually on — the Core keyword
  argument, the CLI flag or the MCP argument — instead of putting one surface's
  name on every surface. A truncated patch reports the hunk's expected and actual
  line counts, so a failed application says how far off the hunk was.

### Changed

- `filekeeper-cli` 0.1.6 and `filekeeper-mcp` 0.1.6 are released together and
  carry the same version number.

## 0.1.5 — 2026-10-31

**Cut but never published.** Three fixes landed after this version was built, so
its artefacts would not match what a consumer surveyed under this number. Its
changes ship in 0.1.6; no 0.1.5 artefact reached users.

### Fixed

- Undo no longer reports success when it changed nothing. A committed operation
  leaves every affected path in its post-operation state, so an undo whose
  recorded target no longer matches that state means the path was edited, deleted
  or renamed away — not that the undo had already run. It now refuses with
  `UNDO_CONFLICT`, exit code 12 on the CLI, and names the conflicting paths
  relative to the caller's root, instead of returning `ok: true` with an empty
  item list. An agent that retried an undo on a stale operation id gets a code it
  can branch on rather than a false confirmation. The same refusal covers an
  identical-content committed write: undoing it would change nothing, so the
  message now says the target already holds its pre-operation state.
- An MCP tool result is now one payload instead of two. Every successful call
  carried the compact JSON once in a text block and again in `structuredContent`,
  doubling the bytes an agent receives per call — roughly halved now for a write
  and a read. The structured object is the single channel and its contents are
  unchanged; a successful call's text block is empty. This is a wire-shape change
  for any client that read successes from text only.
- An MCP error now reaches a text-only client intact. Clients that read an error
  from text blocks alone used to fall back to a fixed sentence, "MCP tool returned
  an error", dropping the code, message, `retryable` flag and `details`. The text
  block is now serialized from the same payload object that goes into
  `structuredContent`, so an agent sees the actual `PATH_NOT_FOUND`,
  `CONTENT_CONFLICT`, `SENSITIVE_PATH_REFUSED` or `STORAGE_ERROR` document — with
  its recovery hint and relative paths — and the two channels cannot drift. The
  text copy is scrubbed by the same envelope, so it leaks no host path either.

### Changed

- `filekeeper-cli` 0.1.5 and `filekeeper-mcp` 0.1.5 carry the same version number.
  This version was built but not published, so its changes reach users in 0.1.6.

## 0.1.4 — 2026-10-31

### Fixed

- `keeper init` no longer defeats the `--root /` fallback. 0.1.3 wrote
  `state_location = "project"` into the generated `config.toml`, and an explicit
  `state_location` refuses the global fallback — so anyone who ran the documented
  setup step got `STORAGE_ERROR: cannot create state directory: Permission
  denied` for `--root /` instead of the warning and the global store. The
  generated file now carries the setting only as a commented example; a genuine
  operator choice, written by hand as a live key, still fails loudly. A
  configuration generated by 0.1.3 still contains the live key: delete that line
  to restore the fallback.
- A state-directory failure no longer puts an absolute host path in the public
  error envelope. `details.state_dir` is rendered workspace-relative
  (`<root>/.filekeeper` becomes `.filekeeper`) or, for the global store, relative
  to the home directory (`workspaces/<id>`). The MCP adapter scrubs every value
  in `details` rather than exempting `state_dir`, and registry and database
  failures name the location in prose instead of quoting the host path.
- A dry run touches no state. It no longer creates the state directory or opens
  the journal, so a preview answers even where the real mutation would be
  refused, and `--dry-run --root /` after `keeper init` returns a preview with a
  warning instead of a storage error.

### Changed

- `filekeeper-cli` 0.1.4 and `filekeeper-mcp` 0.1.4 are released together and
  carry the same version number.

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
- The distribution-isolation test installs the built wheels with `--no-deps` and
  asserts the installed version against `pyproject.toml`, so it proves this build
  rather than whichever release the index happens to satisfy. The adapter's own
  runtime requirements are installed explicitly from its metadata. No product
  behaviour changed.

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

## 0.1.0 — 2026-10-31

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
