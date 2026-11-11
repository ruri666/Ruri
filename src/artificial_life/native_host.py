"""Hand lifecycle work to the existing agent without creating another model.

The host executes with its own tools and permissions. This mailbox only owns
handoff state; activity facts are committed by the existing lifecycle channel.
"""
from copy import deepcopy
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .host import Interrupted
from .exploration import USER_RELEVANCE
from .storage import atomic_write_json, read_json


class NativeHost:
    name = "native-host"
    native_execution = True

    def __init__(self, root: Path, auto_fallback_delay: float | None = None):
        self.root = root.resolve()
        self.auto_fallback_delay = auto_fallback_delay
        self.folder = self.root / "runtime/host-jobs"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.changed = threading.Event()
        self.pending = {}
        # No consumer survives the lifetime owner. Never hand stale work to a
        # second agent after restart and accidentally repeat external actions.
        for path in self.folder.glob("*/state.json"):
            state = read_json(path, {})
            if state.get("status") in {"queued", "claimed"}:
                atomic_write_json(path, {**state, "status": "cancelled", "reason": "service restarted"})

    def status(self):
        with self.lock:
            return {"mode": "native", "model": None, "model_owner": "host",
                    "tools_owner": "host", "requires_host_worker": True,
                    "pending_jobs": len(self.pending)}

    def run(self, request, cancel):
        if cancel.is_set():
            raise Interrupted("Native execution cancelled before handoff")
        job_id = uuid4().hex
        directory = self.folder / job_id
        directory.mkdir()
        value = deepcopy(request)
        value["execution_environment"] = {"mode": "native", "model_owner": "host", "tools_owner": "host",
            "instruction": "使用宿主原有模型、工具、权限和工作环境完成活动；插件只提供补充记忆与管理信息，不替换宿主指令或执行器。通过execution_complete提交实际结果，通过execution_fail报告执行失败。"}
        access = value.get("context", {}).get("base", {}).get("memory_access")
        if access:
            access["tool"] = "memory_search"
        channel = "dialogue" if request.get("kind") == "reply" else "autonomous"
        if channel == "dialogue":
            value["execution_environment"]["instruction"] = (
                "这是独立的陪伴对话。以当前人设回应 message，通过 execution_complete 提交 reply。"
                "自主活动的摘要只供理解对话，需要时可查记忆。执行失败通过 execution_fail 报告。"
            )
        if channel == "autonomous" and value.get("context", {}).get("base", {}).get("exploration", {}).get("mode") == "user_service":
            value["user_relevance"] = USER_RELEVANCE
        with self.lock:
            atomic_write_json(directory / "request.json", value)
            atomic_write_json(directory / "state.json", {"job_id": job_id, "channel": channel,
                                                         "status": "queued", "worker_id": None})
            self.pending[job_id] = directory / "state.json"
        self.changed.set()
        started_at = time.monotonic()
        while True:
            # Cancellation is checked before accepting a result, so a paused
            # dialogue cannot commit even if its host finishes at the same time.
            if cancel.is_set():
                with self.lock:
                    state = read_json(directory / "state.json", {})
                    atomic_write_json(directory / "state.json", {**state, "status": "cancelled"})
                    self.pending.pop(job_id, None)
                raise Interrupted("Native execution cancelled")
            with self.lock:
                state = read_json(directory / "state.json", {})
                if state.get("status") == "completed":
                    return read_json(directory / "result.json", {})
                if state.get("status") == "failed":
                    raise RuntimeError(state.get("error", "execution failed"))
                if self.auto_fallback_delay is not None and state.get("status") == "queued" and state.get("worker_id") is None:
                    if time.monotonic() - started_at >= self.auto_fallback_delay:
                        state.update(status="claimed", worker_id="native-engine")
                        atomic_write_json(directory / "state.json", state)
                        self._auto_execute(job_id, value)
                        continue
            self.changed.wait(.1)
            self.changed.clear()

    def _auto_execute(self, job_id: str, request: dict):
        from .companion_generator import generate_reply, generate_plan, generate_work, generate_route
        from .host import validate_result
        kind = request.get("kind")
        if kind == "reply":
            result = {"reply": generate_reply(request)}
        elif kind == "plan":
            result = generate_plan(request)
        elif kind == "work":
            result = generate_work(request)
        elif kind == "route":
            result = generate_route(request)
        else:
            result = {}
        validated = validate_result(result, request)
        self.complete(job_id, "native-engine", validated)

    def claim(self, worker_id, channel="autonomous"):
        self._worker(worker_id)
        if channel not in {"autonomous", "dialogue"}:
            raise ValueError("channel must be autonomous or dialogue")
        with self.lock:
            paths = list(self.pending.values())
            for path in paths:
                state = read_json(path, {})
                if state.get("channel") != channel:
                    continue
                if state.get("status") == "claimed" and state.get("worker_id") == worker_id:
                    return self._handoff(path, state)
            for path in paths:
                state = read_json(path, {})
                if state.get("channel") == channel and state.get("status") == "queued":
                    state.update(status="claimed", worker_id=worker_id)
                    atomic_write_json(path, state)
                    return self._handoff(path, state)
        return {"status": "empty", "channel": channel}

    @staticmethod
    def _handoff(path, state):
        request = read_json(path.parent / "request.json", {})
        if state["channel"] == "dialogue":
            return {**state, "request": request,
                    "completion": {"tool": "execution_complete", "result_fields": ["reply"],
                                   "instruction": "提交当前对话的实际回复，供聊天界面显示。"}}
        fields = {"plan": ["name", "goal", "steps"],
                  "work": ["body", "outcome", "deliverables"], "route": ["routing"]}
        return {**state, "request": request,
                "completion": {"tool": "execution_complete", "result_fields": fields.get(request.get("kind"), []),
                               "timing": "规划具体活动用act、duration_seconds:0。需要恢复精力时，用leisure选择与用户兴趣相关的实际娱乐，比如用宿主工具浏览相关视频或体验小游戏。按工具回执区分搜索、阅读简介和实际播放。",
                               "files": "原生已生成文件可在body引用路径；deliverables只接受[{name,content}]，用于服务创建新文本文件。",
                               "instruction": "结果是工具参数中的管理数据；宿主推理、工具调用和面向用户的表达形式保持原样。"}}

    def complete(self, job_id, worker_id, result):
        if not isinstance(result, dict):
            raise ValueError("result must be an object containing the actual execution result")
        with self.lock:
            path, state = self._owned(job_id, worker_id)
            if state["status"] == "completed":
                if read_json(path.parent / "result.json", {}) != result:
                    raise ValueError("job already completed with a different result")
                return {"job_id": job_id, "status": "completed"}
            if state["status"] != "claimed":
                raise ValueError("job is no longer claimed")
            reply = result.get("reply", "")
            if state["channel"] == "dialogue" and (not isinstance(reply, str) or not reply.strip()):
                raise ValueError("dialogue result requires a nonempty reply")
            if state["channel"] == "autonomous" and reply:
                raise ValueError("autonomous results cannot submit a dialogue reply")
            atomic_write_json(path.parent / "result.json", result)
            atomic_write_json(path, {**state, "status": "completed"})
            self.pending.pop(job_id, None)
        self.changed.set()
        return {"job_id": job_id, "status": "completed"}

    def fail(self, job_id, worker_id, error):
        if not isinstance(error, str) or not error.strip():
            raise ValueError("error must be nonempty")
        with self.lock:
            path, state = self._owned(job_id, worker_id)
            if state["status"] != "claimed":
                raise ValueError("job is no longer claimed")
            atomic_write_json(path, {**state, "status": "failed", "error": error})
            self.pending.pop(job_id, None)
        self.changed.set()
        return {"job_id": job_id, "status": "failed"}

    def inspect(self, job_id, worker_id):
        with self.lock:
            _, state = self._owned(job_id, worker_id)
            return dict(state)

    def _owned(self, job_id, worker_id):
        self._worker(worker_id)
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise ValueError("invalid job_id")
        path = self.folder / job_id / "state.json"
        state = read_json(path, {})
        if state.get("worker_id") != worker_id:
            raise ValueError("job is not owned by this worker")
        return path, state

    @staticmethod
    def _worker(worker_id):
        if not isinstance(worker_id, str) or not worker_id.strip() or len(worker_id) > 128:
            raise ValueError("worker_id must contain 1..128 characters")
