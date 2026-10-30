"""The ``keeper`` command line.

The CLI parses arguments, calls Core, and renders. It never touches the
filesystem itself: every path decision and every write belongs to Core, so the
CLI and the future MCP server cannot drift apart in behaviour.

Line numbers accepted and printed here are 1-based and inclusive.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ..core.errors import FilekeeperError, ValidationError
from ..core.service import Filekeeper, FilekeeperService
from . import exit_codes, output


def _add_common(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "--workspace",
        metavar="NAME",
        default=default,
        help="workspace name or id (default: the configured default workspace)",
    )
    parser.add_argument(
        "--root",
        metavar="PATH",
        default=default,
        help="operate on this directory as the workspace root",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="emit one machine-readable JSON object",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="suppress non-essential human output",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS if suppress else False,
        help="add detail to human output",
    )
    parser.add_argument(
        "--home",
        metavar="PATH",
        default=default,
        help="Filekeeper home directory (default: FILEKEEPER_HOME or the user data dir)",
    )
    parser.add_argument(
        "--actor",
        metavar="NAME",
        default=default,
        help="identity recorded on a mutation, or used as a filter by history list",
    )
    parser.add_argument(
        "--task-id",
        metavar="ID",
        default=default,
        help="optional external task identifier recorded with a mutation",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    _add_common(common, suppress=True)

    parser = argparse.ArgumentParser(
        prog="keeper",
        description=(
            "Safe, workspace-confined filesystem operations. Line numbers are "
            "1-based and inclusive. Every whole-file write is atomic."
        ),
        epilog=(
            "Exit codes: "
            + "; ".join(f"{code}={meaning}" for code, meaning in exit_codes.EXIT_CODE_DOCUMENTATION)
        ),
    )
    _add_common(parser, suppress=False)
    parser.add_argument("--version", action="version", version=_version_string())

    groups = parser.add_subparsers(dest="group", metavar="COMMAND", required=True)

    init_parser = groups.add_parser(
        "init", parents=[common], help="create the home directory and database"
    )
    init_parser.set_defaults(handler=_handle_init)

    workspace = groups.add_parser(
        "workspace", parents=[common], help="register and select workspaces"
    )
    workspace_sub = workspace.add_subparsers(dest="action", metavar="ACTION", required=True)

    ws_create = workspace_sub.add_parser(
        "create", parents=[common], help="register a directory as a workspace"
    )
    ws_create.add_argument("name")
    ws_create.add_argument("root")
    ws_create.set_defaults(handler=_handle_workspace_create)

    ws_list = workspace_sub.add_parser("list", parents=[common], help="list workspaces")
    ws_list.set_defaults(handler=_handle_workspace_list)

    ws_show = workspace_sub.add_parser("show", parents=[common], help="show one workspace")
    ws_show.add_argument("workspace_ref", metavar="NAME")
    ws_show.set_defaults(handler=_handle_workspace_show)

    ws_use = workspace_sub.add_parser("use", parents=[common], help="make a workspace the default")
    ws_use.add_argument("workspace_ref", metavar="NAME")
    ws_use.set_defaults(handler=_handle_workspace_use)

    ws_remove = workspace_sub.add_parser(
        "remove",
        parents=[common],
        help="remove a registration (never deletes files)",
    )
    ws_remove.add_argument("workspace_ref", metavar="NAME")
    ws_remove.set_defaults(handler=_handle_workspace_remove)

    file_group = groups.add_parser("file", parents=[common], help="file operations")
    file_sub = file_group.add_subparsers(dest="action", metavar="ACTION", required=True)

    read = file_sub.add_parser("read", parents=[common], help="read a file or line range")
    read.add_argument("path")
    read.add_argument("--from-line", type=int, dest="start_line", metavar="N")
    read.add_argument("--to-line", type=int, dest="end_line", metavar="M")
    read.add_argument("--max-bytes", type=int)
    read.add_argument("--encoding")
    read.set_defaults(handler=_handle_read)

    read_many = file_sub.add_parser(
        "read-many", parents=[common], help="read several files in one call"
    )
    read_many.add_argument("paths", nargs="+")
    read_many.add_argument("--max-bytes-per-file", type=int)
    read_many.add_argument("--from-line", type=int, dest="start_line", metavar="N")
    read_many.add_argument("--to-line", type=int, dest="end_line", metavar="M")
    read_many.set_defaults(handler=_handle_read_many)

    info = file_sub.add_parser("info", parents=[common], help="show metadata for a path")
    info.add_argument("path")
    info.set_defaults(handler=_handle_info)

    hash_parser = file_sub.add_parser("hash", parents=[common], help="print a file's SHA-256")
    hash_parser.add_argument("path")
    hash_parser.set_defaults(handler=_handle_hash)

    list_parser = file_sub.add_parser("list", parents=[common], help="list a directory")
    list_parser.add_argument("path", nargs="?", default=".")
    list_parser.add_argument("--recursive", action="store_true")
    list_parser.add_argument("--glob", metavar="PATTERN")
    list_parser.add_argument("--max-entries", type=int)
    list_parser.set_defaults(handler=_handle_list)

    exists_parser = file_sub.add_parser(
        "exists", parents=[common], help="report whether a path exists"
    )
    exists_parser.add_argument("path")
    exists_parser.set_defaults(handler=_handle_exists)

    search = file_sub.add_parser("search", parents=[common], help="find paths matching a glob")
    search.add_argument("path")
    search.add_argument("pattern", metavar="GLOB")
    search.add_argument("--max-results", type=int)
    search.set_defaults(handler=_handle_search_paths)

    grep = file_sub.add_parser(
        "grep", parents=[common], help="search file contents (literal or --regex)"
    )
    grep.add_argument("path")
    grep.add_argument("query")
    grep.add_argument("--regex", action="store_true")
    grep.add_argument("--glob", metavar="PATTERN")
    grep.add_argument("--ignore-case", action="store_true")
    grep.add_argument("--max-results", type=int)
    grep.set_defaults(handler=_handle_grep)

    for name, handler, help_text in (
        ("create", _handle_create, "create a file (fails if it exists)"),
        ("write", _handle_write, "replace a file's content"),
    ):
        mutation = file_sub.add_parser(name, parents=[common], help=help_text)
        mutation.add_argument("path")
        mutation.add_argument("--content", help="content as an argument")
        mutation.add_argument("--input", metavar="FILE", help="read content from this file")
        mutation.add_argument("--encoding")
        mutation.add_argument("--dry-run", action="store_true")
        mutation.add_argument(
            "--i-accept-secret-write-risk",
            action="store_true",
            dest="allow_sensitive_write",
            help="permit writing credential-like paths (.env, id_rsa, *.pem, ...)",
        )
        if name == "create":
            mutation.add_argument(
                "--allow-existing",
                action="store_true",
                help="overwrite an existing file instead of failing",
            )
        else:
            mutation.add_argument("--expected-hash", metavar="HASH")
        mutation.set_defaults(handler=handler)

    copy = file_sub.add_parser("copy", parents=[common], help="copy a file")
    copy.add_argument("source")
    copy.add_argument("destination")
    copy.add_argument("--expected-destination-hash", metavar="HASH")
    copy.add_argument("--dry-run", action="store_true")
    copy.add_argument(
        "--i-accept-secret-write-risk",
        action="store_true",
        dest="allow_sensitive_write",
    )
    copy.set_defaults(handler=_handle_copy)

    move = file_sub.add_parser("move", parents=[common], help="move or rename a file")
    move.add_argument("source")
    move.add_argument("destination")
    move.add_argument("--overwrite", action="store_true")
    move.add_argument("--dry-run", action="store_true")
    move.add_argument(
        "--i-accept-secret-write-risk",
        action="store_true",
        dest="allow_sensitive_write",
    )
    move.set_defaults(handler=_handle_move)

    delete = file_sub.add_parser("delete", parents=[common], help="delete a file")
    delete.add_argument("path")
    delete.add_argument("--expected-hash", metavar="HASH")
    delete.add_argument("--dry-run", action="store_true")
    delete.set_defaults(handler=_handle_delete)

    replace_text_parser = file_sub.add_parser(
        "replace-text",
        parents=[common],
        help="replace literal text, reporting the matching stage",
    )
    replace_text_parser.add_argument("path")
    replace_text_parser.add_argument("--old", required=True, metavar="TEXT")
    replace_text_parser.add_argument("--new", required=True, metavar="TEXT")
    policy = replace_text_parser.add_mutually_exclusive_group(required=True)
    policy.add_argument("--first", action="store_const", const="first", dest="match")
    policy.add_argument("--all", action="store_const", const="all", dest="match")
    replace_text_parser.add_argument("--expected-hash", metavar="HASH")
    replace_text_parser.add_argument("--expected-content", metavar="TEXT")
    replace_text_parser.add_argument("--expected-match-count", type=int, metavar="N")
    replace_text_parser.add_argument("--dry-run", action="store_true")
    replace_text_parser.add_argument(
        "--i-accept-secret-write-risk",
        action="store_true",
        dest="allow_sensitive_write",
    )
    replace_text_parser.set_defaults(handler=_handle_replace_text)

    replace_lines_parser = file_sub.add_parser(
        "replace-lines", parents=[common], help="replace an inclusive 1-based line range"
    )
    replace_lines_parser.add_argument("path")
    replace_lines_parser.add_argument("--start", type=int, required=True, dest="start_line")
    replace_lines_parser.add_argument("--end", type=int, required=True, dest="end_line")
    replace_lines_parser.add_argument("--replacement", required=True, metavar="TEXT")
    replace_lines_parser.add_argument("--expected-content", metavar="TEXT")
    replace_lines_parser.add_argument("--expected-hash", metavar="HASH")
    replace_lines_parser.add_argument("--dry-run", action="store_true")
    replace_lines_parser.set_defaults(handler=_handle_replace_lines)

    insert_lines_parser = file_sub.add_parser(
        "insert-lines", parents=[common], help="insert lines before, after or at the end"
    )
    insert_lines_parser.add_argument("path")
    insert_lines_parser.add_argument("--line", type=int, metavar="N")
    insert_lines_parser.add_argument(
        "--position", choices=["before", "after", "end"], required=True
    )
    insert_lines_parser.add_argument("--content", required=True, metavar="TEXT")
    insert_lines_parser.add_argument("--expected-content", metavar="TEXT")
    insert_lines_parser.add_argument("--expected-hash", metavar="HASH")
    insert_lines_parser.add_argument("--dry-run", action="store_true")
    insert_lines_parser.set_defaults(handler=_handle_insert_lines)

    delete_lines_parser = file_sub.add_parser(
        "delete-lines", parents=[common], help="delete an inclusive 1-based line range"
    )
    delete_lines_parser.add_argument("path")
    delete_lines_parser.add_argument("--start", type=int, required=True, dest="start_line")
    delete_lines_parser.add_argument("--end", type=int, required=True, dest="end_line")
    delete_lines_parser.add_argument("--expected-content", metavar="TEXT")
    delete_lines_parser.add_argument("--expected-hash", metavar="HASH")
    delete_lines_parser.add_argument("--dry-run", action="store_true")
    delete_lines_parser.set_defaults(handler=_handle_delete_lines)

    apply_patch_parser = file_sub.add_parser(
        "apply-patch",
        parents=[common],
        help="apply a unified diff, recoverably across files",
    )
    apply_patch_parser.add_argument("patch_file", nargs="?", metavar="PATCH")
    apply_patch_parser.add_argument("--dry-run", action="store_true")
    apply_patch_parser.add_argument(
        "--i-accept-secret-write-risk",
        action="store_true",
        dest="allow_sensitive_write",
    )
    apply_patch_parser.set_defaults(handler=_handle_apply_patch)

    directory_group = groups.add_parser("dir", parents=[common], help="directory operations")
    directory_sub = directory_group.add_subparsers(dest="action", metavar="ACTION", required=True)

    dir_create = directory_sub.add_parser("create", parents=[common], help="create a directory")
    dir_create.add_argument("path")
    dir_create.add_argument("--parents", action="store_true")
    dir_create.add_argument("--dry-run", action="store_true")
    dir_create.set_defaults(handler=_handle_dir_create)

    dir_move = directory_sub.add_parser("move", parents=[common], help="move a directory")
    dir_move.add_argument("source")
    dir_move.add_argument("destination")
    dir_move.add_argument("--dry-run", action="store_true")
    dir_move.set_defaults(handler=_handle_dir_move)

    dir_delete = directory_sub.add_parser("delete", parents=[common], help="delete a directory")
    dir_delete.add_argument("path")
    dir_delete.add_argument("--recursive", action="store_true")
    dir_delete.add_argument(
        "--confirm-recursive",
        action="store_true",
        help="required together with --recursive: acknowledge that a tree will be deleted",
    )
    dir_delete.add_argument("--dry-run", action="store_true")
    dir_delete.set_defaults(handler=_handle_dir_delete)

    history = groups.add_parser("history", parents=[common], help="operation history and undo")
    history_sub = history.add_subparsers(dest="action", metavar="ACTION", required=True)

    history_list = history_sub.add_parser(
        "list", parents=[common], help="list recorded operations, newest first"
    )
    history_list.add_argument("--path", metavar="PATH", help="only operations touching this path")
    history_list.add_argument("--limit", type=int, default=50, metavar="N")
    history_list.set_defaults(handler=_handle_history_list)

    history_show = history_sub.add_parser(
        "show", parents=[common], help="show one operation and its affected paths"
    )
    history_show.add_argument("operation_id")
    history_show.set_defaults(handler=_handle_history_show)

    history_diff = history_sub.add_parser(
        "diff", parents=[common], help="print the unified diff an operation made"
    )
    history_diff.add_argument("operation_id")
    history_diff.set_defaults(handler=_handle_history_diff)

    history_undo = history_sub.add_parser(
        "undo", parents=[common], help="restore the state before one operation"
    )
    history_undo.add_argument("operation_id")
    history_undo.set_defaults(handler=_handle_history_undo)

    history_undo_last = history_sub.add_parser(
        "undo-last", parents=[common], help="undo the most recent operation in this workspace"
    )
    history_undo_last.set_defaults(handler=_handle_history_undo_last)

    return parser


def _version_string() -> str:
    from .. import __version__

    return f"keeper {__version__}"


class _Context:
    """Resolved options and lazily constructed Core objects."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.as_json: bool = bool(getattr(args, "json", False))
        self.quiet: bool = bool(getattr(args, "quiet", False))
        self.home = getattr(args, "home", None)
        self._app: Filekeeper | None = None
        self._service: FilekeeperService | None = None

    @property
    def app(self) -> Filekeeper:
        if self._app is None:
            self._app = Filekeeper(home=self.home)
        return self._app

    @property
    def service(self) -> FilekeeperService:
        if self._service is None:
            self._service = self.app.workspace(
                name=getattr(self.args, "workspace", None),
                root=getattr(self.args, "root", None),
            )
        return self._service

    def emit(self, payload: dict[str, object], text: str) -> None:
        if self.as_json:
            output.print_json(payload)
        elif text:
            sys.stdout.write(text if text.endswith("\n") else text + "\n")


