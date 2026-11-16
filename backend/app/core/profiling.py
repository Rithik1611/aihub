"""Development-only CPU profiling (AIHUB_PROFILE=1): yappi measures every thread's CPU time per function, and
the top functions are written to AIHUB_PROFILE_FILE (default aihub_profile.txt) every minute. Off by default:
yappi is a dev dependency and slows the process down."""

import asyncio
import contextlib
import io
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

INTERVAL_S = 60
TOP = 60


def _report(yappi: Any) -> str:
    stats = yappi.get_func_stats()
    out = io.StringIO()
    for title, key in (("own CPU time (tsub)", "tsub"), ("total CPU time incl. callees (ttot)", "ttot")):
        out.write(f"== top {TOP} by {title}\n")
        stats.sort(key, "desc")
        for s in list(stats)[:TOP]:
            out.write(f"{s.tsub:9.3f} {s.ttot:9.3f} {s.ncall:9d}  {s.module}:{s.lineno} {s.name}\n")
        out.write("\n")
    return out.getvalue()


@contextlib.asynccontextmanager
async def maybe_profile() -> AsyncIterator[None]:
    if os.environ.get("AIHUB_PROFILE") != "1":
        yield
        return
    import yappi  # dev dependency, only when asked for

    path = os.environ.get("AIHUB_PROFILE_FILE", "aihub_profile.txt")
    yappi.set_clock_type("cpu")
    yappi.start(builtins=True, profile_threads=True)

    async def dump() -> None:
        while True:
            await asyncio.sleep(INTERVAL_S)
            report = _report(yappi)
            await asyncio.to_thread(Path(path).write_text, report, encoding="utf-8")

    task = asyncio.create_task(dump())
    try:
        yield
    finally:
        task.cancel()
        yappi.stop()
