"""Append-oriented raw timeline (L0) persistence."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Iterator

from .models import SCHEMA_VERSION, as_utc, iso


class TimelineCorruption(RuntimeError):
    """Raised when a canonical timeline cannot be safely replayed."""


@dataclass(frozen=True)
class TimelineEvent:
    id: str
    seq: int
    occurred_at: datetime
    event_type: str
    payload: dict[str, Any]
    heartbeat_id: str | None = None
    causation_id: str | None = None
    checksum: str = ""

    def unsigned_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "id": self.id,
            "seq": self.seq,
            "occurred_at": iso(self.occurred_at),
            "event_type": self.event_type,
            "payload": self.payload,
            "heartbeat_id": self.heartbeat_id,
            "causation_id": self.causation_id,
        }

    def to_dict(self) -> dict[str, Any]:
        value = self.unsigned_dict()
        value["checksum"] = self.checksum
        return value

    @classmethod
    def build(
        cls,
        *,
        seq: int,
        occurred_at: datetime,
        event_type: str,
        payload: dict[str, Any] | None = None,
        heartbeat_id: str | None = None,
        causation_id: str | None = None,
        event_id: str | None = None,
    ) -> "TimelineEvent":
        event = cls(
            id=event_id or f"evt_{seq:012d}",
            seq=seq,
            occurred_at=as_utc(occurred_at),
            event_type=event_type,
            payload=dict(payload or {}),
            heartbeat_id=heartbeat_id,
            causation_id=causation_id,
        )
        return cls(**{**event.__dict__, "checksum": _checksum(event.unsigned_dict())})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TimelineEvent":
        version = int(data.get("schema_version", SCHEMA_VERSION))
        if version != SCHEMA_VERSION:
            raise TimelineCorruption(f"unsupported timeline schema_version={version}")
        event = cls(
            id=str(data["id"]),
            seq=int(data["seq"]),
            occurred_at=as_utc(data["occurred_at"]),
            event_type=str(data["event_type"]),
            payload=dict(data.get("payload", {})),
            heartbeat_id=data.get("heartbeat_id"),
            causation_id=data.get("causation_id"),
            checksum=str(data.get("checksum", "")),
        )
        expected = _checksum(event.unsigned_dict())
        if event.checksum != expected:
            raise TimelineCorruption(f"checksum mismatch for timeline event {event.id}")
        return event


class RawTimeline:
    """A dated JSONL event log with monotonic per-agent sequence numbers.

    There is deliberately no update or delete operation.  A correction in a
    later phase will be represented by another event that references its
    predecessor.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def _files(self) -> list[Path]:
        return sorted(self.root.rglob("*.jsonl"))

    def events(self) -> list[TimelineEvent]:
        events: list[TimelineEvent] = []
        for path in self._files():
            with path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, 1):
                    if not line.strip():
                        continue
                    try:
                        event = TimelineEvent.from_dict(json.loads(line))
                    except (ValueError, KeyError, json.JSONDecodeError) as exc:
                        raise TimelineCorruption(f"invalid event in {path}:{line_number}: {exc}") from exc
                    events.append(event)
        events.sort(key=lambda event: event.seq)
        for expected_seq, event in enumerate(events, 1):
            if event.seq != expected_seq:
                raise TimelineCorruption(f"timeline sequence gap: expected {expected_seq}, got {event.seq}")
        return events

    def scan(self, after_seq: int = 0) -> Iterator[TimelineEvent]:
        yield from (event for event in self.events() if event.seq > after_seq)

    def last_seq(self) -> int:
        all_events = self.events()
        return all_events[-1].seq if all_events else 0

    def append(
        self,
        *,
        occurred_at: datetime,
        event_type: str,
        payload: dict[str, Any] | None = None,
        heartbeat_id: str | None = None,
        causation_id: str | None = None,
        event_id: str | None = None,
    ) -> TimelineEvent:
        return self.append_batch(
            [
                {
                    "occurred_at": occurred_at,
                    "event_type": event_type,
                    "payload": payload or {},
                    "heartbeat_id": heartbeat_id,
                    "causation_id": causation_id,
                    "event_id": event_id,
                }
            ]
        )[0]

    def append_batch(self, specifications: Iterable[dict[str, Any]]) -> list[TimelineEvent]:
        specs = list(specifications)
        if not specs:
            return []
        with self._lock:
            next_seq = self.last_seq() + 1
            events: list[TimelineEvent] = []
            for spec in specs:
                events.append(
                    TimelineEvent.build(
                        seq=next_seq,
                        occurred_at=spec["occurred_at"],
                        event_type=spec["event_type"],
                        payload=spec.get("payload", {}),
                        heartbeat_id=spec.get("heartbeat_id"),
                        causation_id=spec.get("causation_id"),
                        event_id=spec.get("event_id"),
                    )
                )
                next_seq += 1
            handles: dict[Path, Any] = {}
            try:
                for event in events:
                    occurred_at = as_utc(event.occurred_at)
                    path = self.root / f"{occurred_at:%Y}" / f"{occurred_at:%m}" / f"{occurred_at:%Y-%m-%d}.jsonl"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    if path not in handles:
                        handles[path] = path.open("a", encoding="utf-8")
                    handles[path].write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                for handle in handles.values():
                    handle.flush()
                return events
            finally:
                for handle in handles.values():
                    handle.close()


def _checksum(value: dict[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()