def _content_from_args(args: argparse.Namespace) -> str:
    content = getattr(args, "content", None)
    if content is not None:
        return str(content)
    input_path = getattr(args, "input", None)
    if input_path is not None:
        try:
            return Path(input_path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(
                f"cannot read content file: {exc}", details={"input": str(input_path)}
            ) from exc
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"content file is not UTF-8 text: {exc}", details={"input": str(input_path)}
            ) from exc
    if not sys.stdin.isatty():
        return sys.stdin.read()
    raise ValidationError("provide --content, --input FILE, or content on standard input")


def _handle_init(context: _Context) -> int:
    home = context.app.init()
    context.emit(
        {"ok": True, "home": str(home)},
        f"initialized Filekeeper home at {home}",
    )
    return exit_codes.SUCCESS


def _handle_workspace_create(context: _Context) -> int:
    workspace = context.app.create_workspace(context.args.name, context.args.root)
    context.emit(
        {"ok": True, "workspace": output.jsonable(workspace)},
        f"registered workspace {workspace.name} at {workspace.root_path} ({workspace.id})\n"
        "state directory .filekeeper/ is created on the first mutation, together with "
        "a .gitignore entry when this directory is a git repository",
    )
    return exit_codes.SUCCESS


def _handle_workspace_list(context: _Context) -> int:
    workspaces = context.app.list_workspaces()
    default_name = context.app.registry.get_default_name()
    lines = [
        output.workspace_summary(workspace, is_default=workspace.name == default_name)
        for workspace in workspaces
    ]
    context.emit(
        {
            "ok": True,
            "default_workspace": default_name,
            "workspaces": output.jsonable(workspaces),
        },
        "\n".join(lines) if lines else "no workspaces registered",
    )
    return exit_codes.SUCCESS


