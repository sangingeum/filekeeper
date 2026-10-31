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
not pull in the MCP SDK. Keeping the whole `filekeeper-cli` distribution at zero
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

## Exact-text replacement is the primary edit, line ranges are secondary

The specification made line-range replacement the essential coding-agent edit.
Measured use says otherwise: exact-text replacement does not need a prior read to
discover line numbers, so it costs one fewer round trip per edit. `replace_text`
is therefore the primary surface, and `replace_lines`, `insert_lines` and
`delete_lines` stay for genuinely positional work. All four share the same
journal, guard and undo path.

## Replacement matching is staged, and the stage is part of the result

A strict literal-only match would reject edits that differ only in indentation,
which is a common and harmless drift; a silently tolerant match would hide the
fact that the edit landed somewhere else. The stages are therefore explicit and
ordered: `exact`, then a whitespace-normalised match reported as `normalised`,
then a `NO_MATCH` refusal with the nearest candidate lines. A normalised match is
never applied without that word in the result. In the normalised stage the
replacement is re-based onto the indentation of the line it replaces, keeping the
caller's relative indentation inside a multi-line block.

A caller must choose `first` or `all`: there is no default policy that could
quietly change more or less than intended. `match_count` reports the number of
matches found before the policy is applied; `expected_match_count` turns a
different number into a refusal rather than a surprise.

`expected_content` guards one target region and must equal its exact text,
including the file's indentation. With `all` and more than one matched region it
is refused as ambiguous, because one expected value cannot describe several
regions.

## Line-range edits preserve the file's shape, not the caller's sloppiness

Ranges are 1-based and inclusive and are rejected when out of range; clamping
would silently edit the wrong lines. End-of-file insertion is the explicit
`position="end"` and is never encoded as a sentinel line number. The file's
dominant newline and byte-order mark are preserved, and replacement lines are
written in that convention. If the caller's replacement omits a trailing
newline, the termination of the line it replaces is kept, so a line edit cannot
accidentally join two lines or strip a file's final newline.

## A patch is validated completely, then applied recoverably

Every hunk is parsed, every path resolved, and every preimage checked against
the file in memory before a byte is written. A failed hunk rejects the whole
patch; an ambiguous hunk (its preimage occurs more than once and the stated line
does not disambiguate it) is rejected rather than guessed. This makes the common
failure cases harmless.

The remaining failure mode is a write that fails part-way through several files.
That cannot be made atomic across paths, so it is made recoverable: all affected
files are recorded as one logical operation, and a failure after earlier writes
restores those files to their pre-patch bytes in reverse order before reporting
the failure. If restoration itself fails, the error says which paths were
restored and which were not instead of claiming success. Undo of a multi-file
patch validates every path first and restores every file as one operation.

A patch that creates or deletes a file is expressed with `/dev/null` on the
relevant side, as unified diff does. Renaming patches are refused: the diff
format carries no content-based move semantics that could be verified safely.

## Diff rendering states a missing final newline instead of gluing lines

`difflib.unified_diff` returns each content line exactly as it was supplied. A
final line without a newline therefore had no terminator and ran into the next
rendered line, producing output that no patch consumer could read. The renderer
now normalises every content line to end with a newline and, when a side has no
final newline, emits the standard `\ No newline at end of file` marker after the
line that represents that final line. The human view, the JSON payload and the
dry-run preview all use this one renderer, so they cannot disagree.

## The MCP adapter is a separate distribution, and its SDK major is pinned

The specification placed `mcp/` inside `src/filekeeper/` while also requiring Core
to stay free of MCP SDK dependencies. Both cannot hold in one distribution: a
single package would pull the SDK into every CLI install. The design review's
two-distribution layout therefore wins. `filekeeper-mcp` depends on
`filekeeper-cli>=0.1,<0.2` and on the SDK; `filekeeper-cli` keeps an empty
dependency list, and a wheel-level test proves both directions.

The SDK is pinned to `mcp>=2.3,<3`. Version 2.x renamed the ergonomic server class
to `MCPServer` (from `FastMCP`) and changed how handlers and transports are
wired, so an unpinned requirement would let a major upgrade change the API under
a published server. The lower bound is the version the adapter was built and
tested against, not a guess at older compatibility; the upper bound is a major
version we have not exercised. Raising either bound is a deliberate change that
needs the adapter tests run against the new version.

## Adapter request ceilings are fixed, not caller-configurable

Core's limits are configurable in `config.toml`, which is right for a single-user
tool. A protocol server also needs a bound that one request cannot raise: a
client that asks for a million search results must be refused before the server
allocates for it. The adapter therefore adds its own ceilings — 32 paths in
`file_read_many`, a 1 MiB patch, 200 search results, 1000 directory entries, 200
history entries — and refuses excess with `LIMIT_EXCEEDED`, naming the limit and
the actual size. These are protocol shape, not domain rules, so they live in the
adapter; the domain limits stay in Core and are never duplicated.

## Error payloads are sanitised at the adapter boundary

