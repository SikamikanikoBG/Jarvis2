"""A core with a scripted model and a scripted Whisper, for driving the SPA by hand or by Playwright.

    uv run python scripts/dev_fake_core.py [--port 9025]

The model answers every message with the same spoken-style reply, streamed token by token, and
``POST /api/stt`` returns a fixed Bulgarian sentence for any audio at all — so a call can be
walked end to end on a laptop with no vader and no ardi. Web dist is served from web/dist.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "core"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "proto"))

from jarvis_core.app import Core, create_app  # noqa: E402
from jarvis_core.config import CoreConfig  # noqa: E402
from jarvis_core.features.stt import SttResult  # noqa: E402
from jarvis_core.models.fake import FakeAdapter, FakeTurn  # noqa: E402
from jarvis_proto import RoleName  # noqa: E402

REPLY = "Три дни е малко. Има ли сърбеж или парене? Ако има, кажи ми къде точно."
HEARD = "Сърбежът е от три дни, без отделяне."


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9025)
    ns = ap.parse_args()
    home = Path(tempfile.mkdtemp(prefix="jarvis2-fake-"))
    dist = Path(__file__).resolve().parents[1] / "web" / "dist"
    cfg = CoreConfig(home=home, host="127.0.0.1", port=ns.port, token=None, web_dist=dist)
    core = Core(cfg)
    fake = FakeAdapter()
    fake.default_turn = FakeTurn(text=REPLY, token_delay_s=0.02)  # the same answer, every turn, streamed
    core.adapters.fakes = {r: fake for r in RoleName}

    async def transcribe(audio: bytes, *, filename: str, mime: str, language: str | None = None) -> SttResult:
        await asyncio.sleep(0.3)
        return SttResult(text=HEARD, language="bg", backend="fake", duration_ms=300)

    core.transcriber.transcribe = transcribe  # type: ignore[method-assign]
    app = create_app(cfg, core=core)
    print(f"fake core on http://127.0.0.1:{ns.port}  (home {home})", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=ns.port, log_level="warning")


if __name__ == "__main__":
    main()
