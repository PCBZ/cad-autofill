"""A local log of what a run did: one JSON line per step in run_log.jsonl.

Only the step name, the time it took and whether it worked are written, plus small
facts a caller adds (counts, token numbers). Never document text or prompts. The log
stays on this computer (output/ is ignored by git) and feeds the cost and pass-rate
measurements of issue #13.
"""

import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


class RunLog:
    def __init__(self, path: Path):
        self.path = path

    def record(
        self, step: str, seconds: float, ok: bool = True, **facts: str | int | float | bool
    ) -> None:
        entry = {
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "step": step,
            "seconds": round(seconds, 3),
            "ok": ok,
            **facts,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    @contextmanager
    def step(self, name: str) -> Iterator[dict]:
        """Time a step. The body may add facts: `with log.step("x") as facts: facts["n"] = 3`."""
        facts: dict = {}
        start = time.monotonic()
        try:
            yield facts
        except BaseException:
            self.record(name, time.monotonic() - start, ok=False, **facts)
            raise
        self.record(name, time.monotonic() - start, ok=True, **facts)
