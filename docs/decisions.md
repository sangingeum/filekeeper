# Design decisions

Choices the specification left open, and why each was made. Where the plan and
the design review disagreed, the review won; this file records what remained
undecided and how it was settled.

## State is split: home registry, project-local workspace state

The specification put everything in one global directory; the design review
requires project-local state by default. Both cannot be true of a registry that
lists many roots.

Settled as: workspace *registrations* and settings live in the home database
(`metadata.sqlite3`), because no single workspace may own the list of all
workspaces. Per-workspace state (operations, operation items, blobs, locks) lives
in `<workspace>/.filekeeper/state.sqlite3` by default, so history travels with the
checkout. `state_location = "global"` in `config.toml` instead uses
`<home>/workspaces/<workspace-id>/`, keeping per-project attribution while making
the store survive deleting the checkout.

Consequence: two migration chains rather than one. `database.py` exposes
`migrate_registry` and `migrate_workspace` over the same forward-only runner.

## A single registered workspace is selected automatically

Selection precedence is explicit `--workspace`, explicit `--root`, then the
configured default. When there is no default and exactly one workspace is
registered, that workspace is used. This is not a guess between alternatives; the
specification only forbids choosing arbitrarily when several exist. With several
workspaces and no default, the command fails with `NO_WORKSPACE_SELECTED` and
lists them.

## `--root` outside the registry is a transient workspace

Naming an unregistered directory with `--root` runs the operation inside it with
exactly the same confinement as a registered workspace, but records nothing and
creates no registration. This keeps one-off use cheap without ever broadening the
allowed roots silently: the root comes from the caller's explicit argument, which
is the same authority as `--workspace`.

## The CLI uses `argparse`, not a CLI framework

Core must be dependency-free, and the review requires that installing the CLI does
not pull in the MCP SDK. Keeping the whole `filekeeper` distribution at zero
third-party dependencies is simpler to state and verify than "Core is clean but the
CLI is not". `argparse` covers the command tree; the cost is a little more
boilerplate in `cli/app.py`.

## Directory operations get their own `dir` group

The specification's proposed command tree only shows `file` commands, yet the
operation list includes `create_directory`, `move_directory` and
`delete_directory`. The CLI therefore has `keeper dir create|move|delete`. Helpers
that are Core-only but not in the required CLI list (`read_multiple_files`) are
exposed as `keeper file read-many` because it reduces round trips, which is the
stated purpose of the operation.

## Credential refusal covers writes, not reads

The review asks for refusal on writes. Reads of `.env` or `id_rsa` are still
allowed: the tool is often the thing that needs to inspect such a file, and
refusing reads is not what was asked. Refusal is a name-shape check (file name,
suffix, or containing key directory), not a content scan, so it is cheap and
predictable. The override is `allow_sensitive_write=True` in Core and
`--i-accept-secret-write-risk` on the CLI; both names state what is being accepted.

## Sensitive-path refusal precedes parent-directory validation

A write to `.ssh/config` when `.ssh/` does not exist reports
`SENSITIVE_PATH_REFUSED`, not a missing-parent error. The refusal is about the
target's meaning, and reporting it first avoids teaching a caller to create the
directory and retry.

## Newlines are never normalised; BOMs are preserved

Content is decoded, and re-encoded exactly. `\r\n` stays `\r\n`; a UTF-8 BOM
survives a rewrite; the detected convention is reported in results
(`newline`, `has_bom`) and never applied. This follows the specification's "do not
silently normalize line endings" literally, including when the caller's content
uses a different convention than the file it replaces.

Encoding inheritance: an existing file is decoded to learn its encoding (and BOM),
and the rewrite uses it unless the caller names an encoding explicitly. New files
default to UTF-8 without a BOM.

## Text reads refuse binary content

`read_file` is a text operation. A file whose first 8 KiB contain a NUL byte, or
whose bytes do not decode, is refused with `BINARY_CONTENT_REFUSED` unless an
encoding is named explicitly. Binary transport is not part of this release and is
documented as a limitation rather than half-implemented.

## `..` is rejected lexically, not just canonically

`sub/../notes.txt` stays inside the workspace, but any relative path containing a
`..` segment is refused anyway. Such paths are never necessary for workspace work,
and a blanket rule is cheaper to audit than a proof that each normalisation is
benign. Absolute paths are canonically resolved and then bounded; they are never
rejected for shape alone.

## Symlinks: readable when they stay inside, never a write target

A symlink whose canonical target leaves the workspace is refused. A symlink whose
target stays inside may be read; its metadata is reported from the link itself
(`symlink_target`), not from the target. Writes to a symlink path are always
refused, because an atomic rename would replace the link rather than the file it
points at.

## Recursive directory deletion needs two explicit flags

`recursive=True` alone is not enough: `confirm_recursive=True` must also be passed
(CLI: `--recursive --confirm-recursive`). History is not recorded in this release,
so a deleted tree cannot be restored; two flags make the destructive intent
impossible to inherit by accident from a default.

## Copy and move refuse an occupied destination by default

`copy_file` overwrites an existing destination only when
`expected_destination_hash` matches the current content — the same optimistic
concurrency rule as `write_file`. `move_file` overwrites only with
`overwrite=True`. Neither operation is atomic across its two paths, and the README
says so in user-facing words.

## `dry_run` is available on mutations now

The specification ties dry-run to edits and patches, but reporting what a whole-file
mutation would do is cheap, useful and keeps the result shape identical. Dry runs
validate every guard (confinement, sensitive paths, expected hashes, size limits,
directory preconditions) and then stop before touching the filesystem.

