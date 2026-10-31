"""Text editing primitives.

Three surfaces are implemented here, in the order the design review gives them:

* :func:`replace_text` — exact-text replacement, the primary edit;
* :func:`replace_lines`, :func:`insert_lines`, :func:`delete_lines` — positional
  editing for genuinely line-shaped work;
* :func:`apply_patch` — unified-diff application across one or more files.

Every application goes through the same journal sequence as a whole-file write:
blobs, pending record, atomic replacement, commit. Undo therefore needs no
separate code path.

Matching in :func:`replace_text` is staged, and the result names the stage that
matched:

1. exact literal match — reported as ``"exact"``;
2. whitespace-normalised match — reported as ``"normalised"`` and never applied
   silently;
3. no match — fails with the nearest candidate line numbers and their text.

Line numbers are 1-based and inclusive. A file's newline convention and
byte-order mark are preserved.
"""

from __future__ import annotations

import dataclasses
import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..diffing import DiffLine, join_lines, split_lines, unified_diff_text
from ..errors import (
    ContentConflictError,
    FilekeeperError,
    MatchCountMismatchError,
    NoMatchError,
    PartialRecoveryError,
    PatchFailedError,
    ValidationError,
    storage_failure,
)
from ..history.journal import OperationJournal, PendingItem
from ..models import (
    MutationResult,
    OperationItemResult,
    PatchFileResult,
    PatchHunkResult,
    as_dry_run,
)
from ..paths import ResolvedPath, ensure_parent_directory, reported_path, resolve_path
from ..safety.atomic import atomic_write_bytes, fsync_directory
from ..safety.hashing import hash_bytes, hashes_match, normalize_hash
from ..safety.textdata import TextProfile, decode_bytes, encode_text
from ..workspace import WorkspaceHandle
from .write import (
    _after_write,
    _check_size,
    _default_file_mode,
    _guard_sensitive,
    _target_mode,
)

MATCH_STAGE_EXACT = "exact"
MATCH_STAGE_NORMALISED = "normalised"
MATCH_STAGE_NONE = "none"
_MATCH_POLICIES = {"first", "all"}
_INSERT_POSITIONS = {"before", "after", "end"}
_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_METADATA_PREFIXES = (
    "diff ",
    "index ",
    "new file mode ",
    "deleted file mode ",
    "similarity index ",
    "rename from ",
    "rename to ",
)


@dataclass(frozen=True, slots=True)
class _Loaded:
    resolved: ResolvedPath
    raw: bytes
    text: str
    profile: TextProfile
    before_hash: str
    before_mode: int
    warnings: tuple[str, ...]


def _load(
    handle: WorkspaceHandle,
    path: str,
    *,
    allow_sensitive_write: bool,
) -> _Loaded:
    resolved = resolve_path(
        handle.root,
        path,
        for_write=True,
        must_exist=True,
        expect="file",
        state_dir=handle.state_dir,
    )
    warnings = _guard_sensitive(handle, resolved, allow_sensitive_write=allow_sensitive_write)
    try:
        raw = resolved.absolute.read_bytes()
    except OSError as exc:
        raise storage_failure("read", resolved.relative, exc) from exc
    text, profile = decode_bytes(raw, path=resolved.relative)
    mode = _target_mode(resolved)
    return _Loaded(
        resolved=resolved,
        raw=raw,
        text=text,
        profile=profile,
        before_hash=hash_bytes(raw),
        before_mode=mode if mode is not None else _default_file_mode(),
        warnings=warnings,
    )


def _guard_hash(loaded: _Loaded, expected_hash: str | None) -> None:
    if expected_hash is None:
        return
    wanted = normalize_hash(expected_hash)
    if not hashes_match(wanted, loaded.before_hash):
        raise ContentConflictError(
            "file content changed since it was read; refusing to edit",
            details={
                "path": loaded.resolved.relative,
                "expected_hash": wanted,
                "actual_hash": loaded.before_hash,
            },
        )


def _guard_region(path: str, matched: str, expected_content: str | None) -> None:
    if expected_content is None:
        return
    if matched != expected_content:
        raise ContentConflictError(
            "the target region does not match expected_content; refusing to edit",
            details={
                "path": path,
                "expected_content": expected_content,
                "actual_content": matched,
            },
        )


def _newline_for(profile: TextProfile) -> str:
    return profile.newline or "\n"


def _leading_whitespace(text: str) -> str:
    return text[: len(text) - len(text.lstrip(" \t"))]


