"""The published tool annotations, as a host's directory checker sees them.

A server that advertises tools without the four protocol hints is rejected by
agent directories, so the hints are part of the contract, not decoration. This
module spawns the server, completes the handshake and reads ``tools/list``: the
assertions run against what arrived on the wire, not against the constants that
produced it.

Every tool is pinned in ``EXPECTED_ANNOTATIONS``. Adding a tool, dropping a
hint, or reclassifying one therefore fails here until the table is updated
deliberately.
"""

from __future__ import annotations

from harness import ServerTarget, exercise

from filekeeper_mcp.tools import TOOL_BINDINGS

TOOL_COUNT = 25

# name -> (read_only_hint, destructive_hint, idempotent_hint)
#
# Readers touch nothing. MUTATING tools replace or remove existing state and
# are not idempotent: each call appends a history record. The two additive
# tools create new state without replacing any, which is why they are neither
# read-only nor destructive.
EXPECTED_ANNOTATIONS: dict[str, tuple[bool, bool, bool]] = {
    "workspace_list": (True, False, True),
    "workspace_info": (True, False, True),
    "file_read": (True, False, True),
    "file_read_many": (True, False, True),
    "directory_list": (True, False, True),
    "file_info": (True, False, True),
    "file_search": (True, False, True),
    "content_search": (True, False, True),
    "file_create": (False, True, False),
    "file_write": (False, True, False),
    "file_copy": (False, True, False),
    "file_move": (False, True, False),
    "file_delete": (False, True, False),
    "directory_create": (False, False, False),
    "directory_delete": (False, True, False),
    "file_replace_text": (False, True, False),
    "file_replace_lines": (False, True, False),
    "file_insert_lines": (False, False, False),
    "file_delete_lines": (False, True, False),
    "file_apply_patch": (False, True, False),
    "history_list": (True, False, True),
    "history_get": (True, False, True),
    "history_diff": (True, False, True),
    "undo_operation": (False, True, False),
    "undo_last": (False, True, False),
}

HINT_FIELDS = frozenset(
    {
        "read_only_hint",
        "destructive_hint",
        "idempotent_hint",
        "open_world_hint",
    }
)


def test_every_tool_publishes_four_json_boolean_hints(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    # Pinned so a tool added without an entry in the table is caught here.
    assert len(advertised) == TOOL_COUNT
    assert TOOL_COUNT == len(TOOL_BINDINGS)

    for name, tool in advertised.items():
        annotations = tool.annotations
        assert annotations is not None, f"{name} publishes no annotations"

        # ``model_fields_set`` holds exactly the keys that arrived in the
        # payload; a hint left to a default would be absent here.
        assert annotations.model_fields_set == HINT_FIELDS, name

        for field_name in HINT_FIELDS:
            value = getattr(annotations, field_name)
            assert value is True or value is False, (name, field_name, value)

        # The two hints must never contradict each other.
        if annotations.read_only_hint is True:
            assert annotations.destructive_hint is False, name

        # No tool reaches the network, so the world is closed for all of them.
        assert annotations.open_world_hint is False, name


def test_annotation_values_match_the_handlers(target: ServerTarget) -> None:
    advertised = {tool.name: tool for tool in exercise(target, []).tools}

    assert set(advertised) == set(EXPECTED_ANNOTATIONS)

    for name, tool in advertised.items():
        annotations = tool.annotations
        assert annotations is not None, name
        expected = EXPECTED_ANNOTATIONS[name]
        assert isinstance(expected[0], bool), name
        assert isinstance(expected[1], bool), name
        assert isinstance(expected[2], bool), name
        actual = (
            annotations.read_only_hint,
            annotations.destructive_hint,
            annotations.idempotent_hint,
        )
        assert actual == expected, (name, actual, expected)

    # The three constant groups still partition the table the way the
    # classification reasons: readers, destructive mutators, additive creators.
    read_only = {name for name, hints in EXPECTED_ANNOTATIONS.items() if hints[0] is True}
    additive = {
        name
        for name, hints in EXPECTED_ANNOTATIONS.items()
        if hints[0] is False and hints[1] is False
    }
    destructive = {
        name for name, hints in EXPECTED_ANNOTATIONS.items() if hints[1] is True
    }

    assert additive == {"directory_create", "file_insert_lines"}
    assert not read_only & destructive
    assert read_only | additive | destructive == set(EXPECTED_ANNOTATIONS)
