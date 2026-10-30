"""The MCP tool set: one thin function per Core call.

Each function validates only the protocol-level shape of its arguments (types,
array bounds, payload caps) and then delegates. Semantic validation — path
confinement, guard comparison, line ranges, patch preimages — stays in Core, so
the CLI and this adapter cannot disagree about what is allowed.

Line numbers are 1-based and inclusive, everywhere.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import mcp.types as types

from filekeeper import Filekeeper, FilekeeperError, FilekeeperService
from filekeeper.core.errors import LimitExceededError, NoWorkspaceSelectedError

from .errors import error_result, internal_error_result
from .payloads import compact_json, jsonable, success_payload

logger = logging.getLogger(__name__)

# Adapter-side ceilings. Core has its own, configurable limits; these keep a
# single protocol call from asking the server process to hold an unbounded
# amount of data in memory before Core ever sees it.
MAX_READ_MANY_FILES = 32
MAX_PATCH_BYTES = 1024 * 1024
MAX_SEARCH_RESULTS = 200
MAX_DIRECTORY_ENTRIES = 1000
MAX_HISTORY_ENTRIES = 200

READ_ONLY = types.ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
MUTATING = types.ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
ADDITIVE = types.ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)

class WorkspaceSelection:
    """The workspace chosen by server start-up configuration.

    No tool argument can widen it. When start-up found no usable workspace, the
    error is kept and raised by every workspace-scoped call.
    """

    def __init__(self, app: Filekeeper, *, workspace: str | None, root: str | None) -> None:
        self.app = app
        self._service: FilekeeperService | None = None
        self._error: FilekeeperError | None = None
        try:
            self._service = app.workspace(name=workspace, root=root)
        except FilekeeperError as error:
            self._error = error

    def service(self) -> FilekeeperService:
        if self._service is not None:
            return self._service
        if self._error is not None:
            raise self._error
        raise NoWorkspaceSelectedError(
            "no workspace selected: start the server with --workspace or --root"
        )

    def selected_name(self) -> str | None:
        return None if self._service is None else self._service.name

    def host_paths(self) -> tuple[str, ...]:
        paths = [str(self.app.home)]
        if self._service is not None:
            paths.append(str(self._service.root))
            paths.append(str(self._service.handle.state_dir))
        return tuple(dict.fromkeys(paths))


def _enforce_max(parameter: str, value: int | None, maximum: int) -> None:
    if value is not None and value > maximum:
        raise LimitExceededError(
            f"{parameter} is limited to {maximum} in this server",
            limit=maximum,
            actual=value,
            details={"parameter": parameter},
        )


def _check_size(parameter: str, text: str, maximum: int) -> None:
    size = len(text.encode("utf-8"))
    if size > maximum:
        raise LimitExceededError(
            f"{parameter} is limited to {maximum} bytes in this server",
            limit=maximum,
            actual=size,
            details={"parameter": parameter},
        )


def _ok(payload: dict[str, Any]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=compact_json(payload))],
        structured_content=payload,
    )


class FilekeeperTools:
    """The tool surface. All filesystem work happens inside Core."""

    def __init__(
        self, selection: WorkspaceSelection, *, actor: str | None, task_id: str | None
    ) -> None:
        self.selection = selection
        self.actor = actor
        self.task_id = task_id

    # -- delegation ---------------------------------------------------------
    def _run(
        self,
        call: Callable[[FilekeeperService], Any],
        *,
        path: str | None = None,
        build: Callable[[Any], dict[str, Any]] | None = None,
        guard: Callable[[], None] | None = None,
    ) -> types.CallToolResult:
        workspace: str | None = self.selection.selected_name()
        try:
            if guard is not None:
                guard()
            service = self.selection.service()
            workspace = service.name
            result = call(service)
        except FilekeeperError as error:
            return error_result(
                error,
                host_paths=self.selection.host_paths(),
                caller_path=path,
                workspace=workspace,
            )
        except Exception:
            logger.exception("unhandled failure in an MCP tool")
            return internal_error_result(caller_path=path, workspace=workspace)
        payload = build(result) if build is not None else success_payload(result)
        return _ok(payload)

    def _audit(self) -> dict[str, str]:
        audit: dict[str, str] = {}
        if self.actor is not None:
            audit["actor"] = self.actor
        if self.task_id is not None:
            audit["task_id"] = self.task_id
        return audit

    def _mutation(
        self, call: Callable[[FilekeeperService], Any], *, path: str | None
    ) -> types.CallToolResult:
        return self._run(call, path=path)

    # -- workspace ----------------------------------------------------------
    def workspace_list(self) -> types.CallToolResult:
        """List the workspaces registered in the server's home directory.

        Read-only. The configured default is named separately. Registration is
        administration done elsewhere; this tool never registers or removes one.
        """

        def build(workspaces: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "default_workspace": self.selection.app.registry.get_default_name(),
                "workspaces": jsonable(workspaces),
            }

        return self._run(lambda _service: self.selection.app.list_workspaces(), build=build)

    def workspace_info(self, workspace: str | None = None) -> types.CallToolResult:
        """Describe the selected workspace, or one registered workspace by name.

        Read-only. Reports the workspace name, identifier, registered root and
        whether the selection is a transient --root rather than a registration.
        It does not create, select or modify a workspace.
        """

        def build(_ignored: Any) -> dict[str, Any]:
            app = self.selection.app
            if workspace is not None:
                record = app.get_workspace(workspace)
                is_default = app.registry.get_default_name() == record.name
                return {
                    "ok": True,
                    "workspace": jsonable(record),
                    "selected": self.selection.selected_name(),
                    "default": is_default,
                    "registered": True,
                }
            service = self.selection.service()
            handle = service.handle
            return {
                "ok": True,
                "selected": service.name,
                "workspace": {
                    "name": service.name,
                    "id": handle.workspace_id,
                    "root_path": str(service.root),
                },
                "registered": service.registered,
            }

        return self._run(
            lambda service: service,
            build=build,
            path=None,
        )

    # -- read and inspection ------------------------------------------------
    def file_read(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes: int | None = None,
        encoding: str | None = None,
    ) -> types.CallToolResult:
        """Read a text file, optionally a bounded line range.

        Line numbers are 1-based and inclusive. Omitting both returns from the
        first line. A byte limit smaller than the range truncates honestly:
        truncated, truncation_reason and next_start_line say where to resume. A
        UTF-8 byte-order mark is preserved and line endings are returned exactly
        as stored. Binary content is refused with BINARY_CONTENT_REFUSED unless
        an encoding is named. Read-only; no history is recorded.
        """

        return self._run(
            lambda service: service.read_file(
                path,
                start_line=start_line,
                end_line=end_line,
                max_bytes=max_bytes,
                encoding=encoding,
            ),
            path=path,
        )

    def file_read_many(
        self,
        paths: list[str],
        start_line: int | None = None,
        end_line: int | None = None,
        max_bytes_per_file: int | None = None,
    ) -> types.CallToolResult:
        """Read several text files in one call (at most 32 paths).

        Each entry reports its own path, ok flag, result or error; one missing
        file does not hide the others. Line numbers are 1-based and inclusive and
        apply to every file. The call itself is read-only. More than 32 paths is
        refused with LIMIT_EXCEEDED rather than silently truncated.
        """

        def build(entries: Any) -> dict[str, Any]:
            files = jsonable(entries)
            return {
                "ok": all(entry.ok for entry in entries),
                "files": files,
                "count": len(files),
            }

        return self._run(
            lambda service: service.read_multiple_files(
                paths,
                max_bytes_per_file=max_bytes_per_file,
                start_line=start_line,
                end_line=end_line,
            ),
            path=None,
            build=build,
            guard=lambda: _enforce_max("paths", len(paths), MAX_READ_MANY_FILES),
        )

    def directory_list(
        self,
        path: str = ".",
        recursive: bool = False,
        glob: str | None = None,
        max_entries: int | None = None,
    ) -> types.CallToolResult:
        """List directory entries under a workspace-relative path.

        Read-only. Recursion depth and entries are bounded by configuration; a
        truncated listing says so instead of pretending it is complete. The
        server refuses max_entries above 1000. Filekeeper's own .filekeeper/
        state directory is never listed.
        """

        return self._run(
            lambda service: service.list_directory(
                path, recursive=recursive, glob=glob, max_entries=max_entries
            ),
            path=path,
            guard=lambda: _enforce_max("max_entries", max_entries, MAX_DIRECTORY_ENTRIES),
        )

    def file_info(self, path: str) -> types.CallToolResult:
        """Report metadata for one path, including hash, encoding and newline for text.

        Read-only. The content_hash is what the guarded mutating tools accept as
        expected_hash. Symlink metadata describes the link, not its target.
        """

        return self._run(lambda service: service.get_file_info(path), path=path)

    def file_search(
        self, path: str, glob: str, max_results: int | None = None
    ) -> types.CallToolResult:
        """Find workspace paths matching a glob, under one directory.

        Read-only. At most 200 results may be requested; a larger request is
        refused with LIMIT_EXCEEDED. A truncated result says so. Literal file
        names are faster with directory_list when the directory is known.
        """

        return self._run(
            lambda service: service.search_files(path, glob, max_results=max_results),
            path=path,
            guard=lambda: _enforce_max("max_results", max_results, MAX_SEARCH_RESULTS),
        )

    def content_search(
        self,
        path: str,
        query: str,
        regex: bool = False,
        glob: str | None = None,
        max_results: int | None = None,
        case_sensitive: bool = True,
    ) -> types.CallToolResult:
        """Search file contents by literal text (default) or a regular expression.

        Read-only. Matches carry path, 1-based line number, column and the whole
        line. At most 200 matches may be requested; a larger request is refused
        with LIMIT_EXCEEDED and a truncated result says so. An invalid regular
        expression is INVALID_PATTERN. Binary files are skipped.
        """

        return self._run(
            lambda service: service.search_content(
                path,
                query,
                regex=regex,
                glob=glob,
                max_results=max_results,
                case_sensitive=case_sensitive,
            ),
            path=path,
            guard=lambda: _enforce_max("max_results", max_results, MAX_SEARCH_RESULTS),
        )

    # -- mutations ----------------------------------------------------------
    def file_create(
        self,
        path: str,
        content: str,
        expected_absent: bool = True,
        encoding: str = "utf-8",
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Create a new text file and return operation_id and undo_available.

        Fails with ALREADY_EXISTS while expected_absent is true (the safe
        default); set it false to overwrite an existing file, which makes the
        call destructive. Writes are atomic. Credential-shaped targets are
        refused with SENSITIVE_PATH_REFUSED unless i_accept_secret_write_risk is
        true, which records a warning and is itself undoable. dry_run validates
        every guard and writes nothing, returning operation_id null. Otherwise
        the operation is recorded and undoable.
        """

        return self._mutation(
            lambda service: service.create_file(
                path,
                content,
                expected_absent=expected_absent,
                encoding=encoding,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_write(
        self,
        path: str,
        content: str,
        expected_hash: str | None = None,
        encoding: str | None = None,
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Replace a whole file with new content and return operation_id and undo_available.

        The replacement is atomic: a crash leaves the complete previous content.
        expected_hash guards against clobbering a file that changed since it was
        read; a mismatch is CONTENT_CONFLICT and nothing is written. The file's
        encoding and byte-order mark are inherited unless encoding is named.
        Credential-shaped targets are refused unless i_accept_secret_write_risk
        is true. dry_run validates and writes nothing. Otherwise the operation is
        recorded and undoable. Not idempotent: each successful call adds a
        history record.
        """

        return self._mutation(
            lambda service: service.write_file(
                path,
                content,
                expected_hash=expected_hash,
                encoding=encoding,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_copy(
        self,
        source: str,
        destination: str,
        expected_destination_hash: str | None = None,
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Copy one file inside the workspace and return operation_id and undo_available.

        An occupied destination is refused unless expected_destination_hash
        matches its current content. The copy is not atomic across its two paths,
        but it is recorded as one operation, so undo restores both ends. The
        destination is subject to the credential-shaped refusal. dry_run writes
        nothing. Otherwise the operation is undoable.
        """

        return self._mutation(
            lambda service: service.copy_file(
                source,
                destination,
                expected_destination_hash=expected_destination_hash,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=destination,
        )

    def file_move(
        self,
        source: str,
        destination: str,
        overwrite: bool = False,
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Move or rename one file inside the workspace and return operation_id and undo_available.

        An occupied destination is refused unless overwrite is true, which makes
        the call destructive at the destination. The move is recorded as one
        operation and undo restores both ends. Credential-shaped destinations are
        refused. dry_run writes nothing. Otherwise the operation is undoable.
        """

        return self._mutation(
            lambda service: service.move_file(
                source,
                destination,
                overwrite=overwrite,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=destination,
        )

    def file_delete(
        self,
        path: str,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Delete one file and return operation_id and undo_available. Destructive.

        Deletes exactly the named file: no glob, no recursion, no directory
        removal, and no widening of the workspace root. expected_hash refuses the
        delete when the content changed since it was read (CONTENT_CONFLICT).
        dry_run validates and deletes nothing. Otherwise the operation is
        recorded, and undo restores the file while nothing else has recreated
        that path.
        """

        return self._mutation(
            lambda service: service.delete_file(
                path,
                expected_hash=expected_hash,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def directory_create(
        self,
        path: str,
        parents: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Create one directory and return operation_id and undo_available.

        Additive: no existing file or directory is replaced. parents controls
        whether missing ancestors may be created. dry_run validates and creates
        nothing. Otherwise the operation is recorded, and undo removes the
        directory only while everything inside it is exactly what the create
        produced.
        """

        return self._mutation(
            lambda service: service.create_directory(
                path, parents=parents, dry_run=dry_run, **self._audit()
            ),
            path=path,
        )

    def directory_delete(
        self,
        path: str,
        recursive: bool = False,
        confirm_recursive: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Delete one directory and return operation_id and undo_available. Destructive.

        A non-empty directory requires recursive true and confirm_recursive true;
        one without the other is refused before anything is touched. The scope is
        exactly the named subtree inside the selected workspace root — this tool
        cannot delete outside it, and Filekeeper's own .filekeeper/ state
        directory is refused. dry_run lists what would be removed and deletes
        nothing. Otherwise the whole tree is recorded as one operation and is
        undoable.
        """

        return self._mutation(
            lambda service: service.delete_directory(
                path,
                recursive=recursive,
                confirm_recursive=confirm_recursive,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    # -- text editing -------------------------------------------------------
    def file_replace_text(
        self,
        path: str,
        old: str,
        new: str,
        match: str,
        expected_hash: str | None = None,
        expected_content: str | None = None,
        expected_match_count: int | None = None,
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Replace literal text and return operation_id, undo_available and the match stage.

        Matching is staged and the result always names the stage: exact first,
        then normalised (indentation and trailing whitespace tolerated, the
        replacement re-based onto the matched indentation), then a NO_MATCH
        refusal carrying the nearest candidate lines. match is required and is
        either first or all; there is no default policy. match_count reports the
        matches found, expected_match_count refuses a different number, and
        expected_content pins the exact text of one region.
        """

        return self._mutation(
            lambda service: service.replace_text(
                path,
                old,
                new,
                match=match,
                expected_hash=expected_hash,
                expected_content=expected_content,
                expected_match_count=expected_match_count,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_replace_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        replacement: str,
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Replace an inclusive 1-based line range and return operation_id and undo_available.

        Use this for genuinely positional work; file_replace_text is cheaper when
        the line numbers are not already known. Out-of-range references are
        refused, never clamped. The file's newline convention and byte-order mark
        are preserved, and a replacement without a trailing newline does not fuse
        lines. dry_run returns a unified diff preview and writes nothing.
        Otherwise the operation is recorded and undoable.
        """

        return self._mutation(
            lambda service: service.replace_lines(
                path,
                start_line,
                end_line,
                replacement,
                expected_content=expected_content,
                expected_hash=expected_hash,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_insert_lines(
        self,
        path: str,
        content: str,
        line: int | None = None,
        position: str = "after",
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Insert text at a defined position and return operation_id and undo_available.

        line is a 1-based line number. position is before or after that line, or
        end to append at end of file, which takes no line number; there is no
        sentinel line number for end of file. Additive: no existing line is
        replaced. dry_run returns a unified diff preview and writes nothing.
        Otherwise the operation is recorded and undoable.
        """

        return self._mutation(
            lambda service: service.insert_lines(
                path,
                line,
                content,
                position=position,
                expected_content=expected_content,
                expected_hash=expected_hash,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_delete_lines(
        self,
        path: str,
        start_line: int,
        end_line: int,
        expected_content: str | None = None,
        expected_hash: str | None = None,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Delete an inclusive 1-based line range and return operation_id and undo_available.

        Out-of-range references are refused, never clamped. The file's newline
        convention and byte-order mark are preserved. dry_run returns a unified
        diff preview and writes nothing. Otherwise the operation is recorded and
        undoable.
        """

        return self._mutation(
            lambda service: service.delete_lines(
                path,
                start_line,
                end_line,
                expected_content=expected_content,
                expected_hash=expected_hash,
                dry_run=dry_run,
                **self._audit(),
            ),
            path=path,
        )

    def file_apply_patch(
        self,
        patch: str,
        i_accept_secret_write_risk: bool = False,
        dry_run: bool = False,
    ) -> types.CallToolResult:
        """Apply a unified diff and return operation_id and undo_available. Destructive.

        The whole patch is parsed and every hunk is checked against its file
        before a byte is written; a failed or ambiguous hunk rejects the patch
        with PATCH_FAILED and leaves the pre-patch state intact. A patch is
        limited to 1048576 bytes here. A multi-file patch is recorded as one
        logical operation and is undoable, but its application is recoverable,
        not atomic across files. dry_run validates and reports every file and
        hunk without touching the tree. Renaming patches are refused.
        """

        def call(service: FilekeeperService) -> Any:
            _check_size("patch", patch, MAX_PATCH_BYTES)
            return service.apply_patch(
                patch,
                allow_sensitive_write=i_accept_secret_write_risk,
                dry_run=dry_run,
                **self._audit(),
            )

        return self._mutation(
            call,
            path=None,
        )

    # -- history ------------------------------------------------------------
    def history_list(
        self,
        path: str | None = None,
        actor: str | None = None,
        task_id: str | None = None,
        limit: int = 50,
    ) -> types.CallToolResult:
        """List recorded operations in the selected workspace, newest first.

        Read-only. path narrows to operations touching that workspace-relative
        path; actor and task_id are filters, not authority, and never change what
        a mutation records. limit is capped at 200. Returns operation ids usable
        with history_get, history_diff and undo_operation.
        """

        def build(operations: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "operations": jsonable(operations),
                "count": len(operations),
            }

        return self._run(
            lambda service: service.list_history(
                path=path, actor=actor, task_id=task_id, limit=limit
            ),
            path=path,
            build=build,
            guard=lambda: _enforce_max("limit", limit, MAX_HISTORY_ENTRIES),
        )

    def history_get(self, operation_id: str) -> types.CallToolResult:
        """Show one recorded operation: status, actor, paths, before/after hashes and modes.

        Read-only. Unknown identifiers are OPERATION_NOT_FOUND. No file content is
        returned; use history_diff for a before/after diff.
        """

        def build(record: Any) -> dict[str, Any]:
            return {"ok": True, "operation": jsonable(record)}

        return self._run(
            lambda service: service.get_operation(operation_id),
            path=None,
            build=build,
        )

    def history_diff(self, operation_id: str) -> types.CallToolResult:
        """Show the recorded before/after unified diff for one operation.

        Read-only. Returns one entry per affected path with a textual unified
        diff, or is_binary true for content that has no textual diff. Never
        returns whole file bodies.
        """

        def build(diff: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "operation_id": diff.operation_id,
                "files": jsonable(diff.items),
            }

        return self._run(
            lambda service: service.diff_operation(operation_id),
            path=None,
            build=build,
        )

    def undo_operation(self, operation_id: str) -> types.CallToolResult:
        """Undo one recorded operation and return the new operation_id and undo_available.

        Before writing, every affected path is compared with the operation's
        recorded before and after state. A path that matches neither was changed
        afterwards: the whole undo is refused with UNDO_CONFLICT and nothing is
        modified. A recorded move, copy or multi-file patch is undone as one
        operation. Undoing a create removes the file only while it still matches
        what was created. The undo is itself recorded.
        """

        return self._run(
            lambda service: service.undo(
                operation_id, actor=self.actor, task_id=self.task_id
            ),
            path=None,
        )

    def undo_last(self) -> types.CallToolResult:
        """Undo the most recent undoable operation in the selected workspace. Destructive.

        Same conflict rules as undo_operation: any path changed after the
        operation refuses the whole undo with UNDO_CONFLICT and modifies nothing.
        history_skipped entries in the result were above the history size cap and
        cannot be restored (HISTORY_UNAVAILABLE). The undo is itself recorded.
        """

        return self._run(
            lambda service: service.undo_last(actor=self.actor, task_id=self.task_id),
            path=None,
        )

TOOL_BINDINGS: tuple[tuple[str, types.ToolAnnotations], ...] = (
    ("workspace_list", READ_ONLY),
    ("workspace_info", READ_ONLY),
    ("file_read", READ_ONLY),
    ("file_read_many", READ_ONLY),
    ("directory_list", READ_ONLY),
    ("file_info", READ_ONLY),
    ("file_search", READ_ONLY),
    ("content_search", READ_ONLY),
    ("file_create", MUTATING),
    ("file_write", MUTATING),
    ("file_copy", MUTATING),
    ("file_move", MUTATING),
    ("file_delete", MUTATING),
    ("directory_create", ADDITIVE),
    ("directory_delete", MUTATING),
    ("file_replace_text", MUTATING),
    ("file_replace_lines", MUTATING),
    ("file_insert_lines", ADDITIVE),
    ("file_delete_lines", MUTATING),
    ("file_apply_patch", MUTATING),
    ("history_list", READ_ONLY),
    ("history_get", READ_ONLY),
    ("history_diff", READ_ONLY),
    ("undo_operation", MUTATING),
    ("undo_last", MUTATING),
)
"""The tool list published by ``tools/list``: name and protocol annotations."""