def _handle_workspace_show(context: _Context) -> int:
    workspace = context.app.get_workspace(context.args.workspace_ref)
    is_default = context.app.registry.get_default_name() == workspace.name
    context.emit(
        {
            "ok": True,
            "workspace": output.jsonable(workspace),
            "default": is_default,
            "state_dir": str(context.app.registry.state_dir_for(workspace)),
        },
        output.workspace_detail(workspace, is_default=is_default),
    )
    return exit_codes.SUCCESS


def _handle_workspace_use(context: _Context) -> int:
    workspace = context.app.use_workspace(context.args.workspace_ref)
    context.emit(
        {"ok": True, "default_workspace": workspace.name},
        f"default workspace is now {workspace.name}",
    )
    return exit_codes.SUCCESS


def _handle_workspace_remove(context: _Context) -> int:
    workspace = context.app.remove_workspace(context.args.workspace_ref)
    context.emit(
        {"ok": True, "removed": output.jsonable(workspace)},
        f"removed registration for {workspace.name}; its files were not touched",
    )
    return exit_codes.SUCCESS


def _handle_read(context: _Context) -> int:
    result = context.service.read_file(
        context.args.path,
        start_line=context.args.start_line,
        end_line=context.args.end_line,
        max_bytes=context.args.max_bytes,
        encoding=context.args.encoding,
    )
    if context.as_json:
        output.print_json(output.result_payload(result))
    else:
        sys.stdout.write(result.content)
        if result.truncated and not context.quiet:
            sys.stderr.write(
                f"note: {result.truncation_reason}; resume at line {result.next_start_line}\n"
            )
    return exit_codes.SUCCESS


