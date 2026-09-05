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
