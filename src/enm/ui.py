from __future__ import annotations

import os
import sys
import threading
from types import TracebackType
from typing import TextIO


UTF8_CLOVER_FRAMES = (".", "·", "+", "✣", "✤", "✣", "+", "·")
LEGACY_CLOVER_FRAMES = (".", "·", "+", "¤", "◆", "¤", "+", "·")
BLUE = "\x1b[34m"
BRIGHT_BLUE = "\x1b[94m"
CYAN = "\x1b[36m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
RED = "\x1b[31m"
RESET = "\x1b[0m"


def color(text: str, code: str, stream: TextIO | None = None) -> str:
    stream = stream or sys.stdout
    if not getattr(stream, "isatty", lambda: False)() or os.environ.get("NO_COLOR") is not None:
        return text
    return f"{code}{text}{RESET}"


def yes_no(value: bool, stream: TextIO | None = None) -> str:
    return color("yes", GREEN, stream) if value else color("no", RED, stream)


def level_label(level: str, stream: TextIO | None = None) -> str:
    code = {"e": RED, "error": RED, "w": YELLOW, "warning": YELLOW, "info": GREEN}.get(
        level.lower()
    )
    return color(level, code, stream) if code else level


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
            frame = color(self.frames[index], BRIGHT_BLUE, self.stream)
            self.stream.write(f"\r{frame} {self.message}")
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
