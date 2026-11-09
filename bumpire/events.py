"""Small event stream for observable dependency-update stages."""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, TextIO


@dataclass(slots=True)
class EventLogger:
    stream: TextIO = sys.stderr
    json_lines: bool = False
    started_at: float = field(default_factory=time.monotonic)

    def emit(self, stage: str, message: str, **details: Any) -> None:
        elapsed = round(time.monotonic() - self.started_at, 2)
        if self.json_lines:
            payload = {"stage": stage, "message": message, "elapsed_seconds": elapsed, **details}
            self.stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        else:
            self.stream.write(f"[{elapsed:6.2f}s] {message}\n")
        self.stream.flush()
