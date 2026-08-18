"""Clock ports and deterministic test clock."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from .models import as_utc


class Clock(Protocol):
    def now(self) -> datetime:
        """Return the current aware UTC instant."""


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class TestClock:
    """A manually controlled clock for deterministic tests and simulations."""

    __test__ = False

    def __init__(self, initial: datetime) -> None:
        self._now = as_utc(initial)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> datetime:
        moment = as_utc(moment)
        if moment < self._now:
            raise ValueError("TestClock cannot move backwards")
        self._now = moment
        return self._now

    def advance(self, duration: timedelta) -> datetime:
        if duration.total_seconds() < 0:
            raise ValueError("TestClock cannot move backwards")
        self._now += duration
        return self._now