Core's error messages are written for a local operator and may quote an operating
system message that contains a resolved path. An MCP client is not that operator.
The adapter keeps the stable code, the message's meaning, the `retryable` flag and
the caller's own path, drops detail keys that only carry server-side locations,
and replaces the workspace root, the home directory and any absolute-path token
with `<host-path>`. A storage failure therefore still says `STORAGE_ERROR` and
still names the path the caller asked for, without disclosing the tree the server
is confined to.

## Mutations take their audit identity from start-up configuration

`actor` and `task_id` are optional server start-up options. A tool argument does
not set them, so a caller cannot claim an identity it was not given; the fields
stay generic audit metadata. The `history_list` filters named `actor` and
`task_id` are queries over what was recorded, not authority to record anything.

## Every operation is identified by `operation_id`

History items were keyed `id` while mutations, diff, undo and the MCP tools used
`operation_id`. One concept with two access patterns forces every client to know
which surface produced a document. History records now expose `operation_id`
everywhere — `history list` items, `history show`, `history diff`, the undo
argument and every mutation result. The database column stays `id`; the mapping
happens once, when a row becomes an `OperationRecord`.

`id` is not kept as a deprecated alias. A second key would be the same defect
with a longer name, and the field is new enough that no published client can
depend on it.

## One error envelope for the CLI and the MCP server

Errors used to differ by adapter: the CLI emitted `{"ok": false, "error": {...}}`
while the MCP server nested the code the same way but also duplicated `path` and
`workspace` at the top level. Successes were flat, so a client needed one shape
for success and two for failure.

The canonical envelope is now the CLI's: a success document is flat with `ok`
first; a failure document has exactly `ok` and `error`, and `error` carries
`code`, `message`, `retryable` and, when present, `details`. The caller's own path
lives in `error.details.path`, which Core already populates for most errors; the
adapter fills it in when Core recorded only a hash. Nothing is duplicated at the
top level. Exit-code mapping is unchanged.

`ContentConflictError` reports `retryable: false`. The identical request cannot
succeed, so the payload names the real recovery instead: re-read the target, then
write against the hash that read returned. There is no separate "retryable after
re-read" state; `details.recovery` is the whole contract.

## An undo record reports only history that really was skipped

`history_skipped` means the bytes needed to undo were not stored — the
`max_history_blob_bytes` path. An undo names hashes whose blobs the original
operation already stored, so its record now reports an empty `history_skipped`;
the flag is set only when the blob is genuinely absent from the store.

## A permission failure on a read is a storage error, reported with the caller's path

The write path already mapped `EACCES`/`EPERM` to `STORAGE_ERROR` (exit 9) and
named only the caller's workspace-relative path. The read path let the operating
system's `OSError` escape to the CLI's last-resort handler, which reported
`INTERNAL_ERROR` (exit 1) and quoted the resolved host path inside the operating
system's message. Two things were wrong at once: the code said "bug" for an
environment condition, and the message disclosed the tree the tool is confined to.

Read syscalls now raise `StorageError` through one helper that keeps only the
standard reason phrase (`Permission denied`) and the workspace-relative path.
`INTERNAL_ERROR` therefore still means what it says: an unexpected fault that the
read path did not classify.

`keeper file read-many` reports per-file failures as data, so it has no single
error object. Its process exit code is taken from the first failed entry's code
instead of always being 3. That keeps the documented meaning of each exit code on
the command that produced it, and a mixed batch still exits non-zero. No exit-code
number or mapping changed, and no new error code was added.


Path search and content search share one directory walker, so the same rule
applies to `keeper file search`: an unreadable requested root raises
`STORAGE_ERROR` rather than returning an empty, misleadingly successful result.
Unreadable nested entries are still skipped and the readable results are
returned. The rule is "an unreadable root is a failure, an unreadable child is
not" — the caller named the root, so the tool may not answer its question with
silence. The mutation and undo surfaces obey the same rule: copy, move and undo
raise the same error with the caller's relative path when they cannot read or
hash a file they were asked to act on.

## The PyPI distribution is named `filekeeper-cli`, the import package is `filekeeper`

PyPI rejected `filekeeper` as too similar to existing project names, so the
distribution is published as `filekeeper-cli`. The import package and the
console script keep their names: `import filekeeper` works, and the command is
still `keeper`. The name split is deliberate — the import package is the
product's identity and renaming it would churn every user, test and document for
a packaging constraint. The MCP distribution keeps its own name,
`filekeeper-mcp`, and depends on `filekeeper-cli`. It is registered and
published only after `filekeeper-cli` exists on PyPI, because PyPI accepts one
pending project at a time.

## The two distributions ride separate tag trains

`filekeeper-cli` and `filekeeper-mcp` version independently, and PyPI never lets
a version number be reused for a project. So the release workflow has two tag
trains: `v*` publishes `filekeeper-cli` and `mcp-v*` publishes `filekeeper-mcp`.
The patterns cannot overlap — `v*` is anchored at the first character, so
`mcp-v0.1.0` does not match it — and each publish job is additionally gated on
its own tag prefix, so a single tag can never upload both distributions.

