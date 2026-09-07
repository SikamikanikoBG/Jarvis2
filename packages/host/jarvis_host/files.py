"""``fs_*`` tools — files under an allow-list of roots, nothing outside them."""

from __future__ import annotations

import contextlib
import os
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 256_000
MAX_LIST_ENTRIES = 2_000
MAX_SEARCH_RESULTS = 500


class FsError(RuntimeError):
    pass


class OutsideRoots(FsError, PermissionError):
    pass


class Files:
    def __init__(self, roots: Sequence[Path]) -> None:
        if not roots:
            raise ValueError("fs.roots must not be empty")
        self.roots = tuple(Path(r).expanduser().resolve() for r in roots)

    # --- guards --------------------------------------------------------------------------

    def resolve(self, path: str | os.PathLike[str]) -> Path:
        """Absolute, symlink-resolved path that lies under one of the roots — or ``OutsideRoots``."""
        raw = str(path).strip()
        if not raw:
            raise FsError("path is empty")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.roots[0] / p
        resolved = p.resolve()
        for root in self.roots:
            if resolved == root or resolved.is_relative_to(root):
                return resolved
        raise OutsideRoots(f"{raw!r} is outside the allowed roots {[str(r) for r in self.roots]}")

    @staticmethod
    def _entry(p: Path) -> dict[str, Any]:
        entry: dict[str, Any] = {"name": p.name, "path": str(p)}
        try:
            st = p.lstat()
        except OSError as exc:
            entry["type"] = "unknown"
            entry["error"] = exc.strerror or str(exc)
            return entry
        if p.is_symlink():
            entry["type"] = "symlink"
        elif p.is_dir():
            entry["type"] = "dir"
        else:
            entry["type"] = "file"
            entry["size"] = st.st_size
        # OneDrive placeholders / reparse points can carry timestamps fromtimestamp rejects (Errno 22).
        with contextlib.suppress(OSError, OverflowError, ValueError):
            entry["modified"] = datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")
        return entry

    # --- tools ---------------------------------------------------------------------------

    def list(self, path: str, limit: int = MAX_LIST_ENTRIES) -> dict[str, Any]:
        target = self.resolve(path)
        if not target.exists():
            raise FsError(f"{target} does not exist")
        if target.is_file():
            return {"path": str(target), "entries": [self._entry(target)], "truncated": False}
        entries: list[dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(target) as it:
                for de in sorted(it, key=lambda d: (not d.is_dir(follow_symlinks=False), d.name.lower())):
                    if len(entries) >= limit:
                        truncated = True
                        break
                    entries.append(self._entry(Path(de.path)))
        except PermissionError as exc:
            raise FsError(f"cannot list {target}: {exc.strerror or exc}") from exc
        return {"path": str(target), "entries": entries, "truncated": truncated}

    def read(self, path: str, max_bytes: int = MAX_READ_BYTES, offset: int = 0) -> dict[str, Any]:
        target = self.resolve(path)
        if not target.is_file():
            raise FsError(f"{target} is not a file")
        size = target.stat().st_size
        max_bytes = max(1, min(int(max_bytes), MAX_READ_BYTES))
        with target.open("rb") as fh:
            fh.seek(max(0, int(offset)))
            data = fh.read(max_bytes)
        if b"\x00" in data[:8192]:
            raise FsError(f"{target} looks binary ({size} bytes); fs_read returns text only")
        text = data.decode("utf-8-sig", errors="replace")
        return {
            "path": str(target),
            "size": size,
            "offset": int(offset),
            "returned": len(data),
            "truncated": int(offset) + len(data) < size,
            "text": text,
        }

    def write(self, path: str, text: str, append: bool = False) -> dict[str, Any]:
        target = self.resolve(path)
        if target.is_dir():
            raise FsError(f"{target} is a directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        existed = target.exists()
        with target.open("ab" if append else "wb") as fh:
            payload = text.encode("utf-8")
            fh.write(payload)
        return {"path": str(target), "bytes": len(payload), "created": not existed, "appended": append}

    def edit(self, path: str, old: str, new: str, *, expect: int = 1) -> dict[str, Any]:
        """Replace ``old`` with ``new`` in an existing file, without re-sending the whole file.

        A whole-file ``write`` is the only way to change one section of a long document, so
        editing a 23-slide HTML deck meant re-emitting all of it: measured 2026-09-07 on
        "бизнес презентация", 16 writes re-generated 107,765 chars (~33k output tokens) of
        markup that was already on disk. ``expect`` is the number of occurrences the caller
        believes are there; a different count is an error and nothing is written, so a string
        that turns out not to be unique cannot silently rewrite the wrong place.
        """
        target = self.resolve(path)
        if not target.is_file():
            raise FsError(f"{target} is not a file")
        if not old:
            raise FsError("old is empty; use fs_write to create or replace a file")
        raw = target.read_bytes()
        if b"\0" in raw[:8192]:
            raise FsError(f"{target} looks binary; fs_edit works on text only")
        text = raw.decode("utf-8-sig", errors="strict")
        found = text.count(old)
        expect = max(1, int(expect))
        if found != expect:
            hint = "not found" if found == 0 else f"found {found} times"
            raise FsError(
                f"old text {hint} in {target} but expect={expect}; nothing was written. "
                "Read the file and quote a longer, unique snippet."
            )
        updated = text.replace(old, new)
        target.write_bytes(updated.encode("utf-8"))
        return {
            "path": str(target),
            "replaced": found,
            "bytes": len(updated.encode("utf-8")),
            "delta_bytes": len(updated.encode("utf-8")) - len(raw),
        }

    def search(self, root: str, glob: str, limit: int = MAX_SEARCH_RESULTS) -> dict[str, Any]:
        base = self.resolve(root)
        if not base.is_dir():
            raise FsError(f"{base} is not a directory")
        pattern = (glob or "*").strip()
        if not pattern:
            raise FsError("glob is empty")
        limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))
        matches: list[dict[str, Any]] = []
        truncated = False
        for hit in base.rglob(pattern) if not pattern.startswith("**") else base.glob(pattern):
            try:
                resolved = hit.resolve()
            except OSError:
                continue
            if not any(resolved == r or resolved.is_relative_to(r) for r in self.roots):
                continue  # a symlink pointing out of the roots
            if len(matches) >= limit:
                truncated = True
                break
            entry = self._entry(hit)
            entry["relative"] = str(hit.relative_to(base))
            matches.append(entry)
        return {"root": str(base), "glob": pattern, "matches": matches, "truncated": truncated}
