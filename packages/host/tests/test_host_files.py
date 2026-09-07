"""fs_* — the roots are the whole security model."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_host.files import Files, FsError, OutsideRoots


@pytest.fixture
def root(tmp_path: Path) -> Path:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.txt").write_bytes(b"hello\nworld\n")  # bytes: no CRLF translation on Windows
    (tmp_path / "docs" / "b.md").write_text("# title", encoding="utf-8")
    (tmp_path / "docs" / "deep").mkdir()
    (tmp_path / "docs" / "deep" / "c.txt").write_text("deep", encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\x00\x01\x02binary")
    return tmp_path


def test_paths_outside_the_roots_are_refused(root: Path, tmp_path: Path):
    files = Files([root / "docs"])
    with pytest.raises(OutsideRoots):
        files.resolve(str(root / "bin.dat"))
    with pytest.raises(PermissionError):  # OutsideRoots is also a PermissionError
        files.resolve(str(root / "docs" / ".." / "bin.dat"))
    with pytest.raises(OutsideRoots):
        files.list(str(Path.home()))
    assert files.resolve(str(root / "docs" / "deep" / ".." / "a.txt")) == (root / "docs" / "a.txt").resolve()
    assert files.resolve("b.md") == (root / "docs" / "b.md").resolve(), "relative paths land in the first root"


def test_list_read_write_search(root: Path):
    files = Files([root])
    listing = files.list(str(root / "docs"))
    assert [e["name"] for e in listing["entries"]] == ["deep", "a.txt", "b.md"]
    assert listing["entries"][0]["type"] == "dir" and listing["entries"][1]["size"] == 12

    read = files.read(str(root / "docs" / "a.txt"))
    assert read["text"] == "hello\nworld\n" and read["truncated"] is False and read["size"] == 12
    part = files.read(str(root / "docs" / "a.txt"), max_bytes=5)
    assert part["text"] == "hello" and part["truncated"] is True
    rest = files.read(str(root / "docs" / "a.txt"), max_bytes=100, offset=5)
    assert rest["text"] == "\nworld\n" and rest["truncated"] is False
    with pytest.raises(FsError, match="looks binary"):
        files.read(str(root / "bin.dat"))
    with pytest.raises(FsError, match="not a file"):
        files.read(str(root / "docs"))

    written = files.write(str(root / "out" / "new.txt"), "héllo")
    assert written["created"] is True and written["bytes"] == 6
    assert (root / "out" / "new.txt").read_text(encoding="utf-8") == "héllo"
    files.write(str(root / "out" / "new.txt"), "!", append=True)
    assert (root / "out" / "new.txt").read_text(encoding="utf-8") == "héllo!"
    with pytest.raises(OutsideRoots):
        files.write(str(root.parent / "escape.txt"), "x")

    found = files.search(str(root), "*.txt")
    assert sorted(m["relative"] for m in found["matches"]) == sorted(
        [str(Path("docs") / "a.txt"), str(Path("docs") / "deep" / "c.txt"), str(Path("out") / "new.txt")]
    )
    assert found["truncated"] is False
    capped = files.search(str(root), "*.txt", limit=1)
    assert len(capped["matches"]) == 1 and capped["truncated"] is True
    with pytest.raises(FsError, match="not a directory"):
        files.search(str(root / "bin.dat"), "*")


def test_empty_roots_are_rejected():
    with pytest.raises(ValueError):
        Files([])


def test_edit_replaces_in_place_without_resending_the_file(root: Path):
    files = Files([root])
    target = root / "docs" / "deck.html"
    target.write_text("<h1>Население</h1>\n<section id='gdp'>TODO</section>\n", encoding="utf-8")

    out = files.edit(str(target), "<section id='gdp'>TODO</section>", "<section id='gdp'>БВП 2025</section>")
    assert out["replaced"] == 1
    assert target.read_text(encoding="utf-8") == "<h1>Население</h1>\n<section id='gdp'>БВП 2025</section>\n"
    assert out["delta_bytes"] == len("БВП 2025".encode()) - len("TODO")
    # The heading was never re-sent, which is the whole point.
    assert "Население" in target.read_text(encoding="utf-8")


def test_edit_refuses_when_the_count_is_not_what_the_caller_expected(root: Path):
    files = Files([root])
    target = root / "docs" / "twice.txt"
    target.write_text("row\nrow\n", encoding="utf-8")

    with pytest.raises(FsError, match="found 2 times"):
        files.edit(str(target), "row", "col")
    assert target.read_text(encoding="utf-8") == "row\nrow\n", "nothing is written when the count is wrong"

    assert files.edit(str(target), "row", "col", expect=2)["replaced"] == 2
    assert target.read_text(encoding="utf-8") == "col\ncol\n"

    with pytest.raises(FsError, match="not found"):
        files.edit(str(target), "missing", "x")


def test_edit_guards_binaries_missing_files_and_empty_old(root: Path):
    files = Files([root])
    with pytest.raises(FsError, match="looks binary"):
        files.edit(str(root / "bin.dat"), "binary", "text")
    with pytest.raises(FsError, match="is not a file"):
        files.edit(str(root / "docs" / "nope.txt"), "a", "b")
    with pytest.raises(FsError, match="old is empty"):
        files.edit(str(root / "docs" / "a.txt"), "", "b")
    with pytest.raises(OutsideRoots):
        files.edit(str(root.parent / "outside.txt"), "a", "b")