One tag that uploaded twice would be a trap: a partial failure would leave two
version numbers burned with only one artefact on PyPI, and neither half could be
published again under the same version. Two trains keep each project's history
independent: the MCP server can ship a protocol fix without moving the Core
version, and the Core can release without republishing an unchanged server. The
manual workflow trigger builds and inspects both artefact sets and uploads
neither, because a manual run carries no tag to prove which version is intended.

## State falls back when the workspace root cannot host it

The first deployment tried with `--root /` could not mutate anything. The journal
wanted `/.filekeeper`, the kernel refused it, the bare `mkdir` in
`state_database_path` let the `OSError` escape, and the caller received
`INTERNAL_ERROR` quoting an absolute host path. Dry runs still said yes because a
dry run never opened the journal, so the tool previewed work it could not perform.

Settled as: every state-directory construction raises `STORAGE_ERROR`, and the
location is resolved once, before any operation runs. `<root>/.filekeeper` is
used when it is usable. When it is not, and `config.toml` did not name
`state_location` explicitly, the global store (`<home>/workspaces/<id>/`) is used
and the success payload carries a warning saying so. An explicit choice is
honoured and fails with `STORAGE_ERROR` naming `details.state_dir` — the operator
made that choice, so redirecting it silently would be worse than refusing. A dry
run opens the journal before reporting, because a preview that cannot be
performed is a lie. `details.state_dir` stays verbatim in the MCP envelope: it is
an operator-supplied location and the thing they need in order to fix the
deployment, so it is not scrubbed like a host path.

## A transient workspace identity is derived from its root

Per-workspace state is keyed by workspace id. A transient `--root` workspace
used to draw `ws-transient-<random>` per process, so with global state every
process wrote its own journal and history did not survive a restart. The id is
now a truncated SHA-256 of the canonical absolute root, which makes the same root
map to the same workspace across processes without a registry entry.

Existing random-id directories are not migrated automatically. The workspace
database records no root path, so there is nothing to match a legacy directory
against: adopting one would be a guess, and guessing could attach another root's
history to this one. They are left untouched on disk and the response warning
reports how many exist, so nothing is orphaned silently; an operator who wants
that history can point `state_location` at it deliberately.

## Argument validation keeps the error taxonomy

The MCP SDK validates a tool's arguments before the handler runs. That failure
was prose — `1 validation error for file_copyArguments` — with no code, so an
agent could not branch on its own mistake. The server now registers each tool as
a subclass of the SDK's `Tool` that intercepts argument validation and renders
`INVALID_ARGUMENT` with the expected arguments, the missing required ones and the
unexpected ones. The published schema also marks unknown arguments as refused.

The SDK exposes no public way to register a `Tool` subclass; `add_tool` accepts a
function and builds the base `Tool` itself. The coded tool is therefore inserted
into the tool manager directly. That is pinned by `mcp>=2.3,<3`, covered by a
protocol test, and the failure mode is loud rather than silent: if the layout
changes, the coded-error test fails.

## The distribution-isolation proof names the wheel it built

The two-distribution boundary is proved by installing real wheels into fresh
environments. 0.1.3's first release run failed here for a reason that had nothing
to do with the product: the adapter wheel declares `filekeeper-cli>=0.1,<0.2`, and
the installer satisfied that requirement from the index — the published 0.1.2 —
even though the core wheel was named in the same command. The probe then imported
0.1.2 and the version assertion failed. The defect could only appear once PyPI
held a different version inside the range, which is exactly release time.

The install now passes `--no-deps`, so the core and the adapter under test are the
wheels this run just built. The adapter's own runtime requirements are read from
its wheel metadata and installed explicitly, so "the adapter pulls the SDK" is
still proved by a real SDK import rather than by the installer's dependency
resolution. Core alone still installs nothing from the index.

Both tests then assert that the installed distribution version equals the version
in `pyproject.toml`, and a mismatch reports the wheel path and the source the
installed distribution actually came from. The assertion exists so a future
installer behaviour that reaches the index fails with the wheel and the source in
the message, instead of a bare version-string comparison. The suite is runnable
with `UV_OFFLINE=1`, which is how the "core no longer consults PyPI" claim is
checked.

## Deferred and accepted limitations

The trial also asked for unbounded `directory_list`/`file_search` payloads, a
last-N-lines read, rename-plus-edit, patches that create or delete files, a
single copy of the payload in `content[0].text` and `structuredContent`, and
context lines in `content_search`. None is fixed in 0.1.3. The duplicated
envelope is **accepted**: `structuredContent` is the protocol's structured
channel and the text block is what older hosts read, so keeping both is
compatible behaviour, not an oversight. The rest are **deferred**: bounding and
paging listings needs a cursor or limit contract across CLI and MCP; last-N-lines
needs an end-relative range that does not conflict with "out-of-range is refused";
rename-plus-edit and file-creating/deleting patches are new operation shapes with
their own undo semantics; and context lines change the match result shape. They
are the next round's starting point, not silent gaps.
