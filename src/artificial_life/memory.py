"""Append-only raw dialogue stores and provenance pointers.

Raw dialogue is the factual source for all V1 derived memory.  This module has
no summary or retrieval policy; it only stores and addresses complete records.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .models import SCHEMA_VERSION, as_utc, iso


@dataclass(frozen=True)
class RawPointer:
    stream: str
    start_record: str
    end_record: str
    activity_id: str | None = None
    conversation_id: str | None = None

    def to_dict(self) -> dict[str, str]:
        value = {
            "stream": self.stream,
            "start_record": self.start_record,
            "end_record": self.end_record,
        }
        if self.activity_id is not None:
            value["activity_id"] = self.activity_id
        if self.conversation_id is not None:
            value["conversation_id"] = self.conversation_id
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RawPointer":
        return cls(str(value["stream"]), str(value["start_record"]), str(value["end_record"]),
                   value.get("activity_id"), value.get("conversation_id"))

    def as_text(self) -> str:
        value = f"{self.stream};start_record={self.start_record};end_record={self.end_record}"
        scope = {key: val for key, val in self.to_dict().items() if key.endswith("_id")}
        return value + (";scope=" + json.dumps(scope, ensure_ascii=False) if scope else "")

    @classmethod
    def from_text(cls, value: str) -> "RawPointer | None":
        if not value.strip():
            return None
        parts = [part.strip() for part in value.split(";", 3)]
        if len(parts) not in {3, 4} or "=" not in parts[1] or "=" not in parts[2]:
            raise ValueError(f"invalid raw pointer: {value}")
        scope = json.loads(parts[3].removeprefix("scope=")) if len(parts) == 4 else {}
        return cls(parts[0], parts[1].split("=", 1)[1], parts[2].split("=", 1)[1],
                   scope.get("activity_id"), scope.get("conversation_id"))


@dataclass(frozen=True)
class RawRecord:
    record_id: str
    stream: str
    recorded_at: datetime
    kind: str
    role: str
    text: str
    activity_id: str | None = None
    conversation_id: str | None = None
    timeline_seq: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "recorded_at", as_utc(self.recorded_at))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "record_id": self.record_id,
            "stream": self.stream,
            "recorded_at": iso(self.recorded_at),
            "kind": self.kind,
            "role": self.role,
            "text": self.text,
            "activity_id": self.activity_id,
            "conversation_id": self.conversation_id,
            "timeline_seq": self.timeline_seq,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RawRecord":
        if int(value.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
            raise ValueError("unsupported raw record schema")
        return cls(
            record_id=str(value["record_id"]),
            stream=str(value["stream"]),
            recorded_at=value["recorded_at"],
            kind=str(value.get("kind", "system")),
            role=str(value.get("role", "system")),
            text=str(value.get("text", "")),
            activity_id=value.get("activity_id"),
            conversation_id=value.get("conversation_id"),
            timeline_seq=value.get("timeline_seq"),
            metadata=dict(value.get("metadata", {})),
        )


class RawDialogueStore:
    """One append-only JSONL stream for either agent or user raw history."""

    def __init__(self, root: Path | str, stream: str) -> None:
        if stream not in {"agent", "user"}:
            raise ValueError("raw stream must be 'agent' or 'user'")
        self.stream = stream
        self.path = Path(root) / f"{stream}_raw_dialogue.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: list[RawRecord] | None = None
        self._cached_mtime: int | None = None
        self._cached_size: int | None = None

    def records(self) -> list[RawRecord]:
        if not self.path.exists():
            self._cache = []
            return []
        try:
            stat = self.path.stat()
            if (
                self._cache is not None
                and self._cached_mtime == stat.st_mtime_ns
                and self._cached_size == stat.st_size
            ):
                return list(self._cache)
        except OSError:
            pass
        result: list[RawRecord] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    record = RawRecord.from_dict(json.loads(line))
                except (ValueError, KeyError, json.JSONDecodeError) as exc:
                    raise ValueError(f"invalid {self.stream} raw record at line {line_number}: {exc}") from exc
                if record.stream != self.stream:
                    raise ValueError(f"raw stream mismatch at line {line_number}")
                result.append(record)
        self._cache = result
        try:
            stat = self.path.stat()
            self._cached_mtime = stat.st_mtime_ns
            self._cached_size = stat.st_size
        except OSError:
            self._cached_mtime = None
            self._cached_size = None
        return list(result)

    def append(
        self,
        *,
        recorded_at: datetime,
        kind: str,
        role: str,
        text: str,
        activity_id: str | None = None,
        conversation_id: str | None = None,
        timeline_seq: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RawRecord:
        records = self.records()
        record_id = f"{'ar' if self.stream == 'agent' else 'ur'}_{len(records) + 1:012d}"
        record = RawRecord(
            record_id=record_id,
            stream=self.stream,
            recorded_at=recorded_at,
            kind=kind,
            role=role,
            text=text,
            activity_id=activity_id,
            conversation_id=conversation_id,
            timeline_seq=timeline_seq,
            metadata=dict(metadata or {}),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self._cache is not None:
            self._cache.append(record)
            try:
                stat = self.path.stat()
                self._cached_mtime = stat.st_mtime_ns
                self._cached_size = stat.st_size
            except OSError:
                self._cache = None
        return record

    def append_many(self, records: Iterable[dict[str, Any]]) -> list[RawRecord]:
        return [self.append(**record) for record in records]

    def query(
        self,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        start_record: str | None = None,
        end_record: str | None = None,
        record_id: str | None = None,
    ) -> list[RawRecord]:
        records = self.records()
        if record_id is not None:
            return [record for record in records if record.record_id == record_id]
        if start_record is not None or end_record is not None:
            start_index = next((i for i, record in enumerate(records) if record.record_id == start_record), None) if start_record else 0
            end_index = next((i for i, record in enumerate(records) if record.record_id == end_record), None) if end_record else len(records) - 1
            if start_index is None or end_index is None:
                raise KeyError("raw record range contains an unknown record id")
            records = records[start_index : end_index + 1]
        start_utc = as_utc(start) if start is not None else None
        end_utc = as_utc(end) if end is not None else None
        return [
            record
            for record in records
            if (start_utc is None or record.recorded_at >= start_utc)
            and (end_utc is None or record.recorded_at <= end_utc)
        ]

    def resolve(self, pointer: RawPointer) -> list[RawRecord]:
        if pointer.stream != self.stream:
            raise ValueError(f"pointer stream {pointer.stream} does not match {self.stream}")
        return [record for record in self.query(start_record=pointer.start_record, end_record=pointer.end_record)
                if (pointer.activity_id is None or record.activity_id == pointer.activity_id)
                and (pointer.conversation_id is None or record.conversation_id == pointer.conversation_id)]


class RawMemory:
    def __init__(self, memory_root: Path | str) -> None:
        self.root = Path(memory_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.agent = RawDialogueStore(self.root, "agent")
        self.user = RawDialogueStore(self.root, "user")

    def store(self, stream: str) -> RawDialogueStore:
        if stream == "agent":
            return self.agent
        if stream == "user":
            return self.user
        raise ValueError(f"unknown raw stream: {stream}")
