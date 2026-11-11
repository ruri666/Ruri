"""Transport-independent plugin tools. No UI or MCP SDK dependency."""
from __future__ import annotations

import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .context import retrieval_to_dict
from .dialogue_service import _PreparedTurn
from .llm import LLMResult
from .models import iso
from .persistent_agent import PersistentLifeAgent
from .runner import HeartbeatRunner
from .runtime import RuntimeConfig
from .storage import atomic_write_json


class HarnessProvider:
    """The harness generates prose; maintenance summaries stay deterministic."""
    provider_name = "harness"

    def generate(self, *args, **kwargs):
        raise ValueError("Use dialogue_begin and dialogue_commit with the harness model")

    def plan(self, *args, **kwargs):
        raise ValueError("The harness selects activities through plugin tools")

    def summarize(self, source_text, *, purpose, source_refs, scale="event"):
        text = " ".join(source_text.split())
        return LLMResult(text=text[:1500] or "No source content", provider="harness", purpose=purpose)


def load_agent(root: Path, *, harness: bool = False, heartbeat_interval: timedelta | None = None):
    interval = heartbeat_interval or timedelta(minutes=30)
    provider = HarnessProvider() if harness else None
    if (root / "manifest.json").exists():
        agent = PersistentLifeAgent(root, provider=provider)
        agent.configure_heartbeat_interval(interval)
        return agent
    from .storage import read_json
    profile = read_json(root / "persona.json", {})
    return PersistentLifeAgent.initialize(root, provider=provider, config=RuntimeConfig(
        heartbeat_interval=interval, persona=profile.get("definition", "")))


def activity_dict(activity):
    return {
        "activity_id": activity.activity_id, "name": activity.name, "goal": activity.goal,
        "parent_activity": activity.parent_activity, "status": activity.status,
        "planned_start_time": iso(activity.planned_start_time), "expected_end_time": iso(activity.expected_end_time),
        "actual_start_time": iso(activity.actual_start_time), "actual_end_time": iso(activity.actual_end_time),
        "summary": activity.final_summary or activity.live_summary or activity.summary,
        "children": activity.children, "execution_segments": [segment.to_dict() for segment in activity.execution_segments],
    }


def heartbeat_dict(result):
    return {
        "heartbeat_id": result.heartbeat.heartbeat_id, "no_op": result.heartbeat.no_op,
        "events": [event.to_dict() for event in result.heartbeat.events], "state": result.heartbeat.state.to_dict(),
        "random_event": asdict(result.random_event) if result.random_event else None,
        "interrupted_activity_id": result.interrupted_activity_id, "random_activity_id": result.random_activity_id,
        "expected_end_activity_ids": list(result.expected_end_activity_ids),
        "planning": result.planning.to_dict() if result.planning else None,
    }


def text(value, name="text"):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def conversation(value):
    value = text(value, "conversation_id")
    if not re.fullmatch(r"[\w-]{1,100}", value):
        raise ValueError("conversation_id must contain only letters, digits, underscores or hyphens")
    return value


