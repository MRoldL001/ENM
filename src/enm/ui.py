from __future__ import annotations

import os
import sys
import threading
from types import TracebackType
from typing import TextIO


UTF8_CLOVER_FRAMES = (".", "·", "+", "✣", "✤", "✣", "+", "·")
LEGACY_CLOVER_FRAMES = (".", "·", "+", "¤", "◆", "¤", "+", "·")


def clover_frames(encoding: str | None) -> tuple[str, ...]:
    encoding = encoding or "utf-8"
    try:
        "".join(UTF8_CLOVER_FRAMES).encode(encoding)
        return UTF8_CLOVER_FRAMES
    except (LookupError, UnicodeEncodeError):
        return LEGACY_CLOVER_FRAMES


class Spinner:
    def __init__(
        self,
        message: str,
        *,
        stream: TextIO | None = None,
        interval: float = 0.11,
    ) -> None:
        self.message = message
        self.stream = stream or sys.stderr
        self.interval = interval
        self.frames = clover_frames(getattr(self.stream, "encoding", None))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = bool(
            getattr(self.stream, "isatty", lambda: False)()
            and os.environ.get("ENM_NO_SPINNER") not in {"1", "true", "TRUE"}
        )

    def _animate(self) -> None:
        index = 0
        while not self._stop.is_set():
            self.stream.write(f"\r{self.frames[index]} {self.message}")
            self.stream.flush()
            index = (index + 1) % len(self.frames)
            self._stop.wait(self.interval)

    def __enter__(self) -> "Spinner":
        if self.enabled:
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1.0, self.interval * 2))
        self.stream.write("\r" + " " * (len(self.message) + 4) + "\r")
        self.stream.flush()
