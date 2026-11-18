"""Durable append-only JSON-lines change log plus an in-process lock.

State is a *definition* (current snapshot) plus an *append-only event log*.
Every accepted write increments the definition version, which is what lets the
report record exactly which bytes it consumed.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .ids import dumps, loads, utcnow


@dataclass(frozen=True)
class Event:
    seq: int
    kind: str
    at: str
    payload: Any


class Journal:
    """One append-only file. Lines are fsynced before the writer releases its lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._events: list[Event] = []
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = loads(line)
                self._events.append(
                    Event(seq=rec["seq"], kind=rec["kind"], at=rec["at"], payload=rec["payload"])
                )

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def append(self, kind: str, payload: Any) -> Event:
        with self._lock:
            seq = (self._events[-1].seq + 1) if self._events else 1
            event = Event(seq=seq, kind=kind, at=utcnow(), payload=payload)
            line = dumps({"seq": seq, "kind": kind, "at": event.at, "payload": payload})
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._events.append(event)
            return event

    def events(self) -> list[Event]:
        with self._lock:
            return list(self._events)

    def replay(self, apply: Callable[[Any, Any], Any], initial: Any = None) -> Any:
        """Fold the log into a projection. ``apply(state, event)`` returns new state."""
        state = initial
        for event in self.events():
            state = apply(state, event)
        return state

    def truncate_after(self, seq: int) -> int:
        """Drop events with seq > ``seq`` (used by interrupted-write recovery)."""
        with self._lock:
            kept = [e for e in self._events if e.seq <= seq]
            dropped = len(self._events) - len(kept)
            if dropped:
                tmp = self.path.with_suffix(".tmp")
                with tmp.open("w", encoding="utf-8") as fh:
                    for e in kept:
                        fh.write(dumps({"seq": e.seq, "kind": e.kind, "at": e.at, "payload": e.payload}) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
                self._events = kept
            return dropped
