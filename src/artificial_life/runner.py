"""Host-side background heartbeat runner.

The runner is deliberately small: durable timers decide *when* work is due,
while ``PersistentLifeAgent.run_due`` performs the canonical write transaction.
It never invents activities or calls an LLM on its own.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .persistent_agent import PersistentHeartbeatResult, PersistentLifeAgent


@dataclass(frozen=True)
class RunnerConfig:
    poll_interval_seconds: float = 1.0

    def __post_init__(self) -> None:
        if self.poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")


class HeartbeatRunner:
    """A stoppable daemon thread for unattended local operation."""

    def __init__(
        self,
        agent: PersistentLifeAgent,
        *,
        config: RunnerConfig | None = None,
        on_results: Callable[[list[PersistentHeartbeatResult]], None] | None = None,
    ) -> None:
        self.agent = agent
        self.config = config or RunnerConfig()
        self.on_results = on_results
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.on_pulse: Callable[[], None] | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="artificial-life-heartbeat", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
        if thread is None or not thread.is_alive():
            self._thread = None

    def run_once(self) -> list[PersistentHeartbeatResult]:
        results = self.agent.run_due()
        self.last_error = None
        if results and self.on_results is not None:
            self.on_results(results)
        if self.on_pulse is not None:
            self.on_pulse()
        return results

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:
                # A transient provider/filesystem error must not kill the
                # unattended runner.  Canonical methods remain responsible
                # for surfacing errors to callers and the next tick retries.
                self.last_error = f"{type(exc).__name__}: {exc}"
            self._stop.wait(self.config.poll_interval_seconds)

