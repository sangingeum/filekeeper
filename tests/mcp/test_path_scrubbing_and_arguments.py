"""Caller-facing paths survive scrubbing, and argument mistakes carry a code.

The workspace root can be ``/``. Scrubbing that location would replace every
separator in the payload, so a bare separator is never treated as a location and
a location is only replaced as a whole path token.

Every detail value is scrubbed, not only ``path``. Core renders a state
directory workspace- or home-relative, so the readable value survives while a
resolved host path never appears in the envelope.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest
from harness import ServerTarget, exercise

from filekeeper.core.errors import PathNotFoundError
from filekeeper_mcp.errors import error_payload, error_result, scrub_text


def test_a_root_at_slash_keeps_a_relative_path_intact() -> None:
    error = PathNotFoundError("path does not exist", details={"path": "tmp/nope/missing.txt"})

    payload = error_payload(
        error, host_paths=("/", str(Path.home() / ".local" / "share" / "filekeeper"))
    )

    assert payload["error"]["details"]["path"] == "tmp/nope/missing.txt"
    assert "<host-path>" not in json.dumps(payload)


def test_text_that_legitimately_contains_a_slash_is_untouched() -> None:
    assert scrub_text("a/b stays readable", ()) == "a/b stays readable"
    assert scrub_text("tmp/notes/missing.txt", ("/",)) == "tmp/notes/missing.txt"


def test_a_bare_slash_is_never_scrubbed() -> None:
    assert scrub_text("home/keum/.ssh/id_rsa", ("/",)) == "home/keum/.ssh/id_rsa"


def test_a_genuine_host_prefix_is_still_scrubbed() -> None:
    cleaned = scrub_text(
        "cannot read /srv/project/sub/a.txt: Permission denied", ("/srv/project",)
    )

    assert cleaned == "cannot read <host-path>/sub/a.txt: Permission denied"
    assert "/srv/project" not in cleaned


def test_an_absolute_state_directory_is_scrubbed_in_details() -> None:
    from filekeeper.core.errors import StorageError

    error = StorageError(
        "cannot create state directory: Permission denied",
        details={"state_dir": "/srv/state/ws-transient-abc"},
    )

    payload = error_payload(error, host_paths=("/",))

    assert payload["error"]["details"]["state_dir"] == "<host-path>"
    assert "/srv/state" not in json.dumps(payload)


def test_a_relative_state_directory_stays_readable_in_details() -> None:
    from filekeeper.core.errors import StorageError

    error = StorageError(
        "cannot create state directory: Permission denied",
        details={"state_dir": ".filekeeper", "operation_id": "op-1"},
    )

    payload = error_payload(error, host_paths=("/", "/srv/project"))

    details = payload["error"]["details"]
    assert details["state_dir"] == ".filekeeper"
    assert details["operation_id"] == "op-1"


def test_the_error_text_fallback_is_scrubbed_exactly_like_the_structured_payload() -> None:
    from filekeeper.core.errors import StorageError

    error = StorageError(
        "cannot read /srv/state/ws-transient-abc/notes.txt",
        details={"state_dir": "/srv/state/ws-transient-abc"},
    )

    result = error_result(error, host_paths=("/srv/state",))
    fallback = json.loads(result.content[0].text)  # type: ignore[union-attr]

    assert fallback == result.structured_content
    assert "/srv/state" not in result.content[0].text  # type: ignore[union-attr]
    assert fallback["error"]["details"]["state_dir"] == "<host-path>/ws-transient-abc"


def test_a_wrong_argument_name_is_a_coded_error(target: ServerTarget) -> None:
    result = exercise(
        target, [("file_copy", {"path": "notes.txt", "destination": "copied.txt"})]
    )

    reply = result.by_name("file_copy")
    assert reply.is_error is True
    assert reply.code == "INVALID_ARGUMENT"
    assert "source" in reply.error["message"]
    assert reply.error["details"]["unexpected"] == ["path"]


def test_the_published_schema_refuses_unknown_arguments(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    for tool in advertised.values():
        assert tool.input_schema.get("additionalProperties") is False, tool.name


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write at the filesystem root")
def test_the_protocol_envelope_at_a_filesystem_root_is_readable(tmp_path: Path) -> None:
    target = ServerTarget(root=Path("/"), home=tmp_path / "home")
    probe = uuid.uuid4().hex[:8]
    missing = f"tmp/filekeeper-probe-{probe}/file.txt"
    directory = f"tmp/filekeeper-probe-{probe}-created"
    created_path = Path("/") / directory

    try:
        result = exercise(
            target,
            [
                ("file_read", {"path": missing}),
                ("directory_create", {"path": directory, "parents": True}),
                ("file_copy", {"path": "tmp/x", "destination": "tmp/y"}),
            ],
        )
    finally:
        if created_path.is_dir():
            created_path.rmdir()

    missing_reply = result.by_name("file_read")
    assert missing_reply.code == "PATH_NOT_FOUND"
    assert missing_reply.error["details"]["path"] == missing

    created = result.replies[1]
    assert created.is_error is False
    assert any("global" in warning for warning in created.structured["warnings"])

    argument_reply = result.replies[2]
    assert argument_reply.code == "INVALID_ARGUMENT"
    assert "source" in argument_reply.error["message"]