class PluginService:
    """One command surface used by MCP and HTTP, serialized by the agent lock."""
    def __init__(self, agent: PersistentLifeAgent):
        self.agent = agent
        self.runner = HeartbeatRunner(agent)
        self.lifecycle = None
        self.dialogue_worker = None
        turns = self._turns()
        turns.mkdir(parents=True, exist_ok=True)

    def _native_host(self):
        host = self.lifecycle.host if self.lifecycle else None
        if not getattr(host, "native_execution", False):
            raise ValueError("Start the lifecycle service with --engine native for host-owned execution")
        return host

    def execution_claim(self, worker_id: str, channel: str = "autonomous"):
        return self._native_host().claim(worker_id, channel)

    def execution_complete(self, job_id: str, worker_id: str, result: dict):
        return self._native_host().complete(job_id, worker_id, result)

    def execution_fail(self, job_id: str, worker_id: str, error: str):
        return self._native_host().fail(job_id, worker_id, error)

    def execution_status(self, job_id: str, worker_id: str):
        return self._native_host().inspect(job_id, worker_id)

    def structure_get(self):
        from .observability import export_structures
        return export_structures(self)

    def lifecycle_control(self, action: str):
        if not self.lifecycle:
            raise ValueError("No autonomous lifecycle is attached to this process")
        if action not in {"pause", "resume"}:
            raise ValueError("action must be pause or resume")
        if action == "pause":
            self.lifecycle.interrupt()
        self.lifecycle.save(enabled=action == "resume", phase="paused" if action == "pause" else "waiting",
                            next_wake_at=iso(self.agent.clock.now()))
        self.lifecycle.wake()
        return self.lifecycle.status()

    def lifecycle_mode(self, mode: str):
        if not self.lifecycle:
            raise ValueError("No autonomous lifecycle is attached")
        return self.lifecycle.set_mode(mode)

    def exploration_mode(self, mode: str, conversation_id: str = "default"):
        from .exploration import configure
        return configure(self.agent, mode, conversation_id)

    def exploration_get(self):
        from .exploration import settings
        return settings(self.agent.root)

    def work_list(self, conversation_id: str = "default"):
        from .exploration import recent_results
        conversation_id = conversation(conversation_id)
        return {"results": recent_results(self.agent.root, self.agent.runtime.timeline.events(), conversation_id)}

    def work_file(self, path: str, conversation_id: str = "default"):
        import hashlib
        from .exploration import recent_results
        conversation_id = conversation(conversation_id)
        candidate = (self.agent.root / text(path, "path")).resolve()
        if not candidate.is_relative_to(self.agent.root.resolve() / "works/autonomous"):
            raise ValueError("Work file is outside the committed artifact directory")
        for result in recent_results(self.agent.root, self.agent.runtime.timeline.events(), conversation_id, limit=100000):
            for item in [{"path": result["path"], "sha256": result["sha256"]}] + result["deliverables"]:
                if candidate == (self.agent.root / item["path"]).resolve() and candidate.is_file():
                    content = candidate.read_bytes()
                    if hashlib.sha256(content).hexdigest() != item["sha256"]:
                        raise ValueError("Work file integrity check failed")
                    return {"name": candidate.name, "content": content.decode("utf-8"), "sha256": item["sha256"]}
        raise ValueError("No committed work file for this conversation")

    def dialogue_control(self, action: str):
        if not self.dialogue_worker:
            raise ValueError("No dialogue worker is attached")
        if action not in {"pause", "resume"}:
            raise ValueError("action must be pause or resume")
        if action == "pause":
            self.dialogue_worker.cancel.set()
        self.dialogue_worker.save(enabled=action == "resume", phase="paused" if action == "pause" else "waiting")
        self.dialogue_worker.notify()
        return self.dialogue_worker.status()

    def call(self, name: str, arguments: dict[str, Any] | None = None):
        if not isinstance(name, str) or name not in TOOLS:
            raise ValueError(f"unknown tool: {name}")
        arguments = {} if arguments is None else arguments
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        definition = TOOLS[name]["inputSchema"]
        for key, value in arguments.items():
            rule = definition["properties"].get(key)
            if rule is None:
                raise ValueError(f"unexpected argument: {key}")
            if rule["type"] == "string" and not isinstance(value, str):
                raise ValueError(f"{key} must be a string")
            if rule["type"] == "integer" and (type(value) is not int or not rule.get("minimum", value) <= value <= rule.get("maximum", value)):
                raise ValueError(f"{key} must be an integer within bounds")
        if name == "dialogue_reply":
            import inspect
            try:
                inspect.signature(self.dialogue_reply).bind(**arguments)
            except TypeError as exc:
                raise ValueError(str(exc)) from exc
            result = self.dialogue_reply(**arguments)
            with self.agent.lock:
                self.structure_get()
            return result
        with self.agent.lock:
            # Signature binding rejects extra and missing fields identically
            # for direct callers, HTTP and MCP.
            import inspect
            method = getattr(self, name)
            try:
                inspect.signature(method).bind(**arguments)
            except TypeError as exc:
                raise ValueError(str(exc)) from exc
            result = method(**arguments)
            if name not in {"health", "state", "timeline", "dialogue_history", "context_get", "memory_search", "structure_get", "user_preferences_get",
                            "execution_claim", "execution_complete", "execution_fail", "execution_status"}:
                self.structure_get()
            return result

    def health(self):
        agent = self.agent
        execution = self.lifecycle.status() if self.lifecycle else None
        return {"ok": True, "persona": agent.runtime.config.persona,
                "provider": execution["host"] if execution else agent.provider.provider_name,
                "model": execution.get("execution", {}).get("model") if execution else getattr(agent.provider, "model", None), "root": str(agent.root),
                "logical_time": iso(agent.runtime.state.logical_time), "runner_running": self.runner.running,
                "runner_last_error": self.runner.last_error,
                "lifecycle": execution}

    def state(self):
        agent = self.agent
        return {"exploration": self.exploration_get(), "state": agent.runtime.state.to_dict(), "activities": [activity_dict(a) for a in agent.activities.all()],
                "goals": [goal.to_dict() for goal in agent.runtime.goals.all()],
                "schedule": [block.to_dict() for block in agent.runtime.schedule.all()],
                "pending_turns": [{"turn_id": p.stem, "conversation_id": value["conversation_id"], "user_record_id": value["user_record_id"],
                                   "execution_owner": value.get("execution_owner", "service")}
                    for p in self._turns().glob("*.json") if (value := json.loads(p.read_text(encoding="utf-8"))).get("status") == "pending"],
                "timeline": [event.to_dict() for event in agent.runtime.timeline.events()[-50:]]}

    def timeline(self, limit: int = 50):
        return {"events": [event.to_dict() for event in self.agent.runtime.timeline.events()[-max(1, min(200, limit)):]]}

    def dialogue_history(self, conversation_id: str = "default", limit: int = 100):
        conversation_id = conversation(conversation_id)
        records = [r for r in self.agent.raw.agent.records() + self.agent.raw.user.records() if r.conversation_id == conversation_id]
        records.sort(key=lambda r: (r.recorded_at, 0 if r.role == "user" else 1, r.record_id))
        return {"conversation_id": conversation_id, "records": [r.to_dict() for r in records[-max(1, min(200, limit)):]]}

    def memory_search(self, query: str, memory_type: str = "mixed", conversation_id: str | None = None):
        if memory_type not in {"mixed", "activity", "temporal", "conversation"}:
            raise ValueError("invalid memory_type")
        return retrieval_to_dict(self.agent.retriever.retrieve(text(query), memory_type=memory_type,
                                conversation_id=conversation(conversation_id) if conversation_id is not None else None))

    def context_get(self, kind: str = "self", conversation_id: str = "default", query: str | None = None):
        if kind not in {"self", "user"}:
            raise ValueError("kind must be self or user")
        if kind == "self":
            return self.agent.build_self_context(query=query, now=self.agent.clock.now()).to_dict()
        return self.agent.build_user_context(conversation_id=conversation(conversation_id), query=query, now=self.agent.clock.now()).to_dict()

    def user_preferences(self, entries: list, review: str | None = None):
        from .user_preferences import review_preferences
        return review_preferences(self.agent, entries, review)

    def user_preferences_get(self, query: str = "", offset: int = 0, limit: int = 10):
        from .user_preferences import get_preferences
        return get_preferences(self.agent, query, offset, limit)

    def activity_create(self, activity_id: str, name: str, goal: str = "", parent_activity: str | None = None):
        return activity_dict(self.agent.create_activity(activity_id=text(activity_id), name=text(name), goal=goal, parent_activity=parent_activity))

    def activity_start(self, activity_id: str):
        return activity_dict(self.agent.start_activity(text(activity_id)))

    def activity_interrupt(self, activity_id: str, reason: str):
        return activity_dict(self.agent.interrupt_activity(text(activity_id), text(reason)))

    def activity_complete(self, activity_id: str, result: str):
        result = text(result, "result")
        self.agent.activities.get(activity_id)
        self.agent.record_agent(result, kind="execution_result", activity_id=activity_id)
        # Include the actual result even when a grouping activity never had
        # a start segment. Raw ranges must be scoped, not an unrelated span.
        from .memory import RawPointer
        activity = self.agent.activities.get(activity_id)
        records = [r for r in self.agent.raw.agent.records() if r.activity_id == activity_id]
        activity.raw_pointer = RawPointer("agent", records[0].record_id, records[-1].record_id, activity_id=activity_id)
        self.agent.activities._put(activity)
        return activity_dict(self.agent.complete_activity(activity_id))

    def heartbeat(self, at: str | None = None):
        return heartbeat_dict(self.agent.heartbeat(datetime.fromisoformat(at.replace("Z", "+00:00")) if at else None))

    def _turns(self):
        return self.agent.root / "contexts" / "turns"

    def _require_no_turn(self):
        if any(json.loads(p.read_text(encoding="utf-8")).get("status") == "pending" for p in self._turns().glob("*.json")):
            raise ValueError("Finish or cancel the pending dialogue turn first")

    def dialogue_begin(self, text: str, conversation_id: str = "default", execution_owner: str = "auto"):
        self._require_no_turn()
        if execution_owner not in {"auto", "host", "service"}:
            raise ValueError("execution_owner must be auto, host or service")
        if execution_owner == "auto":
            native = bool(self.lifecycle and getattr(self.lifecycle.host, "native_execution", False))
            execution_owner = "host" if native else "service"
        conversation_id = conversation(conversation_id)
        prepared = self.agent.dialogue._validate(text, conversation_id, None)
        turn = self.agent.dialogue._prepare(*prepared)
        turn_id = uuid4().hex
        atomic_write_json(self._turns() / f"{turn_id}.json", {"status": "pending", "conversation_id": conversation_id,
                          "user_record_id": turn.user.record_id, "checkpoint": turn.checkpoint, "moment": iso(turn.moment),
                          "execution_owner": execution_owner})
        if self.dialogue_worker and execution_owner == "service":
            self.dialogue_worker.notify()
        return {"turn_id": turn_id, "user": turn.user.to_dict(), "context": turn.context.to_dict()}

    def _read_turn(self, turn_id):
        if not re.fullmatch(r"[0-9a-f]{32}", turn_id):
            raise ValueError("invalid turn_id")
        path = self._turns() / f"{turn_id}.json"
        if not path.exists():
            raise ValueError("unknown turn_id")
        return path, json.loads(path.read_text(encoding="utf-8"))

    def dialogue_receive(self, source_id: str, text: str, conversation_id: str, persona_id: str):
        """Idempotent IM ingress; delegates execution exclusively to dialogue."""
        import hashlib
        if persona_id != self.agent.root.name:
            raise ValueError("IM binding persona does not match active workspace")
        conversation_id = conversation(conversation_id)
        self.agent.dialogue._validate(text, conversation_id, None)
        if not isinstance(source_id, str) or not 1 <= len(source_id) <= 512:
            raise ValueError("source_id must contain 1..512 characters")
        key = hashlib.sha256(source_id.encode()).hexdigest()
        path = self.agent.root / "runtime/im-inbox" / (key + ".json")
        fingerprint = hashlib.sha256(json.dumps([text, conversation_id, persona_id], ensure_ascii=False).encode()).hexdigest()
        if path.exists():
            receipt = json.loads(path.read_text(encoding="utf-8"))
            if receipt["fingerprint"] != fingerprint:
                raise ValueError("source_id reused with different message or binding")
            if not receipt.get("turn_id"):
                raise ValueError("IM ingress interrupted; reconcile reserved receipt before retry")
            return self.dialogue_status(receipt["turn_id"], conversation_id, persona_id)
        # Reserve before recording canonical input: an ambiguous crash is never
        # automatically replayed as a second user message.
        self._require_no_turn()
        atomic_write_json(path, {"fingerprint": fingerprint, "status": "reserved"})
        turn = self.dialogue_begin(text, conversation_id, execution_owner="service")
        atomic_write_json(path, {"fingerprint": fingerprint, "turn_id": turn["turn_id"], "status": "accepted"})
        return self.dialogue_status(turn["turn_id"], conversation_id, persona_id)

    def dialogue_status(self, turn_id: str, conversation_id: str, persona_id: str):
        if persona_id != self.agent.root.name:
            raise ValueError("IM binding persona does not match active workspace")
        _, turn = self._read_turn(turn_id)
        if turn["conversation_id"] != conversation(conversation_id):
            raise ValueError("Turn belongs to another conversation")
        result = {"turn_id": turn_id, "status": turn["status"], "conversation_id": conversation_id}
        if turn["status"] == "committed":
            result["reply"] = turn["response"]["agent"]["text"]
        return result

    def dialogue_commit(self, turn_id: str, reply: str):
        reply = text(reply, "reply")
        path, value = self._read_turn(turn_id)
        if value["status"] == "committed":
            if value["response"]["agent"]["text"] != reply:
                raise ValueError("turn already committed with a different reply")
            return value["response"]
        if value["status"] != "pending":
            raise ValueError("turn is no longer pending")
        user = self.agent.raw.user.query(record_id=value["user_record_id"])[0]
        checkpoint = value["checkpoint"]
        turn = _PreparedTurn(self.agent.clock.now(), value["conversation_id"], user,
                             self.agent.build_user_context(conversation_id=value["conversation_id"]), checkpoint,
                             checkpoint["activity_id"] if checkpoint else None,
                             checkpoint["checkpoint_id"] if checkpoint else None)
        provider = self.lifecycle.host.name if self.lifecycle else "harness"
        response = self.agent.dialogue._finish(turn, reply, provider, None).to_dict()
        atomic_write_json(path, {**value, "status": "committed", "response": response})
        return response

    def dialogue_cancel(self, turn_id: str):
        path, value = self._read_turn(turn_id)
        if value["status"] == "pending":
            self.agent._resume_dialogue_checkpoint(value["checkpoint"], self.agent.clock.now())
            self.agent._append_dialogue_fact("dialogue.cancelled", self.agent.clock.now(), {"turn_id": turn_id})
            value["status"] = "cancelled"
            atomic_write_json(path, value)
        return {"status": value["status"]}

    def dialogue_reply(self, text: str, conversation_id: str = "default"):
        if self.agent.provider.provider_name == "harness":
            import time
            turn = self.call("dialogue_begin", {"text": text, "conversation_id": conversation(conversation_id), "execution_owner": "service"})
            turn_id = turn["turn_id"]
            worker_alive = bool(getattr(self, "dialogue_worker", None) and getattr(self.dialogue_worker, "thread", None) and self.dialogue_worker.thread.is_alive())
            if not worker_alive:
                return {"conversation_id": conversation(conversation_id), "pending": True, "turn_id": turn_id}
            self.dialogue_worker.notify()
            deadline = time.monotonic() + 30.0
            while time.monotonic() < deadline:
                path, current = self._read_turn(turn_id)
                if current.get("status") == "committed":
                    return {"conversation_id": conversation(conversation_id), **current.get("response", {})}
                if current.get("status") == "cancelled":
                    raise ValueError("Dialogue turn was cancelled")
                time.sleep(0.08)
            return {"conversation_id": conversation(conversation_id), "pending": True, "turn_id": turn_id}

        with self.agent.lock:
            self._require_no_turn()
        return {"conversation_id": conversation(conversation_id), **self.agent.respond_to_user(text, conversation_id=conversation_id).to_dict()}

    def stream(self, text: str, conversation_id: str = "default"):
        if self.agent.provider.provider_name == "harness":
            import time
            from .dialogue_service import DialogueStreamEvent, _chunks
            turn = self.call("dialogue_begin", {"text": text, "conversation_id": conversation(conversation_id), "execution_owner": "service"})
            yield DialogueStreamEvent("start", {"user": turn["user"]})
            turn_id = turn["turn_id"]
            worker_alive = bool(getattr(self, "dialogue_worker", None) and getattr(self.dialogue_worker, "thread", None) and self.dialogue_worker.thread.is_alive())
            if not worker_alive:
                yield DialogueStreamEvent("done", {"response": {"user": turn["user"], "agent": None, "pending": True, "turn_id": turn_id}})
                return
            self.dialogue_worker.notify()
            deadline = time.monotonic() + 30.0
            committed_resp = None
            while time.monotonic() < deadline:
                path, current = self._read_turn(turn_id)
                if current.get("status") == "committed":
                    committed_resp = current.get("response")
                    break
                if current.get("status") == "cancelled":
                    yield DialogueStreamEvent("done", {"response": {
                        "user": turn["user"], "agent": None, "cancelled": True, "turn_id": turn_id}})
                    return
                time.sleep(0.08)
            if committed_resp and committed_resp.get("agent"):
                agent_text = committed_resp["agent"].get("text", "")
                for chunk in _chunks(agent_text):
                    yield DialogueStreamEvent("delta", {"text": chunk})
                yield DialogueStreamEvent("done", {"response": committed_resp})
            else:
                yield DialogueStreamEvent("done", {"response": {"user": turn["user"], "agent": None, "pending": True, "turn_id": turn_id}})
            return
        with self.agent.lock:
            self._require_no_turn()
        yield from self.agent.stream_to_user(text, conversation_id=conversation(conversation_id))


