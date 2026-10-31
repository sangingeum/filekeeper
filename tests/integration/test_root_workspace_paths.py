"""Reported paths when the workspace root is the filesystem root.

With a root of ``/`` the workspace-relative form is indistinguishable from a
repository-relative path, so every reported path keeps its leading separator.
Every other root keeps the relative form byte-for-byte.
"""

from __future__ import annotations

from pathlib import Path

from filekeeper.core.service import Filekeeper


def test_reads_listings_and_searches_report_absolute_paths(home: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    notes = tree / "notes.txt"
    notes.write_text("alpha\nbeta\n", encoding="utf-8")
    sub = tree / "sub"
    sub.mkdir()
    nested = sub / "nested.md"
    nested.write_text("# nested\n", encoding="utf-8")

    service = Filekeeper(home=home).workspace(root="/")

    read = service.read_file(str(notes))
    assert read.path == str(notes.resolve())
    assert read.path.startswith("/")

    many = service.read_multiple_files([str(notes), str(nested)])
    assert [entry.path for entry in many] == [str(notes.resolve()), str(nested.resolve())]
    assert [entry.result.path for entry in many if entry.result is not None] == [
        str(notes.resolve()),
        str(nested.resolve()),
    ]

    info = service.get_file_info(str(notes))
    assert info.path == str(notes.resolve())

    listing = service.list_directory(str(tree))
    assert listing.path == str(tree.resolve())
    assert {entry.path for entry in listing.entries} == {
        str(notes.resolve()),
        str(sub.resolve()),
    }

    found = service.search_files(str(tree), "*.txt")
    assert found.path == str(tree.resolve())
    assert found.paths == (str(notes.resolve()),)

    matches = service.search_content(str(tree), "alpha")
    assert matches.path == str(tree.resolve())
    assert [match.path for match in matches.matches] == [str(notes.resolve())]


def test_mutations_and_history_report_absolute_paths(home: Path, tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    source = tree / "notes.txt"
    source.write_text("alpha\n", encoding="utf-8")
    destination = tree / "copy.txt"

    service = Filekeeper(home=home).workspace(root="/")

    written = service.write_file(str(source), "changed\n")
    assert written.path == str(source.resolve())
    assert [item.path for item in written.items] == [str(source.resolve())]

    copied = service.copy_file(str(source), str(destination))
    assert copied.path == str(destination.resolve())
    assert {item.path for item in copied.items} == {
        str(source.resolve()),
        str(destination.resolve()),
    }

    records = service.list_history()
    assert records[0].target_path == str(destination.resolve())
    assert all(item.relative_path.startswith("/") for item in records[0].items)

    fetched = service.get_operation(copied.operation_id or "")
    assert fetched.target_path == str(destination.resolve())

    undone = service.undo(copied.operation_id or "")
    assert undone.path == str(destination.resolve())
    assert all(item.path.startswith("/") for item in undone.items)


def test_other_roots_keep_the_relative_form(service) -> None:
    read = service.read_file("notes.txt")
    assert read.path == "notes.txt"

    info = service.get_file_info("notes.txt")
    assert info.path == "notes.txt"

    listing = service.list_directory(".")
    assert listing.path == "."
    assert {entry.path for entry in listing.entries} == {"notes.txt", "sub"}

    found = service.search_files(".", "*.txt")
    assert found.path == "."
    assert found.paths == ("notes.txt",)

    written = service.write_file("notes.txt", "changed\n")
    assert written.path == "notes.txt"
    assert [item.path for item in written.items] == ["notes.txt"]
    assert service.list_history()[0].target_path == "notes.txt"