## Exit code 10 for credential refusal

Codes 0–9 were proposed in the specification, with 7 and 8 reserved for patch and
partial-recovery failures. Credential refusal gets its own code (10) so a script
can distinguish "you asked me to write a secret" from a generic boundary violation
(4). Codes are documented in `README.md` and in `cli/exit_codes.py`.

## The blob store is included before the operations that use it

The review's ordering rule is that blobs are written and fsynced *before* a
pending operation record, so a lost journal record can never hold the only copy of
the previous bytes. `BlobStore` therefore exists now, with tests, even though no
operation writes to it in this release. The history release will add journaling
around it, not replace it.

## Filekeeper's own state is not an addressable path

Any path inside the workspace's `.filekeeper/` directory is refused with
`PATH_IS_STATE_PATH`. This is the enforcement half of the review's requirement that
the tool never records its own state files in its own history; it also stops
`--recursive` listings and searches from walking the state store.

## `.gitignore` handling is repository-aware

On the first mutation that creates the state directory, Filekeeper appends
`.filekeeper/` to `.gitignore` if the workspace is inside a git repository (it
creates the file if a repository has none). Outside a repository it writes
nothing: an unrequested `.gitignore` in an arbitrary directory is noise. A failed
update is reported in the result's warnings rather than failing the operation.

## Read byte limits are a ceiling, not just a default

`max_bytes` on a read narrows the limit but cannot exceed `max_read_bytes` from
`config.toml`. A caller cannot talk the tool into loading an unbounded file. The
same pattern applies to search results, directory entries and recursion depth.

## Boundary refusals keep distinct error codes and exit codes

A single `PATH_OUTSIDE_WORKSPACE` code conflated three different mistakes, so a
caller had to parse prose to tell them apart. The split is now part of the
contract: `..` traversal keeps `PATH_OUTSIDE_WORKSPACE` and exit code 4; an
absolute path that resolves outside the workspace is
`ABSOLUTE_PATH_OUTSIDE_WORKSPACE` (exit 6); a symlink whose target resolves
outside is `SYMLINK_ESCAPE_REFUSED` (exit 11). `SYMLINK_WRITE_REFUSED` stays
separate, because writing to a link is a different act from following one out of
the workspace. Every case is still a refusal: no input that was rejected before
is accepted now.

## Undo conflict has its own exit code because the specification's number was taken

The specification's suggested table assigned 6 to an undo/redo conflict, but the
existing contract already uses 6 for an absolute path that resolves outside the
workspace. Renumbering a documented code would break every script that branches
on it, so undo conflict takes the next free number, 12, and unusable history
takes 13. `UNDO_CONFLICT` and `HISTORY_UNAVAILABLE` are the corresponding stable
error codes.

## Undo accepts either recorded state, not only the post-operation state

The specification says to compare the current path with the recorded post-state
and restore the pre-state on a match. Taken alone, that makes a multi-file
operation that failed part-way impossible to undo: some paths are already at
their pre-state, so they would look like conflicts. The rule here is: a path may
match either the recorded pre-state or the recorded post-state. Matching
post-state means "restore it"; matching pre-state means "already there, leave
it". Matching neither means someone else changed it, and the whole undo is
refused before any byte is written. This keeps the safety property while letting
a partial batch be repaired.

## A directory is only removed when the whole undo owns it

Undoing a directory create must not delete files added afterwards. A directory
is therefore removable only when every entry inside it is also being removed by
the same undo (which is true for a recorded directory move or delete, and false
for a directory that gained new content). The check runs during validation, in
the same pass that detects hash conflicts, so a refusal changes nothing.

## Undo restores by rewriting content, not by renaming back

A file move could be undone with a rename, but undo writes the recorded previous
bytes through the atomic-write path and then removes the destination. That
verifies the blob hash on read, works for a copy as well as a move, and leaves a
single well-tested code path. The cost is an extra write inside the same
directory, which is cheap compared with the risk of a restore that trusts a path
instead of content.

## History size cap defaults to 4 MiB per file

The review requires a configurable cap so one large edit cannot consume the
store. Four mebibytes is above nearly every source file and well below the 20 MiB
write limit, so ordinary editing keeps full undo while a generated artefact or
large log does not. A skipped file is still recorded with its hashes; the result
names it in `history_skipped`, sets `undo_available` to false, and undo raises
`HISTORY_UNAVAILABLE` rather than pretending.

## A transient workspace records under the identifier `local`

`--root` for an unregistered directory creates no registration, so there is no
workspace id to store. Each such root has its own state database, so the literal
identifier `local` cannot collide across roots, and history still works for
one-off work. This does not widen any allowed root: the root is still the one the
caller named explicitly.

## Reconciliation reports three honest outcomes, not two

A pending operation is resolved as `committed` when every item matches its
post-state, `failed` when every item matches its pre-state, and `failed` with
`metadata.reconciled = "failed"` plus `metadata.unresolved` when the states are
mixed. The last case is a partial application: the schema's `partially_undone`
value describes an undo, not an interrupted mutation, so it is not reused here.
Blobs are never deleted during reconciliation, so even a failed record keeps the
previous bytes available for a later undo.

## A diff shows a move as a removal and an addition

`diff_operation` groups items by path; a move is recorded as the source path
losing its content and the destination gaining it. Rendering that as two hunks
is truthful and needs no move-detection heuristic. Binary items are reported as
binary with no textual diff rather than decoded speculatively.
