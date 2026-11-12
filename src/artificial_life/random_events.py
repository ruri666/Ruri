"""Optional deterministic random-event hook.

The hook is disabled by default.  A hash-derived draw makes the same seed and
heartbeat ID produce the same result after restart without persisting mutable
PRNG state.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True)
class RandomEventSpec:
    kind: str
    name: str
    description: str


DEFAULT_EVENTS = (
    RandomEventSpec("illness", "身体不适", "身体不适，需要休息。"),
    RandomEventSpec("walk", "突然想出门", "突然产生了出门走走的念头。"),
)


class RandomEventEngine:
    def __init__(self, *, seed: int = 0, probability: float = 0.0, events: tuple[RandomEventSpec, ...] = DEFAULT_EVENTS) -> None:
        if not 0.0 <= probability <= 1.0:
            raise ValueError("random event probability must be between 0 and 1")
        if not events:
            raise ValueError("at least one random event is required")
        self.seed = seed
        self.probability = probability
        self.events = events

    def check(self, heartbeat_id: str | None) -> RandomEventSpec | None:
        if heartbeat_id is None or self.probability <= 0.0:
            return None
        digest = hashlib.sha256(f"{self.seed}:{heartbeat_id}".encode("utf-8")).digest()
        draw = int.from_bytes(digest[:8], "big") / float(2**64)
        if draw >= self.probability:
            return None
        index = int.from_bytes(digest[8:16], "big") % len(self.events)
        return self.events[index]

