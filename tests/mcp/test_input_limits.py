"""Oversized requests are refused, not truncated silently.

Each case asks for more than the adapter's published ceiling and expects a
LIMIT_EXCEEDED result with the limit and the actual size in its details.
"""

from __future__ import annotations

from harness import ServerTarget, ToolReply, exercise


def _assert_limit(reply: ToolReply, *, limit: int) -> None:
    assert reply.is_error is True
    assert reply.code == "LIMIT_EXCEEDED"
    assert reply.error["retryable"] is False
    details = reply.error["details"]
    assert details["limit"] == limit
    assert details["actual"] > limit


def test_an_oversized_read_many_is_refused(target: ServerTarget) -> None:
    result = exercise(
        target,
        [("file_read_many", {"paths": [f"file-{index}.txt" for index in range(33)]})],
    )
    _assert_limit(result.by_name("file_read_many"), limit=32)


def test_an_oversized_patch_is_refused(target: ServerTarget) -> None:
    patch = "x" * (1024 * 1024 + 1)
    result = exercise(target, [("file_apply_patch", {"patch": patch})])
    _assert_limit(result.by_name("file_apply_patch"), limit=1024 * 1024)


def test_over_cap_search_requests_are_refused(target: ServerTarget) -> None:
    result = exercise(
        target,
        [
            ("content_search", {"path": ".", "query": "alpha", "max_results": 201}),
            ("file_search", {"path": ".", "glob": "*.txt", "max_results": 201}),
        ],
    )
    _assert_limit(result.by_name("content_search"), limit=200)
    _assert_limit(result.by_name("file_search"), limit=200)


def test_over_cap_listing_and_history_requests_are_refused(target: ServerTarget) -> None:
    result = exercise(
        target,
        [
            ("directory_list", {"path": ".", "max_entries": 1001}),
            ("history_list", {"limit": 201}),
        ],
    )
    _assert_limit(result.by_name("directory_list"), limit=1000)
    _assert_limit(result.by_name("history_list"), limit=200)
