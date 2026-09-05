"""``screen_grab`` — a PNG of a monitor (or all of them) via ``mss``."""

from __future__ import annotations

import importlib
from typing import Any


class ScreenError(RuntimeError):
    pass


class Screen:
    def __init__(self, *, enabled: bool = True) -> None:
        self.enabled = enabled

    def monitors(self) -> list[dict[str, int]]:
        mss = self._mss()
        with mss.mss() as sct:
            return [dict(m) for m in sct.monitors]

    def grab(self, monitor: int = 0) -> tuple[bytes, dict[str, int]]:
        """PNG bytes + geometry. ``monitor`` 0 = every monitor as one image, 1.. = a single one."""
        if not self.enabled:
            raise ScreenError("screen_grab is disabled on this host (screen.enabled = false in host.toml)")
        mss = self._mss()
        with mss.mss() as sct:
            mons: list[dict[str, int]] = list(sct.monitors)
            if monitor < 0 or monitor >= len(mons):
                raise ScreenError(f"monitor {monitor} does not exist; available: 0 (all) .. {len(mons) - 1}")
            shot: Any = sct.grab(mons[monitor])
            png: bytes = mss.tools.to_png(shot.rgb, shot.size)
            return png, dict(mons[monitor])

    @staticmethod
    def _mss() -> Any:
        try:
            module = importlib.import_module("mss")
            importlib.import_module("mss.tools")
        except ImportError as exc:
            raise ScreenError("the `mss` package is not installed on this host") from exc
        return module
