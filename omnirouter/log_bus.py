"""In-process log bus.

Captures records from the standard ``logging`` module, keeps a bounded
ring buffer for late-joining clients, and broadcasts each record to any
attached asyncio queues (used by the web UI's WebSocket).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime
from typing import Deque, List, Set


class LogBus:
    def __init__(self, capacity: int = 2000) -> None:
        self._buffer: Deque[dict] = deque(maxlen=capacity)
        self._subscribers: Set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- main-loop wiring -------------------------------------------------
    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach the asyncio loop that owns the WebSocket subscribers."""
        self._loop = loop

    # --- subscriber management -------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    # --- publishing -------------------------------------------------------
    def publish(self, record: dict) -> None:
        with self._lock:
            self._buffer.append(record)
            subscribers = list(self._subscribers)
            loop = self._loop

        if loop is None:
            return

        def _enqueue() -> None:
            for q in subscribers:
                try:
                    q.put_nowait(record)
                except asyncio.QueueFull:
                    # Drop oldest, push newest.
                    try:
                        q.get_nowait()
                        q.put_nowait(record)
                    except Exception:
                        pass

        try:
            loop.call_soon_threadsafe(_enqueue)
        except RuntimeError:
            # Loop is shutting down — ignore.
            pass

    def snapshot(self) -> List[dict]:
        with self._lock:
            return list(self._buffer)

    def clear(self) -> None:
        with self._lock:
            self._buffer.clear()


bus = LogBus()


class BusHandler(logging.Handler):
    """Logging handler that forwards every record into the LogBus."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg = msg + "\n" + logging.Formatter().formatException(record.exc_info)
            bus.publish(
                {
                    "ts": datetime.fromtimestamp(record.created).strftime(
                        "%Y-%m-%d %H:%M:%S,%f"
                    )[:-3],
                    "level": record.levelname,
                    "logger": record.name,
                    "message": msg,
                }
            )
        except Exception:
            self.handleError(record)


def configure_logging(level: int = logging.INFO) -> None:
    """Initialise root + pynetdicom loggers to feed the bus."""
    handler = BusHandler()
    handler.setLevel(level)

    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(level)
    # Avoid duplicate handlers on reload.
    root.handlers = [handler, console]

    # pynetdicom is noisy at DEBUG; keep at INFO so the UI stays readable.
    for name in ("pynetdicom", "pynetdicom.events", "pynetdicom.acse"):
        logging.getLogger(name).setLevel(logging.INFO)
