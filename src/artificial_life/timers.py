"""Durable timer registry used by the heartbeat driver."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Timer, TimerStatus, as_utc
from .storage import atomic_write_json, read_json


class TimerStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Timer]:
        data = read_json(self.path, {"schema_version": 1, "timers": []})
        if int(data.get("schema_version", 1)) != 1:
            raise ValueError(f"unsupported timer schema_version={data.get('schema_version')}")
        return {item["id"]: Timer.from_dict(item) for item in data.get("timers", [])}

    def save(self, timers: dict[str, Timer]) -> None:
        atomic_write_json(
            self.path,
            {"schema_version": 1, "timers": [timer.to_dict() for timer in sorted(timers.values(), key=lambda x: x.id)]},
        )

    def ensure_heartbeat(self, due_at: datetime, interval: timedelta) -> Timer:
        timers = self.load()
        current = timers.get("heartbeat")
        if current is None:
            seconds = int(interval.total_seconds())
            if seconds <= 0:
                raise ValueError("heartbeat interval must be positive")
            current = Timer(id="heartbeat", due_at=as_utc(due_at), interval_seconds=seconds)
            timers[current.id] = current
            self.save(timers)
        return current

    def due(self, now: datetime) -> list[Timer]:
        now = as_utc(now)
        return sorted(
            (timer for timer in self.load().values() if timer.status is TimerStatus.ACTIVE and timer.due_at <= now),
            key=lambda timer: (timer.due_at, timer.id),
        )

    def fire(self, timer_id: str, fired_at: datetime) -> Timer:
        timers = self.load()
        timer = timers[timer_id]
        fired_at = as_utc(fired_at)
        timer.last_fired_at = fired_at
        if timer.interval_seconds is None:
            timer.status = TimerStatus.FIRED
        else:
            # Preserve the schedule's cadence even if the process was offline.
            interval = timedelta(seconds=timer.interval_seconds)
            next_due = timer.due_at + interval
            while next_due <= fired_at:
                next_due += interval
            timer.due_at = next_due
        self.save(timers)
        return timer
