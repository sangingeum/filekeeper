"""Unified diff rendering that never concatenates two lines.

``difflib.unified_diff`` renders each input line exactly as it received it, so a
final line without a newline carries no terminator and runs straight into the
next rendered line. That produces output no patch consumer can read.

This module normalises every rendered content line to end with a newline and
states a missing final newline explicitly with the conventional
``\\ No newline at end of file`` marker. Every line of the result, including the
last, ends with ``\\n``.

The same function feeds the human view, the JSON payload and the dry-run
preview, so all three describe a change identically.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

NO_NEWLINE_MARKER = "\\ No newline at end of file"


@dataclass(frozen=True, slots=True)
class DiffLine:
    """One line of text, split from its terminator.

    ``text`` keeps every character except the terminator, including unusual
    separators such as a form feed, so joining ``raw`` values reproduces the
    input exactly.
    """

    text: str
    ending: str

    @property
    def raw(self) -> str:
        return self.text + self.ending


def split_lines(text: str) -> list[DiffLine]:
    """Split text into lines without discarding each line's own terminator."""

    lines: list[DiffLine] = []
    for piece in text.splitlines(keepends=True):
        if piece.endswith("\r\n"):
            lines.append(DiffLine(piece[:-2], "\r\n"))
        elif piece.endswith(("\n", "\r")):
            lines.append(DiffLine(piece[:-1], piece[-1]))
        else:
            lines.append(DiffLine(piece, ""))
    return lines


def join_lines(lines: list[DiffLine]) -> str:
    return "".join(line.raw for line in lines)


def _unterminated_last_line(text: str) -> bool:
    if not text:
        return False
    return not text.endswith(("\n", "\r"))


def unified_diff_text(
    before: str,
    after: str,
    *,
    fromfile: str,
    tofile: str,
) -> str:
    """Render a unified diff in which every line is newline-terminated.

    A side that does not end with a newline gets an explicit marker after the
    rendered line that represents its final line. The marker is emitted once
    when both sides share the same final context line.
    """

    before_lines = split_lines(before)
    after_lines = split_lines(after)
    before_texts = [line.text for line in before_lines]
    after_texts = [line.text for line in after_lines]

    rendered = list(
        difflib.unified_diff(
            [text + "\n" for text in before_texts],
            [text + "\n" for text in after_texts],
            fromfile=fromfile,
            tofile=tofile,
            lineterm="\n",
        )
    )
    if not rendered:
        return ""

    inserts: dict[int, bool] = {}

    if before_lines and not before_lines[-1].ending:
        target = before_texts[-1] + "\n"
        for index in range(len(rendered) - 1, 1, -1):
            if rendered[index][:1] in {"-", " "} and rendered[index][1:] == target:
                inserts[index] = True
                break

    if after_lines and not after_lines[-1].ending:
        target = after_texts[-1] + "\n"
        for index in range(len(rendered) - 1, 1, -1):
            if rendered[index][:1] in {"+", " "} and rendered[index][1:] == target:
                inserts[index] = True
                break

    output: list[str] = []
    marker = NO_NEWLINE_MARKER + "\n"
    for index, line in enumerate(rendered):
        output.append(line)
        if index in inserts:
            output.append(marker)
    return "".join(output)


__all__ = [
    "NO_NEWLINE_MARKER",
    "DiffLine",
    "join_lines",
    "split_lines",
    "unified_diff_text",
]
