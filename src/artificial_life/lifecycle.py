"""Independent persistent life loop: plans, does work, handles users, waits, repeats."""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from .host import Host, Interrupted
from .activity_plugins import ActivityPlugins
from .dialogue_worker import DialogueWorker
from .models import Goal, GoalLevel, GoalStatus, ScheduleBlock, ScheduleStatus, as_utc, iso
from .memory import RawPointer
from .storage import atomic_write_json, atomic_write_text, read_json
from . import affect


class Lifecycle:
    def __init__(self, service, host: Host, interval: int = 1800, retry: int = 60, mode=None):
        if interval < 1 or retry < 1:
            raise ValueError("interval/retry must be positive")
        self.service, self.agent, self.host = service, service.agent, host
        self.agent.host_decides_activities = True
        self.interval, self.retry = interval, retry
        self.plugins = None if getattr(host, "native_execution", False) else ActivityPlugins(self.agent.root)
        self.path = self.agent.root / "state/lifecycle.json"
        self.state = read_json(self.path, {"enabled": True, "next_wake_at": iso(self.agent.clock.now())})
        if mode is not None and mode not in {"rhythm", "continuous"}:
            raise ValueError("mode must be rhythm or continuous")
        self.state["mode"] = mode or "continuous"
        self.state.setdefault("route_signature", affect.signature(self.agent.runtime.state))
        self.state.setdefault("route_affect_count", 0)
        self.stop_event, self.cancel = threading.Event(), threading.Event()
        self.pulse = threading.Event()
        self.thread = None
        if self.state.get("quiet_wait"):
            previous = self.state["quiet_wait"]
            self.fact("lifecycle.legacy_wait_released", {"previous": previous, "reason": "user requires continuous engagement"})
            self.save(quiet_wait=None, needs_replan=True, next_wake_at=iso(self.agent.clock.now()))
        self._retire_legacy_rest()
        self.dialogue = DialogueWorker(service, host, retry=retry)
        service.dialogue_worker = self.dialogue
        service.runner.on_pulse = self.scheduler_pulse
        service.lifecycle = self

    def _retire_legacy_rest(self):
        previous = self.state.get("timed_rest") or {}
        retired = []
        for activity in self.agent.activities.unfinished():
            if (activity.activity_id == previous.get("activity_id")
                    or activity.metadata.get("rest_duration_seconds")
                    or activity.metadata.get("activity_profile", {}).get("mode") == "rest"):
                self.agent.cancel_activity(activity.activity_id, "恢复精力改由娱乐活动承担")
                self._cancel_schedule(activity.activity_id)
                self._close_parents(activity.parent_activity)
                retired.append(activity.activity_id)
        if previous or retired:
            self.fact("lifecycle.legacy_rest_retired", {"activity_ids": retired, "previous": previous})
            self.save(timed_rest=None, activity_id=None, needs_replan=True,
                      phase="planning_required" if self.state.get("enabled", True) else "paused",
                      next_wake_at=iso(self.agent.clock.now()))

    def set_mode(self, mode):
        if mode not in {"rhythm", "continuous"}:
            raise ValueError("mode must be rhythm or continuous")
        with self.agent.lock:
            self.save(mode=mode)
            if mode == "continuous" and self.state.get("phase") != "retrying" and not self.state.get("quiet_wait"):
                self.save(next_wake_at=iso(self.agent.clock.now()))
            self.fact("lifecycle.mode_changed", {"mode": mode})
        self.wake()
        return self.status()

    def status(self):
        from .exploration import settings
        return {**self.state, "exploration": settings(self.agent.root), "worker_running": bool(self.thread and self.thread.is_alive()), "host": self.host.name,
                "engagement": {"always_engaged":True, "global_wait":False, "timed_rest":False},
                "execution": self.host.status() if hasattr(self.host, "status") else {}, "dialogue": self.dialogue.status()}

    def save(self, **values):
        self.state.update(values)
        atomic_write_json(self.path, self.state)

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.pulse.set()
        self.dialogue.start()
        self.thread = threading.Thread(target=self.run, name="artificial-life-executor", daemon=True)
        self.thread.start()

    def interrupt(self):
        self.cancel.set()

    def wake(self):
        self.pulse.set()

    def scheduler_pulse(self):
        # A deterministic scheduler heartbeat: no model calls or new decisions.
        with self.agent.lock:
            if not self.state.get("enabled", True):
                return
            if self.state.get("quiet_wait"):
                from .quiet_wait import input_signature
                signature = input_signature(self.agent)
                if signature != self.state["quiet_wait"]["input_signature"]:
                    self.fact("lifecycle.wait_context_changed", {"before": self.state["quiet_wait"]["input_signature"], "after": signature})
                    self.save(quiet_wait=None, phase="waiting", next_wake_at=iso(self.agent.clock.now()))
            if self.agent.clock.now() >= as_utc(self.state["next_wake_at"]):
                self.wake()

    def stop(self):
        self.stop_event.set()
        self.cancel.set()
        self.pulse.set()
        self.dialogue.stop()
        if self.thread:
            self.thread.join(15)

    def fact(self, kind, payload):
        self.agent._append_dialogue_fact(kind, self.agent.clock.now(), payload)

    def _context(self, kind="self", conversation_id="default", query=None):
        context = self.service.context_get(kind, conversation_id, query=query)
        # Bound the prompt. Canonical full raw/source pointers remain on disk.
        context["working"].pop("current_activity_recent_raw", None)
        return context

    def _recent_work(self):
        paths = sorted((self.agent.root / "works" / "autonomous").glob("*.md"), key=lambda p: p.stat().st_mtime)[-2:]
        # Bootstrap continuity from existing authored work, without reading scripts.
        if not paths:
            paths = sorted((self.agent.root / "works").rglob("03-*.md"))[-1:]
        from .exploration import settings
        exploration = settings(self.agent.root)
        if exploration["mode"] == "user_service":
            hidden = {e.payload["path"] for e in self.agent.runtime.timeline.events()
                if e.event_type == "lifecycle.artifact_committed" and e.payload.get("user_service")
                and e.payload["user_service"]["conversation_id"] != exploration["conversation_id"]}
            paths = [p for p in paths if str(p) not in hidden]
        return [{"path": str(p.relative_to(self.agent.root)), "text": p.read_text(encoding="utf-8-sig")[-5000:]} for p in paths]

    def tick(self):
        with self.agent.lock:
            self.cancel.clear()
            now = self.agent.clock.now()
            if self.stop_event.is_set():
                return
            if not self.state.get("enabled", True) or now < as_utc(self.state["next_wake_at"]):
                return
            if self.state.get("quiet_wait"):
                self.save(quiet_wait=None)
            leaves = [] if self.state.get("needs_replan") else [a for a in self.agent.activities.unfinished() if not a.children]
            if leaves:
                future, eligible = [], []
                for item in leaves:
                    windows = [b for b in self.agent.runtime.schedule.all() if b.metadata.get("activity_id") == item.activity_id]
                    cadence_ready = self.state["mode"] == "continuous" and windows and windows[0].metadata.get("cadence_generated")
                    if item.status == "planned" and windows and windows[0].start_at > now and not cadence_ready:
                        future.append(windows[0].start_at)
                    else:
                        eligible.append((windows[0].start_at if windows else now, item))
                if not eligible:
                    self.save(phase="waiting", next_wake_at=iso(min(future)))
                    return
                # The committed schedule defines execution order. No hard-coded
                # user priority or semantic selection is introduced by the runner.
                activity = sorted(eligible, key=lambda pair: pair[0])[0][1]
                committed = next((a for _, a in eligible if a.activity_id == self.state.get("route_next_id")), None)
                if committed:
                    activity = committed
                    self.save(route_next_id=None)
                request = None if committed else self._route_request(eligible, activity)
                if request:
                    self.save(phase="routing", last_error=None)
                else:
                    if activity.status != "active":
                        self.service.activity_start(activity.activity_id)
                    self._ensure_window(activity.activity_id, now)
                    request = {"kind": "work", "activity": {"id": activity.activity_id, "name": activity.name, "goal": activity.goal},
                               "context": self._context(), "recent_work": self._recent_work(), "work_started_at": iso(now),
                               "activity_profile": affect.profile(activity.metadata.get("activity_profile")), "affect_contract": True,
                               "proactive_contract": True, "proactive_available": self._proactive_available()}
                    origin = activity.metadata.get("user_service")
                    if origin and origin.get("conversation_id") == request["context"]["base"].get("exploration", {}).get("conversation_id"):
                        request["activity"]["user_connection"] = {
                            "need": origin.get("need", "")[:240],
                            "expected_help": origin.get("expected_help", "")[:240],
                            "source_refs": origin.get("verified_source_refs", [])[:6],
                        }
                    for block in self.agent.runtime.schedule.all():
                        if block.metadata.get("activity_id") == activity.activity_id:
                            if block.metadata.get("user_request"):
                                request["user_request"] = block.metadata["user_request"]
                            if block.metadata.get("plugin_action"):
                                request["plugin_action"] = block.metadata["plugin_action"]
                            break
                    receipts = self._plugin_results(activity.parent_activity)
                    if receipts:
                        request["plugin_results"] = receipts
                    request["work_contract"] = True
                    request["available_observation_refs"] = [r["evidence_ref"] for r in receipts]
                    request["available_evidence_refs"] = list(dict.fromkeys(
                        request["available_observation_refs"] + request["context"].get("source_refs", []) +
                        [r["path"] for r in request["recent_work"]]))
                    self.save(phase="working", activity_id=activity.activity_id, last_error=None)
            else:
                request = {"kind": "plan", "context": self._context(), "recent_work": self._recent_work()}
                from .quiet_wait import metadata_only_activities
                empty_ids = metadata_only_activities(self.agent.runtime.timeline.events(), self.agent.activities.all())
                request["context"]["base"]["goals"] = [g.to_dict() for g in self.agent.runtime.goals.all()
                    if not(g.status.value in {"completed", "blocked"} and g.id.rsplit("-", 1)[0] in empty_ids)]
                request["context"]["base"]["schedule"] = [b.to_dict() for b in self.agent.runtime.schedule.all()
                    if not(b.status.value in {"completed", "cancelled"} and (b.metadata.get("activity_id") or b.metadata.get("activity_root_id")) in empty_ids)]
                self.save(phase="planning", activity_id=None, last_error=None)
            from .quiet_wait import feedback, input_signature
            request["execution_feedback"] = feedback(self.agent.runtime.timeline.events())
            request["input_signature"] = input_signature(self.agent)
            request["engagement_contract"] = {"always_engaged":True, "global_wait":False,
                "recovery_mode":"leisure", "replanning_required":bool(self.state.get("needs_replan"))}
            if request["kind"] == "work":
                request["allow_discovery"] = True
                request["routing_contract"] = True
                remaining = [a for a in self.agent.activities.unfinished() if not a.children and a.activity_id != activity.activity_id]
                request["route_candidates"] = [{"activity_id":a.activity_id, "name":a.name, "goal":a.goal,
                    "activity_profile":affect.profile(a.metadata.get("activity_profile"))} for a in remaining]
                request["default_activity_id"] = remaining[0].activity_id if remaining else None
                request["state_guidance"] = affect.suggested_load(self.agent.runtime.state, request["activity_profile"], 0)
            if self.plugins and self.plugins.entries and request["kind"] in {"plan", "route"}:
                request["plugin_capabilities"] = self.plugins.catalog()
                request["recent_plugin_results"] = self._plugin_results(None, include_recent=True)
            elif self.plugins and self.plugins.entries and request.get("allow_discovery"):
                request["available_capabilities"] = self.plugins.catalog()
            control_window = self._control_window(request["kind"], now) if request["kind"] in {"plan", "route"} else None
            self.fact("lifecycle.step_started", {"kind": request["kind"], "host": self.host.name})
        # No canonical lock is held while the model runs; UI and user input remain responsive.
        receipt = None
        model_request = None
        if request.get("plugin_action") and self.plugins is not None:
            action = request["plugin_action"]
            previous = None
            try:
                if action["input_from_step"]:
                    source_id = activity.parent_activity + f"-{action['input_from_step']}"
                    sources = [r for r in request.get("plugin_results", []) if r["activity_id"] == source_id]
                    if not sources:
                        raise ValueError("Plugin dependency has no completed receipt")
                    previous = sources[0]["result"]
                receipt, path = self.plugins.execute(activity.activity_id, action, previous, self.cancel)
                result = {"body": "插件活动回执：" + str(path) + "\n\n" + json.dumps(receipt, ensure_ascii=False),
                          "outcome": {"status": "completed", "kind": "observation", "reason": "successful tool receipt",
                                      "evidence_refs": ["plugin:" + activity.activity_id]}}
                if request["activity_profile"]["mode"] == "leisure":
                    reflection = dict(request)
                    reflection.pop("plugin_action", None)
                    reflection["plugin_results"] = request.get("plugin_results", []) + [{
                        "activity_id": activity.activity_id, "evidence_ref": "plugin:" + activity.activity_id,
                        "capability": receipt["capability"], "result": receipt["result"]}]
                    reflection["available_observation_refs"] = request["available_observation_refs"] + ["plugin:" + activity.activity_id]
                    reflection["available_evidence_refs"] = request["available_evidence_refs"] + ["plugin:" + activity.activity_id]
                    reflection["allow_discovery"] = True
                    reflection["experience_instruction"] = "刚刚已实际执行该娱乐工具。根据真实结果报告你的体验；可自然给用户发消息。无需再写研究报告或虚构操作。"
                    model_request = reflection
                    from .adaptive_execution import run_adaptive
                    result.update(run_adaptive(self.host, self.service, reflection, self.cancel, plugins=self.plugins))
            except Interrupted:
                raise
            except Exception as exc:
                result = {"body": "本次工具调用未完成：" + str(exc), "outcome": {
                    "status": "blocked", "kind": "observation", "reason": str(exc)[:2000], "evidence_refs": []}}
        else:
            model_request = request
            if request["kind"] == "plan":
                from .engagement import plan
                result = plan(self.host, self.service, request, self.cancel, self.plugins)
            elif request["kind"] in {"work", "route"}:
                from .adaptive_execution import run_adaptive
                result = run_adaptive(self.host, self.service, request, self.cancel, plugins=self.plugins)
            else:
                result = self.host.run(request, self.cancel)
        if model_request is not None:
            result = self._repair_proposal(result, model_request)
        with self.agent.lock:
            if self.cancel.is_set():
                raise Interrupted("New input arrived before commit")
            if control_window:
                self._ensure_window(control_window, self.agent.clock.now())
                self.save(control_window_id=None)
            if request["kind"] == "route":
                self._apply_route(result, request)
                self.save(phase="waiting", next_wake_at=iso(self.agent.clock.now()), last_error=None)
            elif request["kind"] == "plan":
                cycle = self._apply_plan(result, exploration=request["context"]["base"].get("exploration"))
                self.save(phase="waiting", next_wake_at=iso(self.agent.clock.now()), last_error=None, needs_replan=False,
                          route_next_id=cycle+"-1", route_signature=affect.signature(self.agent.runtime.state), route_authorized_root=cycle)
            else:
                outcome = self._validate_outcome(result.get("outcome"), request, receipt)
                from .deliverables import validate as validate_files, persist as persist_files
                validate_files(result.get("deliverables", []))
                deliverables = persist_files(self.agent.root, activity.activity_id, result.get("deliverables", []))
                reported_experience = affect.experience(result.get("experience"))
                message = result.get("proactive_message", "")
                message = message if isinstance(message, str) else ""
                discoveries = result.get("discoveries", [])
                discoveries = discoveries if isinstance(discoveries, list) else []
                body = result.get("body", "")
                body = body if isinstance(body, str) else ""
                assessment = self._assess_outcome(outcome)
                artifact = self.agent.root / "works/autonomous" / (activity.activity_id + ".md")
                if not body.strip():
                    artifact = artifact.with_suffix(".json")
                artifact.parent.mkdir(parents=True, exist_ok=True)
                if artifact.exists():
                    artifact = artifact.with_stem(artifact.stem + "-" + uuid4().hex[:8])
                if body.strip():
                    atomic_write_text(artifact, body+"\n")
                else:
                    atomic_write_json(artifact, {"activity_id":activity.activity_id, "body":"", "outcome":outcome,
                                      "experience":reported_experience, "evidence_assessment":assessment})
                digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
                record = f"实际记录文档：{artifact}\nSHA256: {digest}\n类型/状态：{outcome['kind']}/{outcome['status']}\n\n{body}"
                if deliverables:
                    record += "\n\n实际文件（未自动执行）：\n" + json.dumps(deliverables, ensure_ascii=False)
                self._ensure_window(activity.activity_id, self.agent.clock.now())
                if outcome["status"] == "completed":
                    self._complete_record(activity.activity_id, record)
                else:
                    self.agent.record_agent(record, kind="activity_blocked", activity_id=activity.activity_id)
                    self.agent.cancel_activity(activity.activity_id, outcome["reason"])
                    self._cancel_schedule(activity.activity_id)
                    self.fact("lifecycle.work_blocked", {"activity_id": activity.activity_id, "reason": outcome["reason"]})
                self._close_parents(activity.parent_activity)
                self.fact("lifecycle.artifact_committed", {"activity_id": activity.activity_id, "path": str(artifact), "sha256": digest,
                          "outcome": outcome, "evidence_assessment": assessment, "body_present": bool(body.strip()),
                          "input_signature": request["input_signature"],
                          "deliverables": deliverables, "user_service": activity.metadata.get("user_service", {})})
                affect.commit_work(self.agent, activity.activity_id, request["activity_profile"], reported_experience,
                                   max(0, (self.agent.clock.now()-as_utc(request["work_started_at"])).total_seconds()),
                                   blocked=outcome["status"] == "blocked")
                if message.strip():
                    sent = self.agent.send_agent_message(message.strip(), conversation_id="default")
                    self.fact("lifecycle.proactive_message", {"activity_id": activity.activity_id,
                              "record_id": sent.record_id, "conversation_id": "default",
                              "message_sha256": hashlib.sha256(sent.text.encode("utf-8")).hexdigest(),
                              "artifact_sha256": digest, "disposition": affect.routing_context(self.agent.runtime.state)})
                if receipt:
                    self.fact("lifecycle.plugin_completed", {"activity_id": activity.activity_id, "capability": receipt["capability"],
                              "receipt": str(path), "result_sha256": receipt["result_sha256"]})
                for proposal in discoveries:
                    self._apply_plan(proposal, discovery_source={"activity_id": activity.activity_id,
                        "path": str(artifact), "sha256": digest}, exploration=request["context"]["base"].get("exploration"))
                if result.get("routing"):
                    self._apply_route(result, request)
                self.save(phase="waiting", activity_id=None, last_artifact=str(artifact), last_error=None,
                          next_wake_at=iso(self.agent.clock.now()+timedelta(
                              seconds=0 if self.state["mode"] == "continuous" else self.interval)))
                self._backoff_empty_results(request["input_signature"])
            self.fact("lifecycle.step_completed", {"kind": request["kind"], "host": self.host.name})
            self.save(failures=0)
            self.service.structure_get()
            if self.state["mode"] == "continuous" and not self.state.get("quiet_wait"):
                self.wake()

    def _backoff_empty_results(self, signature):
        # Compatibility name: the new policy requests a different activity,
        # rather than applying the former global idle backoff.
        from .quiet_wait import feedback
        recent = feedback(self.agent.runtime.timeline.events())
        count = 0
        for item in reversed(recent["recent_results"]):
            if item["input_signature"] != signature or item["body_present"] or item["file_count"] or item["tool_receipt_present"]:
                break
            count += 1
        if count >= 3:
            self.fact("lifecycle.replanning_required", {"no_material_results":count, "input_signature":signature})
            self.save(needs_replan=True, phase="planning_required", next_wake_at=iso(self.agent.clock.now()))

    def _route_request(self, eligible, default):
        # Next-step choice is carried by every work result, not a state threshold.
        return None

    def _apply_route(self, result, request):
        decision = self._validate_routing(result, request)
        if decision["action"] == "plan":
            selected = self._apply_plan(result, exploration=request.get("context", {}).get("base", {}).get("exploration")) + "-1"
        else:
            selected = request["default_activity_id"] if decision["action"] == "continue" else decision["activity_id"]
        # The chosen execution order is itself a committed model-owned plan.
        disposition = affect.routing_context(self.agent.runtime.state)
        sources = [e.id for e in self.agent.runtime.timeline.events() if e.event_type in {"affect.updated", "runtime.initialized"}]
        revisions = []
        for block in self.agent.runtime.schedule.all():
            if block.metadata.get("activity_id") == selected:
                before = block.to_dict()
                block.start_at = min(block.start_at, self.agent.clock.now())
                block.end_at = max(block.end_at, self.agent.clock.now()+timedelta(seconds=self.interval))
                block.metadata.update(route_selected_at=iso(self.agent.clock.now()), route_reason=decision["reason"],
                                      route_source_event_id=sources[-1] if sources else None)
                revisions.append({"before":before, "after":block.to_dict(), "reason":"model next-step choice"})
                revisions.extend(self._expand_schedule_parents(block))
        self.agent.runtime.plan_store.save_schedule(self.agent.runtime.schedule)
        for revision in revisions:
            self.fact("schedule.revised", revision)
        self.fact("schedule.route_committed", {"selected_activity_id": selected, "decision": decision,
                  "disposition": disposition, "source_event_id": sources[-1] if sources else None})
        target = self.agent.activities.get(selected) if selected else None
        self.save(route_next_id=selected, route_authorized_root=(target.parent_activity or selected) if target else None,
                  route_affect_count=sum(e.event_type == "affect.updated" for e in self.agent.runtime.timeline.events()),
                  route_signature=affect.signature(self.agent.runtime.state))

    def _validate_routing(self, result, request):
        value = result.get("routing", {})
        decision = dict(value) if isinstance(value, dict) else {}
        decision.setdefault("action", "continue")
        decision.setdefault("activity_id", "")
        decision.setdefault("reason", "")
        ids = {a["activity_id"] for a in request.get("route_candidates", [])}
        if decision["action"] not in {"continue", "select", "plan"} or (decision["action"] == "select" and decision["activity_id"] not in ids):
            self.fact("lifecycle.advisory", {"kind":"routing", "declared_decision":decision,
                      "message":"无法定位所选执行器/活动，保留声明并沿用已有日程。"})
            return {"action":"continue", "activity_id":"", "reason":str(decision["reason"])}
        return decision

    def _repair_proposal(self, result, request):
        # No policy-driven retries. Keep the declaration and report advisories.
        if not isinstance(result, dict):
            raise ValueError("Result must be a JSON object")
        return result

    def _validate_work_proposal(self, result, request):
        if not isinstance(result, dict):
            raise ValueError("Result must be a JSON object")
        # Only executable data types belong to the transport contract.
        if not isinstance(result.get("body", ""), str):
            raise ValueError("body must be a string")

    def _proactive_available(self):
        return True

    def _validate_outcome(self, outcome, request, receipt=None):
        value = dict(outcome) if isinstance(outcome, dict) else {}
        value.setdefault("status", "completed")
        value.setdefault("kind", "design")
        value.setdefault("reason", "")
        value.setdefault("evidence_refs", [])
        return value

    def _assess_outcome(self, outcome):
        refs = outcome.get("evidence_refs", [])
        refs = refs if isinstance(refs, list) else [refs]
        resolved, unresolved = [], []
        for ref in refs:
            try:
                valid = isinstance(ref, str) and self._evidence_exists(ref)
            except (OSError, ValueError, KeyError, TypeError):
                valid = False
            (resolved if valid else unresolved).append(ref)
        assessment = {"resolved_refs": resolved, "unresolved_refs": unresolved,
                      "successful_tool_receipt": any(r.startswith("plugin:") for r in resolved),
                      "semantic_acceptance": "pending"}
        if unresolved or (outcome.get("kind") == "observation" and not assessment["successful_tool_receipt"]):
            self.fact("lifecycle.advisory", {"kind":"evidence", "assessment":assessment,
                      "message":"来源尚未核实；保留模型声明，不阻止活动提交，不将声明当作已核验事实。"})
        return assessment

    def _evidence_exists(self, reference):
        """Resolve canonical references without interpreting source prose."""
        if reference.startswith("activity:"):
            return reference.removeprefix("activity:") in self.agent.activities.tree.nodes
        if reference.startswith("raw:"):
            parts = reference.split(":")
            if len(parts) != 3 or parts[1] not in {"agent", "user"}:
                return False
            positions = {r.record_id: index for index, r in enumerate(getattr(self.agent.raw, parts[1]).records())}
            bounds = parts[2].split("-", 1)
            return all(bound in positions for bound in bounds) and positions[bounds[0]] <= positions[bounds[-1]]
        if reference.startswith("temporal:"):
            return reference.removeprefix("temporal:") in self.agent.temporal.tree.nodes
        if reference.startswith("conversation:"):
            return reference.removeprefix("conversation:") in self.agent.conversations.tree.nodes
        if reference.startswith("timeline:"):
            return any(e.id == reference.removeprefix("timeline:") for e in self.agent.runtime.timeline.events())
        root = self.agent.root.resolve()
        if reference.startswith("plugin:"):
            activity_id = reference.removeprefix("plugin:")
            folder = (root / "works/plugin-activities" / activity_id).resolve()
            if not folder.is_relative_to(root / "works/plugin-activities"):
                return False
            saved = read_json(folder / "调用回执.json", {})
            raw = folder / "插件原始结果.json"
            return bool(saved and saved.get("activity_id") == activity_id and raw.is_file() and
                        hashlib.sha256(raw.read_bytes()).hexdigest() == saved.get("result_sha256") and
                        read_json(raw, {}) == saved.get("result"))
        path = (root / reference.replace("\\", "/")).resolve()
        if not path.suffix:
            path = path.with_suffix(".md")
        if not path.is_relative_to(root / "works/autonomous") or not path.is_file():
            return False
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return any(e.event_type == "lifecycle.artifact_committed" and
                   Path(e.payload["path"]).name == path.name and e.payload.get("sha256") == digest
                   for e in self.agent.runtime.timeline.events())

    def _cancel_schedule(self, activity_id):
        for block in self.agent.runtime.schedule.all():
            if block.metadata.get("activity_id") == activity_id or block.metadata.get("activity_root_id") == activity_id:
                before = block.to_dict()
                block.status = ScheduleStatus.CANCELLED
                self.fact("schedule.revised", {"before": before, "after": block.to_dict(), "reason": "activity blocked"})
        self.agent.runtime.plan_store.save_schedule(self.agent.runtime.schedule)

    def _plugin_results(self, parent_id, include_recent=False):
        results = []
        children = list(self.agent.activities.get(parent_id).children) if parent_id else []
        required_children = set(children)
        if include_recent:
            recent = [e.payload["activity_id"] for e in self.agent.runtime.timeline.events() if e.event_type == "lifecycle.plugin_completed"][-3:]
            children = list(dict.fromkeys(children + recent))
        for child in children:
            known_ids = {a.activity_id for a in self.agent.activities.all()}
            # Adaptive tool receipts describe actual calls, not standalone
            # Activity nodes. They remain facts even if later writing fails.
            if child not in known_ids or self.agent.activities.get(child).status == "completed":
                receipt = read_json(self.agent.root / "works/plugin-activities" / child / "调用回执.json", {})
                if receipt:
                    raw = self.agent.root / "works/plugin-activities" / child / "插件原始结果.json"
                    if hashlib.sha256(raw.read_bytes()).hexdigest() != receipt["result_sha256"] or read_json(raw, {}) != receipt["result"]:
                        self.fact("lifecycle.advisory", {"kind":"changed_tool_result", "activity_id":child,
                                  "message":"结果与历史回执不符，不作为已核验工具结果提供；后续活动仍可继续。"})
                        continue
                    entry = {"activity_id": child, "evidence_ref": "plugin:" + child, "capability": receipt["capability"], "result": receipt["result"],
                             "receipt": str(raw.with_name("调用回执.json")), "result_sha256": receipt["result_sha256"]}
                    if child in required_children or len(str(results + [entry])) <= 16000:
                        results.append(entry)
        return results

    def _ensure_window(self, activity_id, now):
        blocks = [b for b in self.agent.runtime.schedule.all() if b.metadata.get("activity_id") == activity_id or b.id == activity_id]
        if not blocks:
            return
        block = blocks[0]
        if block.contains(now):
            return
        old = block.to_dict()
        block.start_at, block.end_at = min(block.start_at, now), max(block.end_at, now+timedelta(seconds=self.interval))
        revisions = self._expand_schedule_parents(block)
        self.agent.runtime.plan_store.save_schedule(self.agent.runtime.schedule)
        self.fact("schedule.revised", {"before": old, "after": block.to_dict(), "reason": "actual availability after downtime/user interruption"})
        for revision in revisions:
            self.fact("schedule.revised", revision)

    def _expand_schedule_parents(self, block):
        """Retain complete child envelopes when execution timing changes."""
        revisions, seen = [], {block.id}
        while block.metadata.get("parent_schedule_id"):
            parent_id = block.metadata["parent_schedule_id"]
            if parent_id in seen:
                raise ValueError("cycle in schedule tree")
            seen.add(parent_id)
            parent = self.agent.runtime.schedule.get(parent_id)
            before = parent.to_dict()
            parent.start_at = min(parent.start_at, block.start_at)
            parent.end_at = max(parent.end_at, block.end_at)
            if parent.to_dict() != before:
                revisions.append({"before":before, "after":parent.to_dict(), "reason":"contain revised child window"})
            block = parent
        return revisions

    def _control_window(self, kind, now):
        """Schedule real planning/routing time, without inventing a substantive task."""
        goal_id = "lifecycle-planning"
        if not any(g.id == goal_id for g in self.agent.runtime.goals.all()):
            goal = Goal(goal_id, GoalLevel.LARGE, "自主规划与活动取舍（运行维护）")
            self.agent.runtime.goals.add(goal)
            self.agent.runtime.plan_store.save_goals(self.agent.runtime.goals)
            self.fact("goal.created", goal.to_dict())
        expired = [b.id for b in self.agent.runtime.schedule.all() if b.metadata.get("lifecycle_control") and b.end_at <= now]
        for eid in expired:
            self.agent.runtime.schedule.remove(eid)
        block = ScheduleBlock("control-"+uuid4().hex[:12], goal_id, now,
                              now+timedelta(seconds=self.interval), activity_type=kind,
                              metadata={"lifecycle_control": True, "phase": kind, "autonomous": True})
        self.agent.runtime.schedule.add(block)
        self.agent.runtime.plan_store.save_schedule(self.agent.runtime.schedule)
        self.fact("schedule.created", block.to_dict())
        self.save(control_window_id=block.id)
        return block.id

    def _validate_plan(self, result):
        if not isinstance(result, dict):
            raise ValueError("Plan must be a JSON object")
        name = str(result.get("name") or result.get("goal") or "自主活动")
        goal = str(result.get("goal") or name)
        steps = result.get("steps", [])
        if not isinstance(steps, list) or not steps or any(not isinstance(x, str) or not x.strip() for x in steps):
            raise ValueError("steps must be a non-empty string array")
        actions = {}
        for action in result.get("plugin_actions", []) if self.plugins is not None else []:
            if isinstance(action, dict) and type(action.get("step")) is int and 1 <= action["step"] <= len(steps):
                actions[action["step"]] = action
            else:
                self.fact("lifecycle.advisory", {"kind":"plugin_action", "declared_action":action,
                          "message":"工具步骤无法对应可执行步骤，原始请求保留在计划声明中。"})
        return name, goal, steps, actions, affect.profile(result.get("activity_profile"))

    def _apply_plan(self, result, user_task=False, user_request=None, discovery_source=None, exploration=None):
        from .engagement import planning_decision
        planning_decision(result)
        name, goal, steps, actions, activity_profile = self._validate_plan(result)
        now, cycle = self.agent.clock.now(), "life-"+uuid4().hex[:12]
        tree = self.agent.runtime.goals
        goals = [Goal(cycle+"-large", GoalLevel.LARGE, name),
                 Goal(cycle+"-medium", GoalLevel.MEDIUM, goal, parent_id=cycle+"-large"),
                 Goal(cycle+"-small", GoalLevel.SMALL, "完成本轮活动树", parent_id=cycle+"-medium")]
        for g in goals:
            tree.add(g)
            self.fact("goal.created", g.to_dict())
        self.agent.runtime.plan_store.save_goals(tree)
        self.agent.create_activity(activity_id=cycle, name=name, goal=goal)
        schedule = self.agent.runtime.schedule
        origin = {"activity_profile": activity_profile, "declared_plan": result,
                  "disposition": affect.routing_context(self.agent.runtime.state),
                  **({"user_request": user_request} if user_task and user_request else {})}
        from .exploration import settings, assess_intent
        exploration = exploration or settings(self.agent.root)
        if exploration["mode"] == "user_service":
            origin["user_service"] = assess_intent(self.agent.raw, exploration["conversation_id"], result.get("service_intent"))
            root_node = self.agent.activities.get(cycle)
            root_node.metadata["user_service"] = origin["user_service"]
            self.agent.activities._put(root_node)
        if discovery_source:
            origin["discovery_source"] = discovery_source
        stride = 0 if self.state["mode"] == "continuous" else self.interval
        parent = ScheduleBlock(cycle, goals[-1].id, now, now+timedelta(seconds=self.interval*len(steps)),
                               activity_type=name, metadata={"activity_root_id": cycle, "autonomous": not user_task, "user_task": user_task, **origin})
        schedule.add(parent)
        self.fact("schedule.created", parent.to_dict())
        for index, step in enumerate(steps):
            child = cycle+f"-{index+1}"
            self.agent.create_activity(activity_id=child, name=step, goal=goal, parent_activity=cycle)
            node = self.agent.activities.get(child)
            node.metadata["activity_profile"] = activity_profile
            if "user_service" in origin:
                node.metadata["user_service"] = origin["user_service"]
            self.agent.activities._put(node)
            block = ScheduleBlock(child, goals[-1].id, now+timedelta(seconds=stride*index),
                                  now+timedelta(seconds=stride*index+self.interval), activity_type=step,
                                  metadata={"parent_schedule_id": cycle, "activity_id": child, "autonomous": not user_task, "user_task": user_task,
                                            "cadence_generated": True,
                                            **({"plugin_action": actions[index+1]} if index+1 in actions else {}), **origin})
            schedule.add(block)
            self.fact("schedule.created", block.to_dict())
        self.agent.runtime.plan_store.save_schedule(schedule)
        self.fact("lifecycle.plan_committed", {"activity_root": cycle, "steps": steps, **origin})
        return cycle

    def _close_parents(self, parent_id):
        while parent_id:
            parent = self.agent.activities.get(parent_id)
            children = [self.agent.activities.get(child) for child in parent.children]
            if not children or any(c.status not in {"completed", "cancelled"} for c in children):
                break
            if any(c.status == "cancelled" for c in children):
                self.agent.cancel_activity(parent_id, "子活动存在未完成的阻塞；不标为全部完成")
                self._cancel_schedule(parent_id)
                for block in self.agent.runtime.schedule.all():
                    if block.metadata.get("activity_root_id") == parent_id:
                        goal = self.agent.runtime.goals.get(block.goal_id)
                        while goal:
                            goal.status = GoalStatus.BLOCKED
                            self.fact("goal.blocked", {"goal_id": goal.id, "activity_root": parent_id})
                            goal = self.agent.runtime.goals.get(goal.parent_id) if goal.parent_id else None
                        self.agent.runtime.plan_store.save_goals(self.agent.runtime.goals)
                parent_id = parent.parent_activity
                continue
            self._complete_record(parent_id, "全部子活动均已完成：" + ", ".join(c.activity_id for c in children))
            for block in self.agent.runtime.schedule.all():
                if block.metadata.get("activity_root_id") == parent_id:
                    goal = self.agent.runtime.goals.get(block.goal_id)
                    while goal:
                        self.fact("goal.completed", {"goal_id": goal.id, "activity_root": parent_id})
                        self.agent.runtime.state.completed_goal_ids.add(goal.id)
                        goal.complete()
                        goal = self.agent.runtime.goals.get(goal.parent_id) if goal.parent_id else None
                    self.agent.runtime.plan_store.save_goals(self.agent.runtime.goals)
                    self.agent.runtime._persist_projection()
            parent_id = parent.parent_activity

    def _complete_record(self, activity_id, record):
        self.agent.record_agent(record, kind="execution_result", activity_id=activity_id)
        activity = self.agent.activities.get(activity_id)
        records = [r for r in self.agent.raw.agent.records() if r.activity_id == activity_id]
        activity.raw_pointer = RawPointer("agent", records[0].record_id, records[-1].record_id, activity_id=activity_id)
        self.agent.activities._put(activity)
        self.agent.complete_activity(activity_id)

    def run(self):
        with self.agent.lock:
            self.fact("lifecycle.started", {"host": self.host.name})
            self.save(phase="waiting", last_error=None)
        try:
            while not self.stop_event.is_set():
                self.pulse.wait()
                self.pulse.clear()
                if self.stop_event.is_set():
                    break
                try:
                    self.tick()
                except Interrupted as exc:
                    with self.agent.lock:
                        self._checkpoint_active(str(exc))
                        self.fact("lifecycle.step_interrupted", {"reason": str(exc)})
                        self.save(phase="waiting" if self.state.get("enabled", True) else "paused")
                except Exception as exc:
                    with self.agent.lock:
                        failures = self.state.get("failures", 0) + 1
                        failed_id = self.state.get("activity_id")
                        if failed_id:
                            failed = self.agent.activities.get(failed_id)
                            if failed.status not in {"completed", "cancelled"}:
                                self.agent.cancel_activity(failed_id, str(exc))
                                self._cancel_schedule(failed_id)
                                self._close_parents(failed.parent_activity)
                        self.fact("lifecycle.step_failed", {"error": str(exc)})
                        self.save(phase="waiting", activity_id=None, last_error=f"{type(exc).__name__}: {exc}",
                                  failures=failures, next_wake_at=iso(self.agent.clock.now()))
                        # The deterministic heartbeat reopens the channel; no policy backoff.
        finally:
            with self.agent.lock:
                self._checkpoint_active("autonomous executor stopped")
                self.fact("lifecycle.stopped", {"reason": "service shutdown"})
                self.save(phase="stopped")

    def _checkpoint_active(self, reason):
        if self.state.get("control_window_id"):
            self._ensure_window(self.state["control_window_id"], self.agent.clock.now())
            self.save(control_window_id=None)
        for activity in self.agent.activities.all():
            if activity.status == "active":
                self._ensure_window(activity.activity_id, self.agent.clock.now())
                self.agent.interrupt_activity(activity.activity_id, reason)