def _replacement_lines(replacement: str, newline: str) -> list[DiffLine]:
    """Turn caller content into lines that use the file's newline convention."""

    normalised = replacement.replace("\r\n", "\n").replace("\r", "\n")
    terminated = normalised.endswith("\n")
    parts = normalised.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    ending = newline or "\n"
    lines = [DiffLine(part, ending) for part in parts]
    if lines and not terminated:
        lines[-1] = DiffLine(lines[-1].text, "")
    return lines


def _replacement_block(replacement: str, indent: str, newline: str) -> list[DiffLine]:
    """Replacement content for a whitespace-normalised match.

    Relative indentation inside the caller's content is kept; the block is
    re-based onto the indentation of the line it replaces.
    """

    normalised = replacement.replace("\r\n", "\n").replace("\r", "\n")
    terminated = normalised.endswith("\n")
    parts = normalised.split("\n")
    if parts and parts[-1] == "":
        parts = parts[:-1]
    if not parts:
        return []

    base = _leading_whitespace(parts[0])
    reindented: list[str] = []
    for part in parts:
        if part.strip() == "":
            reindented.append("")
        elif base and part.startswith(base):
            reindented.append(indent + part[len(base) :])
        else:
            reindented.append(indent + part.lstrip(" \t"))

    ending = newline or "\n"
    lines = [DiffLine(text, ending) for text in reindented]
    if not terminated:
        lines[-1] = DiffLine(lines[-1].text, "")
    return lines


def _candidates(text: str, old: str) -> list[dict[str, Any]]:
    """Nearest lines to the requested text, for a no-match refusal."""

    probe = old.strip()
    lines = split_lines(text)
    scored: list[tuple[float, int, str]] = []
    for index, line in enumerate(lines, start=1):
        ratio = difflib.SequenceMatcher(None, probe, line.text.strip()).ratio()
        scored.append((ratio, index, line.text))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [{"line": number, "text": content} for _, number, content in scored[:3]]


def _finish(
    handle: WorkspaceHandle,
    loaded: _Loaded,
    new_text: str,
    *,
    operation_type: str,
    past_verb: str,
    dry_verb: str,
    match_stage: str | None,
    match_count: int | None,
    dry_run: bool,
    journal: OperationJournal | None,
    actor: str | None,
    task_id: str | None,
) -> MutationResult:
    data = encode_text(new_text, encoding=loaded.profile.encoding, has_bom=loaded.profile.has_bom)
    _check_size(handle, data, loaded.resolved.relative)

    preview: str | None = None
    if dry_run:
        preview = unified_diff_text(
            loaded.text,
            new_text,
            fromfile=f"a/{loaded.resolved.relative}",
            tofile=f"b/{loaded.resolved.relative}",
        )

    result = _after_write(
        handle,
        loaded.resolved,
        data,
        operation_type=operation_type,
        change="modified",
        before_hash=loaded.before_hash,
        before_bytes=loaded.raw,
        before_exists=True,
        before_mode=loaded.before_mode,
        dry_run=dry_run,
        warnings=loaded.warnings,
        past_verb=past_verb,
        dry_verb=dry_verb,
        journal=journal,
        actor=actor,
        task_id=task_id,
    )
    return dataclasses.replace(
        result,
        match_stage=match_stage,
        match_count=match_count,
        preview_diff=preview,
    )


def _exact_regions(text: str, old: str) -> list[tuple[int, int]]:
    regions: list[tuple[int, int]] = []
    start = 0
    while True:
        found = text.find(old, start)
        if found < 0:
            break
        regions.append((found, found + len(old)))
        start = found + len(old)
    return regions


def _normalised_regions(text: str, old: str) -> list[tuple[int, int]]:
    old_parts = old.splitlines()
    if not old_parts:
        return []
    keys = [part.strip() for part in old_parts]
    lines = split_lines(text)
    length = len(keys)
    regions: list[tuple[int, int]] = []
    index = 0
    while index + length <= len(lines):
        if all(lines[index + offset].text.strip() == keys[offset] for offset in range(length)):
            regions.append((index, index + length))
            index += length
        else:
            index += 1
    return regions