def _handle_read_many(context: _Context) -> int:
    entries = context.service.read_multiple_files(
        context.args.paths,
        max_bytes_per_file=context.args.max_bytes_per_file,
        start_line=context.args.start_line,
        end_line=context.args.end_line,
    )
    context.emit(
        {"ok": all(entry.ok for entry in entries), "files": output.jsonable(entries)},
        output.read_many(entries),
    )
    return exit_codes.SUCCESS if all(entry.ok for entry in entries) else exit_codes.PATH_NOT_FOUND


def _handle_info(context: _Context) -> int:
    result = context.service.get_file_info(context.args.path)
    context.emit(output.result_payload(result), output.info(result))
    return exit_codes.SUCCESS


def _handle_hash(context: _Context) -> int:
    digest = context.service.get_file_hash(context.args.path)
    context.emit({"ok": True, "path": context.args.path, "content_hash": digest}, digest)
    return exit_codes.SUCCESS


def _handle_list(context: _Context) -> int:
    result = context.service.list_directory(
        context.args.path,
        recursive=context.args.recursive,
        glob=context.args.glob,
        max_entries=context.args.max_entries,
    )
    context.emit(output.result_payload(result), output.listing(result))
    return exit_codes.SUCCESS


def _handle_exists(context: _Context) -> int:
    result = context.service.file_exists(context.args.path)
    context.emit(output.result_payload(result), output.exists(result))
    return exit_codes.SUCCESS if result.exists else exit_codes.PATH_NOT_FOUND


