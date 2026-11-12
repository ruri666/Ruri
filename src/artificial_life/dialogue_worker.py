"""Independent dialogue execution channel; never schedules or interrupts activities."""
from __future__ import annotations

import threading

from .host import Interrupted
from .models import iso
from .storage import atomic_write_json, read_json


class DialogueWorker:
    def __init__(self, service, host, retry=60):
        self.service, self.agent, self.host = service, service.agent, host
        self.path = self.agent.root / "state/dialogue-worker.json"
        self.state = read_json(self.path, {"phase": "waiting"})
        self.cancel, self.stop_event, self.notice = threading.Event(), threading.Event(), threading.Event()
        self.execution_lock = threading.Lock()
        self.thread = None
        self.retry = retry

    def status(self):
        return {**self.state, "worker_running": bool(self.thread and self.thread.is_alive())}

    def save(self, **values):
        self.state.update(values)
        atomic_write_json(self.path, self.state)

    def notify(self):
        self.notice.set()

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        with self.agent.lock:
            self._close_unconfirmed_turn()
        self.stop_event.clear(); self.cancel.clear(); self.notice.set()
        self.save(phase="waiting", active_turn=None)
        self.thread = threading.Thread(target=self.run, name="artificial-life-dialogue", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set(); self.cancel.set(); self.notice.set()
        if self.thread:
            self.thread.join(15)

    def _close_unconfirmed_turn(self):
        turn_id = self.state.get("active_turn")
        if getattr(self.host, "native_execution", False) and turn_id:
            # Native tools may have acted before a reply was committed.
            self.service.dialogue_cancel(turn_id)
            return True
        return False

    def tick(self):
        # Prevent duplicate replies if an explicit fixture pulse and the worker
        # happen concurrently. This mutex is independent of autonomous execution.
        if not self.execution_lock.acquire(blocking=False):
            return
        try:
            with self.agent.lock:
                pending = [turn for turn in self.service.state()["pending_turns"]
                           if turn.get("execution_owner", "service") == "service"]
                if not pending or self.stop_event.is_set() or not self.state.get("enabled", True):
                    return
                turn = pending[0]
                self.cancel.clear()
                user = self.agent.raw.user.query(record_id=turn["user_record_id"])[0]
                request = {"kind": "reply", "message": user.text,
                           "context": self.service.context_get("user", turn["conversation_id"])}
                self.save(phase="replying", active_turn=turn["turn_id"], last_error=None)
            from .adaptive_execution import run_adaptive
            result = run_adaptive(self.host, self.service, request, self.cancel,
                                  conversation_id=turn["conversation_id"])
            with self.agent.lock:
                if self.cancel.is_set():
                    raise Interrupted("Dialogue cancelled before commit")
                _, persisted = self.service._read_turn(turn["turn_id"])
                if persisted["status"] != "pending":
                    self.save(phase="waiting", active_turn=None)
                    return  # externally cancelled/committed; never overwrite it
                reply = result.get("reply", "").strip()
                if not reply:
                    raise ValueError("Empty host reply")
                self.service.dialogue_commit(turn["turn_id"], reply)
                # Any old-host steps are suggestions only, never executable plans.
                self.save(phase="waiting", active_turn=None, last_error=None, last_commit_at=iso(self.agent.clock.now()))
            try:
                self.service.structure_get()
            except Exception:
                pass
            with self.agent.lock:
                rem = [t for t in self.service.state()["pending_turns"] if t.get("execution_owner", "service") == "service"]
            if rem:
                self.notify()
        finally:
            self.execution_lock.release()

    def run(self):
        while not self.stop_event.is_set():
            self.notice.wait(1.0); self.notice.clear()
            if self.stop_event.is_set():
                break
            try:
                self.tick()
            except Interrupted:
                with self.agent.lock:
                    self._close_unconfirmed_turn()
                    self.save(phase="waiting" if self.state.get("enabled", True) else "paused", active_turn=None)
            except Exception as exc:
                with self.agent.lock:
                    closed = self._close_unconfirmed_turn()
                    self.save(phase="waiting" if closed else "retrying",
                              active_turn=None, last_error=f"{type(exc).__name__}: {exc}")
                if closed:
                    self.notice.set()
                    continue
                if not self.stop_event.wait(self.retry):
                    self.notice.set()
        with self.agent.lock:
            self.save(phase="stopped")