def replace_text(
    handle: WorkspaceHandle,
    path: str,
    old: str,
    new: str,
    *,
    match: str,
    expected_hash: str | None = None,
    expected_content: str | None = None,
    expected_match_count: int | None = None,
    allow_sensitive_write: bool = False,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Replace literal text, reporting which matching stage matched.

    ``match`` is required and is either ``"first"`` or ``"all"``. A file that
    does not contain the text fails with ``NO_MATCH`` and the nearest lines;
    ``expected_match_count`` turns a different number of found matches into a
    refusal. ``expected_content`` guards a single matched region and must equal
    its exact text, including indentation.
    """

    if match not in _MATCH_POLICIES:
        raise ValidationError("match policy must be 'first' or 'all'", details={"match": match})
    if old == "":
        raise ValidationError("replacement target must not be empty")
    if expected_match_count is not None and expected_match_count < 0:
        raise ValidationError("expected_match_count must not be negative")

    loaded = _load(handle, path, allow_sensitive_write=allow_sensitive_write)
    _guard_hash(loaded, expected_hash)

    text = loaded.text
    stage = MATCH_STAGE_EXACT
    regions = _exact_regions(text, old)
    if not regions:
        stage = MATCH_STAGE_NORMALISED
        normalised = _normalised_regions(text, old)
        if normalised:
            regions = normalised
        else:
            stage = MATCH_STAGE_NONE

    if not regions:
        raise NoMatchError(
            f"no literal or whitespace-normalised match for the requested text in "
            f"{loaded.resolved.relative}",
            details={
                "path": loaded.resolved.relative,
                "match_stage": stage,
                "candidates": _candidates(text, old),
            },
        )

    found = len(regions)
    if expected_match_count is not None and found != expected_match_count:
        raise MatchCountMismatchError(
            "the requested text matched a different number of times than expected",
            details={
                "path": loaded.resolved.relative,
                "match_stage": stage,
                "expected_match_count": expected_match_count,
                "actual_match_count": found,
            },
        )

    selected = regions[:1] if match == "first" else regions
    lines = split_lines(text)
    blocks: list[str] = []
    for start, end in selected:
        if stage == MATCH_STAGE_EXACT:
            blocks.append(text[start:end])
        else:
            blocks.append(join_lines(lines[start:end]))

    if expected_content is not None:
        if len(selected) != 1:
            raise ContentConflictError(
                "expected_content guards one target region; narrow the match policy",
                details={
                    "path": loaded.resolved.relative,
                    "matched_regions": len(selected),
                },
            )
        _guard_region(loaded.resolved.relative, blocks[0], expected_content)

    if stage == MATCH_STAGE_EXACT:
        new_text = text
        for start, end in reversed(selected):
            new_text = new_text[:start] + new + new_text[end:]
    else:
        newline = _newline_for(loaded.profile)
        replacements: list[tuple[int, int, list[DiffLine]]] = []
        for start, end in selected:
            indent = _leading_whitespace(lines[start].text) if start < len(lines) else ""
            replacements.append((start, end, _replacement_block(new, indent, newline)))
        for start, end, block in reversed(replacements):
            if block and not new.endswith(("\n", "\r")):
                block[-1] = DiffLine(block[-1].text, lines[end - 1].ending)
            lines[start:end] = block
        new_text = join_lines(lines)

    applied = len(selected)
    stage_word = "exact" if stage == MATCH_STAGE_EXACT else "whitespace-normalised"
    plural = "" if applied == 1 else "es"
    verb = f"Replaced {applied} {stage_word} match{plural} in"
    dry_verb = f"Would replace {applied} {stage_word} match{plural} in"

    return _finish(
        handle,
        loaded,
        new_text,
        operation_type="replace_text",
        past_verb=verb,
        dry_verb=dry_verb,
        match_stage=stage,
        match_count=found,
        dry_run=dry_run,
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


def _validate_range(start_line: int, end_line: int, line_count: int, path: str) -> None:
    if start_line < 1 or end_line < 1:
        raise ValidationError(
            "line numbers are 1-based and must be positive",
            details={"path": path, "start_line": start_line, "end_line": end_line},
        )
    if start_line > end_line:
        raise ValidationError(
            "start_line must not be greater than end_line",
            details={"path": path, "start_line": start_line, "end_line": end_line},
        )
    if end_line > line_count:
        raise ValidationError(
            "line range is beyond the end of the file; ranges are never clamped",
            details={
                "path": path,
                "start_line": start_line,
                "end_line": end_line,
                "line_count": line_count,
            },
        )


def replace_lines(
    handle: WorkspaceHandle,
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    *,
    expected_content: str | None = None,
    expected_hash: str | None = None,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Replace an inclusive 1-based line range with ``replacement``."""

    loaded = _load(handle, path, allow_sensitive_write=False)
    _guard_hash(loaded, expected_hash)
    lines = split_lines(loaded.text)
    _validate_range(start_line, end_line, len(lines), loaded.resolved.relative)
    region = join_lines(lines[start_line - 1 : end_line])
    _guard_region(loaded.resolved.relative, region, expected_content)

    newline = _newline_for(loaded.profile)
    replacement_lines = _replacement_lines(replacement, newline)
    if replacement_lines and not replacement.endswith(("\n", "\r")):
        replacement_lines[-1] = DiffLine(replacement_lines[-1].text, lines[end_line - 1].ending)
    new_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    new_text = join_lines(new_lines)

    span = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
    return _finish(
        handle,
        loaded,
        new_text,
        operation_type="replace_lines",
        past_verb=f"Replaced {span} of",
        dry_verb=f"Would replace {span} of",
        match_stage=None,
        match_count=None,
        dry_run=dry_run,
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


def insert_lines(
    handle: WorkspaceHandle,
    path: str,
    line: int | None,
    content: str,
    *,
    position: str = "after",
    expected_content: str | None = None,
    expected_hash: str | None = None,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Insert content before or after a line, or at the end of the file.

    End-of-file insertion is requested with ``position="end"``; ``line`` must be
    omitted there. It is never expressed by overloading a line number.
    """

    if position not in _INSERT_POSITIONS:
        raise ValidationError(
            "position must be 'before', 'after' or 'end'", details={"position": position}
        )
    loaded = _load(handle, path, allow_sensitive_write=False)
    _guard_hash(loaded, expected_hash)
    lines = split_lines(loaded.text)
    count = len(lines)

    if position == "end":
        if line is not None:
            raise ValidationError(
                "position 'end' does not take a line number", details={"line": line}
            )
        index = count
        anchor = lines[-1].raw if lines else ""
        where = "the end of"
    else:
        if line is None:
            raise ValidationError(
                f"position '{position}' requires a line number", details={"position": position}
            )
        if line < 1 or line > count:
            raise ValidationError(
                "line number is beyond the end of the file; numbers are never clamped",
                details={"path": loaded.resolved.relative, "line": line, "line_count": count},
            )
        index = line - 1 if position == "before" else line
        anchor = lines[line - 1].raw
        where = f"{position} line {line} of"

    _guard_region(loaded.resolved.relative, anchor, expected_content)

    newline = _newline_for(loaded.profile)
    if index > 0 and lines[index - 1].ending == "":
        lines[index - 1] = DiffLine(lines[index - 1].text, newline)

    inserted = _replacement_lines(content, newline)
    if index < count and inserted and inserted[-1].ending == "":
        inserted[-1] = DiffLine(inserted[-1].text, newline)

    lines[index:index] = inserted
    new_text = join_lines(lines)

    return _finish(
        handle,
        loaded,
        new_text,
        operation_type="insert_lines",
        past_verb=f"Inserted {len(inserted)} line(s) at {where}",
        dry_verb=f"Would insert {len(inserted)} line(s) at {where}",
        match_stage=None,
        match_count=None,
        dry_run=dry_run,
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


def delete_lines(
    handle: WorkspaceHandle,
    path: str,
    start_line: int,
    end_line: int,
    *,
    expected_content: str | None = None,
    expected_hash: str | None = None,
    dry_run: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Delete an inclusive 1-based line range."""

    loaded = _load(handle, path, allow_sensitive_write=False)
    _guard_hash(loaded, expected_hash)
    lines = split_lines(loaded.text)
    _validate_range(start_line, end_line, len(lines), loaded.resolved.relative)
    region = join_lines(lines[start_line - 1 : end_line])
    _guard_region(loaded.resolved.relative, region, expected_content)

    new_lines = lines[: start_line - 1] + lines[end_line:]
    new_text = join_lines(new_lines)
    span = f"line {start_line}" if start_line == end_line else f"lines {start_line}-{end_line}"
    return _finish(
        handle,
        loaded,
        new_text,
        operation_type="delete_lines",
        past_verb=f"Deleted {span} of",
        dry_verb=f"Would delete {span} of",
        match_stage=None,
        match_count=None,
        dry_run=dry_run,
        journal=journal,
        actor=actor,
        task_id=task_id,
    )


# -- unified diff application ------------------------------------------------


@dataclass(frozen=True, slots=True)
class _BodyLine:
    prefix: str
    text: str
    terminated: bool = True


@dataclass(frozen=True, slots=True)
class _Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    body: tuple[_BodyLine, ...]


@dataclass(frozen=True, slots=True)
class _FilePatch:
    old_path: str
    new_path: str
    hunks: tuple[_Hunk, ...]


@dataclass(frozen=True, slots=True)
class _Plan:
    relative: str
    absolute: Path
    before_exists: bool
    after_exists: bool
    before_bytes: bytes | None
    after_bytes: bytes | None
    before_hash: str | None
    after_hash: str | None
    before_mode: int | None
    after_mode: int | None
    change: Literal["created", "modified", "deleted", "unchanged"]
    hunks: tuple[PatchHunkResult, ...]


def _syntax(message: str, line: int, **counts: int) -> PatchFailedError:
    """A syntax refusal; ``counts`` carries the hunk's expected/actual line counts."""

    details: dict[str, Any] = {"line": line, "reason": "syntax"}
    details.update(counts)
    return PatchFailedError(f"invalid patch: {message}", details=details)


def _parse_patch(patch_text: str) -> tuple[_FilePatch, ...]:
    lines = patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    files: list[_FilePatch] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("--- "):
            old_path = line[4:].strip().split("\t")[0]
            if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
                raise _syntax("a --- header must be followed by a +++ header", index + 1)
            new_path = lines[index + 1][4:].strip().split("\t")[0]
            index += 2
            hunks: list[_Hunk] = []
            while index < len(lines):
                current = lines[index]
                if current.startswith("diff "):
                    break
                if (
                    current.startswith("--- ")
                    and index + 1 < len(lines)
                    and lines[index + 1].startswith("+++ ")
                ):
                    break
                if current.startswith("@@"):
                    match = _HUNK_HEADER.match(current)
                    if match is None:
                        raise _syntax("malformed hunk header", index + 1)
                    old_start = int(match.group(1))
                    old_count = int(match.group(2) or "1")
                    new_start = int(match.group(3))
                    new_count = int(match.group(4) or "1")
                    index += 1
                    body: list[_BodyLine] = []
                    remaining_old = old_count
                    remaining_new = new_count
                    while index < len(lines) and (remaining_old > 0 or remaining_new > 0):
                        body_line = lines[index]
                        prefix = body_line[:1]
                        if prefix == "\\":
                            if not body:
                                raise _syntax("newline marker has no preceding line", index + 1)
                            body[-1] = dataclasses.replace(body[-1], terminated=False)
                            index += 1
                            continue
                        if prefix == " ":
                            body.append(_BodyLine(" ", body_line[1:]))
                            remaining_old -= 1
                            remaining_new -= 1
                        elif prefix == "-":
                            body.append(_BodyLine("-", body_line[1:]))
                            remaining_old -= 1
                        elif prefix == "+":
                            body.append(_BodyLine("+", body_line[1:]))
                            remaining_new -= 1
                        else:
                            found_old = old_count - remaining_old
                            found_new = new_count - remaining_new
                            raise _syntax(
                                "hunk is truncated or has an unknown line prefix "
                                f"(header declares {old_count} old and {new_count} new "
                                f"lines; the body supplies {found_old} old and "
                                f"{found_new} new)",
                                index + 1,
                                expected_old_lines=old_count,
                                expected_new_lines=new_count,
                                actual_old_lines=found_old,
                                actual_new_lines=found_new,
                            )
                        index += 1
                    if remaining_old != 0 or remaining_new != 0:
                        found_old = old_count - remaining_old
                        found_new = new_count - remaining_new
                        raise _syntax(
                            "hunk line counts do not match its header "
                            f"(header declares {old_count} old and {new_count} new "
                            f"lines; the patch supplies {found_old} old and "
                            f"{found_new} new)",
                            index + 1,
                            expected_old_lines=old_count,
                            expected_new_lines=new_count,
                            actual_old_lines=found_old,
                            actual_new_lines=found_new,
                        )
                    if index < len(lines) and lines[index].startswith("\\"):
                        if not body:
                            raise _syntax("newline marker has no preceding line", index + 1)
                        body[-1] = dataclasses.replace(body[-1], terminated=False)
                        index += 1
                    hunks.append(_Hunk(old_start, old_count, new_start, new_count, tuple(body)))
                    continue
                if current.strip() == "" or current.startswith(_METADATA_PREFIXES):
                    index += 1
                    continue
                raise _syntax("unexpected line between hunks", index + 1)
            if not hunks:
                raise _syntax("a file section has no hunks", index + 1)
            files.append(_FilePatch(old_path, new_path, tuple(hunks)))
            continue
        if line.strip() == "" or line.startswith(_METADATA_PREFIXES):
            index += 1
            continue
        raise _syntax("patch must begin with a ---/+++ file header", index + 1)
    if not files:
        raise PatchFailedError(
            "invalid patch: no file sections found", details={"reason": "syntax"}
        )
    return tuple(files)


def _strip_prefix(path: str, side: Literal["a", "b"]) -> str:
    prefix = f"{side}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _hunk_line(line: _BodyLine, newline: str) -> DiffLine:
    ending = (newline or "\n") if line.terminated else ""
    return DiffLine(line.text, ending)


def _block_matches(lines: list[DiffLine], start: int, block: list[_BodyLine]) -> bool:
    if start < 0 or start + len(block) > len(lines):
        return False
    for offset, body_line in enumerate(block):
        if lines[start + offset].text != body_line.text:
            return False
    last = block[-1]
    if not last.terminated and lines[start + len(block) - 1].ending != "":
        return False
    return True


def _apply_hunks(
    lines: list[DiffLine],
    hunks: tuple[_Hunk, ...],
    *,
    newline: str,
    relative: str,
) -> tuple[list[DiffLine], tuple[PatchHunkResult, ...]]:
    working = list(lines)
    results: list[PatchHunkResult] = []
    offset = 0

    for number, hunk in enumerate(hunks, start=1):
        old_block = [line for line in hunk.body if line.prefix in {" ", "-"}]
        new_block = [line for line in hunk.body if line.prefix in {" ", "+"}]

        if hunk.old_count == 0:
            position = hunk.old_start + offset
            if position < 0 or position > len(working):
                raise PatchFailedError(
                    f"hunk {number} inserts beyond the end of {relative}",
                    details={
                        "path": relative,
                        "hunk": number,
                        "reason": "out_of_range",
                    },
                )
        else:
            expected = hunk.old_start - 1 + offset
            matches = [
                start
                for start in range(0, len(working) - len(old_block) + 1)
                if _block_matches(working, start, old_block)
            ]
            if not matches:
                raise PatchFailedError(
                    f"hunk {number} does not match {relative}; the preimage is absent",
                    details={"path": relative, "hunk": number, "reason": "preimage_missing"},
                )
            if expected in matches:
                position = expected
            elif len(matches) == 1:
                position = matches[0]
            else:
                raise PatchFailedError(
                    f"hunk {number} is ambiguous in {relative}: its preimage appears "
                    f"{len(matches)} times and the stated position does not match",
                    details={
                        "path": relative,
                        "hunk": number,
                        "reason": "ambiguous",
                        "positions": [item + 1 for item in matches],
                    },
                )

        working[position : position + len(old_block)] = [
            _hunk_line(line, newline) for line in new_block
        ]
        offset += len(new_block) - len(old_block)
        results.append(
            PatchHunkResult(
                old_start=hunk.old_start,
                old_count=hunk.old_count,
                new_start=hunk.new_start,
                new_count=hunk.new_count,
                status="applied",
            )
        )
    return working, tuple(results)


def _plan_file(
    handle: WorkspaceHandle,
    patch: _FilePatch,
    *,
    allow_sensitive_write: bool,
) -> tuple[_Plan, tuple[str, ...]]:
    create = patch.old_path == "/dev/null"
    delete = patch.new_path == "/dev/null"
    if create:
        relative = _strip_prefix(patch.new_path, "b")
    elif delete:
        relative = _strip_prefix(patch.old_path, "a")
    else:
        old_relative = _strip_prefix(patch.old_path, "a")
        new_relative = _strip_prefix(patch.new_path, "b")
        if old_relative != new_relative:
            raise PatchFailedError(
                "renaming patches are not supported; the old and new paths must match",
                details={"path": old_relative, "reason": "rename"},
            )
        relative = old_relative

    resolved = resolve_path(
        handle.root,
        relative,
        for_write=True,
        state_dir=handle.state_dir,
    )
    warnings = _guard_sensitive(handle, resolved, allow_sensitive_write=allow_sensitive_write)

    newline = ""
    if create:
        if resolved.exists:
            raise PatchFailedError(
                f"cannot create {resolved.relative}: it already exists",
                details={"path": resolved.relative, "reason": "already_exists"},
            )
        ensure_parent_directory(resolved)
        before_bytes: bytes | None = None
        before_hash: str | None = None
        before_mode: int | None = None
        before_text = ""
        profile_encoding = "utf-8"
        has_bom = False
    else:
        if not resolved.exists:
            raise PatchFailedError(
                f"cannot patch {resolved.relative}: the path does not exist",
                details={"path": resolved.relative, "reason": "missing"},
            )
        try:
            before_bytes = resolved.absolute.read_bytes()
        except OSError as exc:
            raise storage_failure("read", resolved.relative, exc) from exc
        before_text, profile = decode_bytes(before_bytes, path=resolved.relative)
        before_hash = hash_bytes(before_bytes)
        before_mode = _target_mode(resolved)
        profile_encoding = profile.encoding
        has_bom = profile.has_bom
        newline = profile.newline

    if create:
        newline = ""

    lines = split_lines(before_text)
    newline_for_file = newline or "\n"
    after_lines, hunk_results = _apply_hunks(
        lines, patch.hunks, newline=newline_for_file, relative=relative
    )
    after_text = join_lines(after_lines)

    if delete and after_text != "":
        raise PatchFailedError(
            f"delete patch does not remove all of {relative}",
            details={"path": relative, "reason": "incomplete_delete"},
        )

    if delete:
        after_bytes: bytes | None = None
        after_hash: str | None = None
        after_exists = False
        change: Literal["created", "modified", "deleted", "unchanged"] = "deleted"
    else:
        after_bytes = encode_text(after_text, encoding=profile_encoding, has_bom=has_bom)
        _check_size(handle, after_bytes, relative)
        after_hash = hash_bytes(after_bytes)
        after_exists = True
        change = "created" if create else "modified"

    plan = _Plan(
        relative=relative,
        absolute=resolved.absolute,
        before_exists=not create,
        after_exists=after_exists,
        before_bytes=before_bytes,
        after_bytes=after_bytes,
        before_hash=before_hash,
        after_hash=after_hash,
        before_mode=before_mode,
        after_mode=before_mode if not create else _default_file_mode(),
        change=change,
        hunks=hunk_results,
    )
    return plan, warnings


def _roll_back(plans: list[_Plan]) -> tuple[list[str], list[str]]:
    restored: list[str] = []
    failed: list[str] = []
    for plan in reversed(plans):
        try:
            if plan.before_exists:
                atomic_write_bytes(
                    plan.absolute,
                    plan.before_bytes or b"",
                    mode=plan.before_mode,
                    fsync=True,
                    display_path=plan.relative,
                )
            else:
                plan.absolute.unlink(missing_ok=True)
                fsync_directory(plan.absolute.parent)
            restored.append(plan.relative)
        except Exception:
            failed.append(plan.relative)
    return restored, failed


def _failure_cause(plan: _Plan | None, exc: BaseException) -> str:
    """Describe a patch failure without naming a resolved host path.

    A raw ``OSError``'s text embeds the absolute path it was given, so only its
    standard reason phrase is used, together with the workspace-relative path of
    the file being changed. Errors the project already builds carry a
    caller-facing message and are passed through unchanged; when that message
    names no file of its own, the relative path is appended so the cause still
    says which file failed.

    This helper must never raise: it runs while unwinding a failed patch, and a
    fault here would mask the failure it is describing.
    """

    relative = plan.relative if plan is not None else None
    if isinstance(exc, FilekeeperError):
        try:
            message = str(exc)
        except Exception:  # pragma: no cover - a faulty __str__ must not mask the failure
            message = type(exc).__name__
        if relative is None or relative in message:
            return message
        return f"{message} ({relative})"

    try:
        reason = getattr(exc, "strerror", None)
    except Exception:  # pragma: no cover - exotic exception objects
        reason = None
    if not isinstance(reason, str) or not reason:
        reason = type(exc).__name__
    if relative is None:
        return reason
    return f"cannot modify {relative}: {reason}"


def apply_patch(
    handle: WorkspaceHandle,
    patch_text: str,
    *,
    dry_run: bool = False,
    allow_sensitive_write: bool = False,
    journal: OperationJournal | None = None,
    actor: str | None = None,
    task_id: str | None = None,
) -> MutationResult:
    """Apply a unified diff after validating every file and hunk.

    No partial patch is applied silently. If a later file fails after earlier
    ones were written, the written files are rolled back to their pre-patch
    bytes and the failure says what was rolled back. A multi-file application
    is **recoverable through history, never atomic across files** — undo
    restores every file as one logical operation.
    """

    if not patch_text.strip():
        raise PatchFailedError("invalid patch: the patch is empty", details={"reason": "syntax"})

    patches = _parse_patch(patch_text)
    seen: set[str] = set()
    plans: list[_Plan] = []
    warnings: list[str] = []
    for patch in patches:
        plan, plan_warnings = _plan_file(handle, patch, allow_sensitive_write=allow_sensitive_write)
        if plan.relative in seen:
            raise PatchFailedError(
                f"the patch names {plan.relative} more than once",
                details={"path": plan.relative, "reason": "duplicate"},
            )
        seen.add(plan.relative)
        plans.append(plan)
        warnings.extend(plan_warnings)

    if dry_run:
        plans = [
            dataclasses.replace(
                plan,
                hunks=tuple(dataclasses.replace(hunk, status="would_apply") for hunk in plan.hunks),
            )
            for plan in plans
        ]

    file_count = len(plans)
    primary = plans[0]
    if file_count > 1:
        warnings.append(
            "a multi-file patch is recoverable through history, not atomic across files"
        )

    file_results = tuple(
        PatchFileResult(
            path=reported_path(handle.root, plan.relative),
            change=plan.change,
            applied=True,
            before_hash=plan.before_hash,
            after_hash=plan.after_hash,
            hunks=plan.hunks,
        )
        for plan in plans
    )
    item_results = tuple(
        OperationItemResult(
            path=reported_path(handle.root, plan.relative),
            change=plan.change,
            before_hash=plan.before_hash,
            after_hash=plan.after_hash,
            size_bytes=len(plan.after_bytes) if plan.after_bytes is not None else None,
        )
        for plan in plans
    )

    if dry_run:
        return as_dry_run(
            MutationResult(
                workspace=handle.name,
                path=reported_path(handle.root, primary.relative),
                changed=True,
                before_hash=primary.before_hash,
                after_hash=primary.after_hash,
                summary=f"Would apply a patch touching {file_count} file(s).",
                dry_run=True,
                warnings=tuple(warnings),
                items=item_results,
                affected_count=file_count,
                preview_diff=patch_text,
                patch_files=file_results,
            ),
            would_change=True,
            predicted_after_hash=primary.after_hash,
        )

    pending = [
        PendingItem(
            relative_path=plan.relative,
            item_type="file",
            before_exists=plan.before_exists,
            after_exists=plan.after_exists,
            before_bytes=plan.before_bytes,
            after_bytes=plan.after_bytes,
            before_hash=plan.before_hash,
            after_hash=plan.after_hash,
            before_mode=plan.before_mode,
            after_mode=plan.after_mode,
        )
        for plan in plans
    ]

    operation_id: str | None = None
    skipped: tuple[str, ...] = ()
    active: list[_Plan] = []
    if journal is not None:
        begun = journal.begin(
            operation_type="apply_patch",
            target_path=primary.relative if file_count == 1 else None,
            summary=f"Applied a patch touching {file_count} file(s).",
            items=pending,
            actor=actor,
            task_id=task_id,
            before_hash=primary.before_hash,
            after_hash=primary.after_hash,
            metadata={"file_count": file_count},
        )
        operation_id = begun.operation_id
        skipped = begun.skipped
        warnings.extend(begun.warnings)

    current: _Plan | None = None
    try:
        for plan in plans:
            active.append(plan)
            current = plan
            if plan.after_exists:
                atomic_write_bytes(
                    plan.absolute,
                    plan.after_bytes or b"",
                    mode=plan.after_mode,
                    fsync=handle.config.fsync,
                    display_path=plan.relative,
                )
            else:
                plan.absolute.unlink()
                fsync_directory(plan.absolute.parent)
    except Exception as exc:
        restored, failed = _roll_back(active)
        if journal is not None and operation_id is not None:
            journal.abort(operation_id)
        if failed:
            raise PartialRecoveryError(
                "patch failed and could not be fully rolled back; these paths were "
                "restored and these were not: " + ", ".join(failed),
                details={
                    "restored": restored,
                    "not_restored": failed,
                    "cause": _failure_cause(current, exc),
                },
            ) from exc
        raise PatchFailedError(
            "patch failed; every affected file was restored to its pre-patch state",
            details={"rolled_back": restored, "cause": _failure_cause(current, exc)},
        ) from exc

    undo_available = False
    if journal is not None and operation_id is not None:
        journal.commit(operation_id)
        undo_available = not skipped

    return MutationResult(
        workspace=handle.name,
        path=reported_path(handle.root, primary.relative),
        changed=True,
        before_hash=primary.before_hash,
        after_hash=primary.after_hash,
        summary=f"Applied a patch touching {file_count} file(s).",
        operation_id=operation_id,
        undo_available=undo_available,
        warnings=tuple(warnings),
        items=item_results,
        affected_count=file_count,
        history_skipped=skipped,
        patch_files=file_results,
    )


__all__ = [
    "MATCH_STAGE_EXACT",
    "MATCH_STAGE_NONE",
    "MATCH_STAGE_NORMALISED",
    "apply_patch",
    "delete_lines",
    "insert_lines",
    "replace_lines",
    "replace_text",
]