def _handle_search_paths(context: _Context) -> int:
    result = context.service.search_files(
        context.args.path, context.args.pattern, max_results=context.args.max_results
    )
    context.emit(output.result_payload(result), output.path_search(result))
    return exit_codes.SUCCESS


def _handle_grep(context: _Context) -> int:
    result = context.service.search_content(
        context.args.path,
        context.args.query,
        regex=context.args.regex,
        glob=context.args.glob,
        max_results=context.args.max_results,
        case_sensitive=not context.args.ignore_case,
    )
    context.emit(output.result_payload(result), output.content_search(result))
    return exit_codes.SUCCESS


def _emit_mutation(context: _Context, result: object) -> int:
    text = output.mutation(result)  # type: ignore[arg-type]
    context.emit(output.result_payload(result), text)
    return exit_codes.SUCCESS


def _handle_create(context: _Context) -> int:
    result = context.service.create_file(
        context.args.path,
        _content_from_args(context.args),
        expected_absent=not context.args.allow_existing,
        encoding=context.args.encoding or "utf-8",
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_write(context: _Context) -> int:
    result = context.service.write_file(
        context.args.path,
        _content_from_args(context.args),
        expected_hash=context.args.expected_hash,
        encoding=context.args.encoding,
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_copy(context: _Context) -> int:
    result = context.service.copy_file(
        context.args.source,
        context.args.destination,
        expected_destination_hash=context.args.expected_destination_hash,
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_move(context: _Context) -> int:
    result = context.service.move_file(
        context.args.source,
        context.args.destination,
        overwrite=context.args.overwrite,
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_delete(context: _Context) -> int:
    result = context.service.delete_file(
        context.args.path,
        expected_hash=context.args.expected_hash,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_replace_text(context: _Context) -> int:
    result = context.service.replace_text(
        context.args.path,
        context.args.old,
        context.args.new,
        match=context.args.match,
        expected_hash=context.args.expected_hash,
        expected_content=context.args.expected_content,
        expected_match_count=context.args.expected_match_count,
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_replace_lines(context: _Context) -> int:
    result = context.service.replace_lines(
        context.args.path,
        context.args.start_line,
        context.args.end_line,
        context.args.replacement,
        expected_content=context.args.expected_content,
        expected_hash=context.args.expected_hash,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_insert_lines(context: _Context) -> int:
    result = context.service.insert_lines(
        context.args.path,
        context.args.line,
        context.args.content,
        position=context.args.position,
        expected_content=context.args.expected_content,
        expected_hash=context.args.expected_hash,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_delete_lines(context: _Context) -> int:
    result = context.service.delete_lines(
        context.args.path,
        context.args.start_line,
        context.args.end_line,
        expected_content=context.args.expected_content,
        expected_hash=context.args.expected_hash,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_apply_patch(context: _Context) -> int:
    patch_file = context.args.patch_file
    if patch_file is None:
        if sys.stdin.isatty():
            raise ValidationError("provide a patch file or patch content on standard input")
        patch_text = sys.stdin.read()
    else:
        try:
            patch_text = Path(patch_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise ValidationError(
                f"cannot read patch file: {exc}", details={"patch": str(patch_file)}
            ) from exc
        except UnicodeDecodeError as exc:
            raise ValidationError(
                f"patch file is not UTF-8 text: {exc}", details={"patch": str(patch_file)}
            ) from exc
    result = context.service.apply_patch(
        patch_text,
        allow_sensitive_write=context.args.allow_sensitive_write,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_dir_create(context: _Context) -> int:
    result = context.service.create_directory(
        context.args.path,
        parents=context.args.parents,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_dir_move(context: _Context) -> int:
    result = context.service.move_directory(
        context.args.source,
        context.args.destination,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_dir_delete(context: _Context) -> int:
    result = context.service.delete_directory(
        context.args.path,
        recursive=context.args.recursive,
        confirm_recursive=context.args.confirm_recursive,
        dry_run=context.args.dry_run,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_history_list(context: _Context) -> int:
    operations = context.service.list_history(
        path=getattr(context.args, "path", None),
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
        limit=context.args.limit,
    )
    context.emit(
        {
            "ok": True,
            "operations": output.jsonable(operations),
            "count": len(operations),
        },
        output.history_list(operations),
    )
    return exit_codes.SUCCESS


def _handle_history_show(context: _Context) -> int:
    record = context.service.get_operation(context.args.operation_id)
    context.emit(
        {"ok": True, "operation": output.jsonable(record)},
        output.history_show(record),
    )
    return exit_codes.SUCCESS


def _handle_history_diff(context: _Context) -> int:
    diff = context.service.diff_operation(context.args.operation_id)
    context.emit(
        {"ok": True, "operation_id": diff.operation_id, "files": output.jsonable(diff.items)},
        output.history_diff(diff),
    )
    return exit_codes.SUCCESS


def _handle_history_undo(context: _Context) -> int:
    result = context.service.undo(
        context.args.operation_id,
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def _handle_history_undo_last(context: _Context) -> int:
    result = context.service.undo_last(
        actor=getattr(context.args, "actor", None),
        task_id=getattr(context.args, "task_id", None),
    )
    return _emit_mutation(context, result)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, run one command, return a stable exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    context = _Context(args)
    try:
        return int(args.handler(context))
    except FilekeeperError as error:
        output.print_error(error, as_json=context.as_json)
        return exit_codes.exit_code_for(error)
    except BrokenPipeError:  # pragma: no cover - piping into head and friends
        return exit_codes.SUCCESS
    except OSError as error:
        unrecognised = FilekeeperError(f"filesystem error: {error}")
        output.print_error(unrecognised, as_json=context.as_json)
        return exit_codes.GENERAL_ERROR


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
