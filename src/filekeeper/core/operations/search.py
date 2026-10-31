"""Path search by glob and content search by literal or regular expression.

Recursion never follows symlinks and never descends into Filekeeper's own state
directory. Every search is capped and reports whether the cap was hit, so a
caller can page rather than believe a truncated list is complete.
"""

from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Iterator
from pathlib import Path

from ..errors import FilekeeperError, InvalidPatternError, ValidationError, storage_failure
from ..models import ContentMatch, ContentSearchResult, PathSearchResult
from ..paths import reported_path, resolve_path
from ..safety.textdata import decode_bytes, looks_binary
from ..workspace import WorkspaceHandle

_BINARY_SAMPLE_SIZE = 8192


def _walk_files(handle: WorkspaceHandle, base: Path) -> Iterator[Path]:
    canonical_state = Path(os.path.realpath(handle.state_dir))
    if base.is_file() and not base.is_symlink():
        yield base
        return
    stack: list[tuple[Path, int]] = [(base, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            children = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            if directory == base:
                # The requested root is unreadable: an empty result would be a
                # silent lie, so the failure is reported instead of skipped.
                raise storage_failure(
                    "list directory", _relative(handle, base) or ".", exc
                ) from exc
            continue
        subdirectories: list[Path] = []
        for child in children:
            if child.is_symlink():
                continue
            try:
                if child.is_dir():
                    if Path(os.path.realpath(child)) == canonical_state:
                        continue
                    if depth + 1 <= handle.config.max_recursion_depth:
                        subdirectories.append(child)
                elif child.is_file():
                    yield child
            except OSError:  # pragma: no cover - races and permission errors
                continue
        for subdirectory in reversed(subdirectories):
            stack.append((subdirectory, depth + 1))


def _relative(handle: WorkspaceHandle, path: Path) -> str:
    return path.relative_to(handle.root).as_posix()


def _matches_glob(relative: str, name: str, pattern: str | None) -> bool:
    if pattern is None:
        return True
    target = relative if "/" in pattern else name
    return fnmatch.fnmatchcase(target, pattern)


def search_files(
    handle: WorkspaceHandle,
    path: str,
    glob: str,
    *,
    max_results: int | None = None,
) -> PathSearchResult:
    """Find paths whose workspace-relative path or file name matches a glob."""

    if not glob:
        raise ValidationError("glob pattern must not be empty")
    limit = handle.config.max_search_results if max_results is None else max_results
    if limit <= 0:
        raise ValidationError("max_results must be positive", details={"max_results": limit})
    limit = min(limit, handle.config.max_search_results)

    resolved = resolve_path(handle.root, path, must_exist=True, state_dir=handle.state_dir)
    matches: list[str] = []
    truncated = False
    for candidate in _walk_files(handle, resolved.absolute):
        if len(matches) >= limit:
            truncated = True
            break
        relative = _relative(handle, candidate)
        if _matches_glob(relative, candidate.name, glob):
            matches.append(reported_path(handle.root, relative))

    return PathSearchResult(
        path=resolved.reported,
        glob=glob,
        paths=tuple(matches),
        truncated=truncated,
    )


def search_content(
    handle: WorkspaceHandle,
    path: str,
    query: str,
    *,
    regex: bool = False,
    glob: str | None = None,
    max_results: int | None = None,
    case_sensitive: bool = True,
) -> ContentSearchResult:
    """Search file contents line by line. Line numbers are 1-based."""

    if query == "":
        raise ValidationError("search query must not be empty")

    limit = handle.config.max_search_results if max_results is None else max_results
    if limit <= 0:
        raise ValidationError("max_results must be positive", details={"max_results": limit})
    limit = min(limit, handle.config.max_search_results)

    pattern: re.Pattern[str] | None = None
    if regex:
        try:
            pattern = re.compile(query, 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise InvalidPatternError(
                f"invalid regular expression: {exc}", details={"query": query}
            ) from exc

    resolved = resolve_path(handle.root, path, must_exist=True, state_dir=handle.state_dir)

    matches: list[ContentMatch] = []
    truncated = False
    files_searched = 0

    for candidate in _walk_files(handle, resolved.absolute):
        if len(matches) >= limit:
            truncated = True
            break
        relative = _relative(handle, candidate)
        if not _matches_glob(relative, candidate.name, glob):
            continue
        try:
            with candidate.open("rb") as file_handle:
                raw = file_handle.read()
        except OSError as exc:
            if candidate == resolved.absolute:
                raise storage_failure("read", relative or ".", exc) from exc
            continue  # nested entries that cannot be read are skipped
        if looks_binary(raw[:_BINARY_SAMPLE_SIZE]):
            continue
        try:
            text, _ = decode_bytes(raw, path=relative)
        except FilekeeperError:
            continue
        files_searched += 1

        for number, line in enumerate(text.splitlines(), start=1):
            if len(matches) >= limit:
                truncated = True
                break
            if pattern is not None:
                found = pattern.search(line)
                if found is None:
                    continue
                column = found.start() + 1
            else:
                haystack = line if case_sensitive else line.lower()
                needle = query if case_sensitive else query.lower()
                index = haystack.find(needle)
                if index < 0:
                    continue
                column = index + 1
            matches.append(
                ContentMatch(
                    path=reported_path(handle.root, relative),
                    line_number=number,
                    line=line,
                    column=column,
                )
            )
        if truncated:
            break

    return ContentSearchResult(
        path=resolved.reported,
        query=query,
        regex=regex,
        matches=tuple(matches),
        truncated=truncated,
        files_searched=files_searched,
    )