# One schema registry shared by all transports. Only explicit commands write.
def schema(description, properties=None, required=()):
    return {"description": description, "inputSchema": {"type": "object", "properties": properties or {},
            "required": list(required), "additionalProperties": False}}

STRING = {"type": "string"}
NATIVE_RESULT = {"type": "object", "properties": {
    "name": STRING, "goal": STRING, "steps": {"type": "array", "items": STRING},
    "body": STRING, "reply": STRING,
    "activity_profile": {"type": "object", "description": "Optional profile for the proposed activity, including work or leisure.",
        "properties": {"mode": STRING, "effort": {"type": "number", "minimum": 0, "maximum": 1}}},
    "service_intent": {"type": "object", "properties": {"need": STRING, "expected_help": STRING,
        "source_refs": {"type": "array", "items": STRING}},
        "description": "For user-related work or leisure, explain the connection and cite the supporting user memory."},
    "planning_decision": {"type": "object", "description": "Planning chooses a concrete activity. Use leisure for recreational recovery.",
        "properties": {"action": {"type": "string", "enum": ["act"]},
                       "duration_seconds": {"type": "integer", "description": "0 for act"}, "reason": STRING},
        "required": ["action", "duration_seconds", "reason"]},
    "deliverables": {"type": "array", "description": "Optional new text files for the service to create. Put existing native file paths in body instead.",
        "items": {"type": "object", "properties": {"name": STRING, "content": STRING}, "required": ["name", "content"]}},
}}
TOOLS = {
    "user_preferences": schema("Preview user preference imports; supply the returned review to commit the reviewed wording through this workspace's sole writer. Preserve inferred/confirmed status. Does not control lifecycle scheduling.",
        {"entries": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "object",
            "properties": {"text": STRING, "sources": {"type": "array", "items": STRING},
                           "category": STRING, "detail": STRING,
                           "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                           "status": {"type": "string", "enum": ["inferred", "confirmed"]}},
            "required": ["text", "sources", "status"], "additionalProperties": False}}, "review": STRING}, ("entries",)),
    "user_preferences_get": schema("Read detailed user preferences and provenance in this persona workspace. Filter by query or page using next_offset. Inferred interests remain hypotheses.",
        {"query": STRING, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 20}}),
    "execution_claim": schema("Claim due work for the existing host agent. Use its original model, tools and permissions; plugin does not execute the work.",
                              {"worker_id": STRING, "channel": STRING}, ("worker_id",)),
    "execution_complete": schema("Report actual host execution to the lifecycle owner. Bookkeeping result only; no restriction on the host's tools or response format.",
                                 {"job_id": STRING, "worker_id": STRING, "result": NATIVE_RESULT}, ("job_id", "worker_id", "result")),
    "execution_fail": schema("Report a claimed host execution failure; never fabricate completion.",
                             {"job_id": STRING, "worker_id": STRING, "error": STRING}, ("job_id", "worker_id", "error")),
    "execution_status": schema("Check whether claimed work is still active before performing another native operation; respect channel cancellation.",
                               {"job_id": STRING, "worker_id": STRING}, ("job_id", "worker_id")),
    "work_list": schema("List committed user-service artifacts for one conversation, without judging their usefulness.", {"conversation_id": STRING}),
    "work_file": schema("Read a hash-verified committed user-service work file for one conversation.", {"path": STRING, "conversation_id": STRING}, ("path",)),
    "exploration_mode": schema("Select exploration motive: persona or user_service, scoped to one owner conversation; does not start or schedule work.", {"mode": STRING, "conversation_id": STRING}, ("mode",)),
    "exploration_get": schema("Read the workspace exploration motive and owner conversation."),
    "dialogue_control": schema("Pause/resume only dialogue processing, without changing autonomous activities.", {"action": STRING}, ("action",)),
    "lifecycle_mode": schema("Set autonomous mode: rhythm or continuous; preserve heartbeat and dialogue settings.", {"mode": STRING}, ("mode",)),
    "lifecycle_control": schema("Pause/resume independent autonomous work; user dialogue remains available.", {"action": STRING}, ("action",)),
    "structure_get": schema("Read the visible activity/time memory trees, activity tree, schedule tree and Heartbeat/lifecycle status."),
    "health": schema("Read provider and runner health."),
    "state": schema("Read activities, goals, schedule and recent facts."),
    "timeline": schema("Read immutable events.", {"limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
    "dialogue_history": schema("Read one conversation.", {"conversation_id": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 200}}),
    "memory_search": schema("Retrieve source-labelled memory; use conversation_id for isolated conversation evidence.",
                            {"query": STRING, "memory_type": STRING, "conversation_id": STRING}, ("query",)),
    "context_get": schema("Get self or isolated user context before model generation.", {"kind": STRING, "conversation_id": STRING, "query": STRING}),
    "activity_create": schema("Record a planned activity. Creation is not execution.",
                              {"activity_id": STRING, "name": STRING, "goal": STRING, "parent_activity": STRING}, ("activity_id", "name")),
    "activity_start": schema("Start/resume an activity; interrupt the previous one.", {"activity_id": STRING}, ("activity_id",)),
    "activity_interrupt": schema("Checkpoint an activity.", {"activity_id": STRING, "reason": STRING}, ("activity_id", "reason")),
    "activity_complete": schema("Commit the harness-reported execution result, then close activity. Do not report unexecuted work.",
                                {"activity_id": STRING, "result": STRING}, ("activity_id", "result")),
    "heartbeat": schema("Advance time and event boundaries; does not execute external work.", {"at": STRING}),
    "dialogue_begin": schema("Record input and return concise context; host may commit or cancel a reply. Autonomous work is unaffected.",
                             {"text": STRING, "conversation_id": STRING, "execution_owner": STRING}, ("text",)),
    "dialogue_receive": schema("Receive an idempotent IM message into the independent dialogue channel.",
                               {"source_id": STRING, "text": STRING, "conversation_id": STRING, "persona_id": STRING},
                               ("source_id", "text", "conversation_id", "persona_id")),
    "dialogue_status": schema("Read a committed reply for an exact persona and conversation binding.",
                              {"turn_id": STRING, "conversation_id": STRING, "persona_id": STRING},
                              ("turn_id", "conversation_id", "persona_id")),
    "dialogue_commit": schema("Persist a host-generated reply; replay-safe and independent of autonomous execution.",
                              {"turn_id": STRING, "reply": STRING}, ("turn_id", "reply")),
    "dialogue_cancel": schema("Cancel a pending reply without controlling autonomous execution.", {"turn_id": STRING}, ("turn_id",)),
    "dialogue_reply": schema("Optional configured provider reply for Web UI; unavailable in harness mode.",
                             {"text": STRING, "conversation_id": STRING}, ("text",)),
}
