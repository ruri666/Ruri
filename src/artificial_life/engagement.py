"""Structural continuous-engagement contract; topics always belong to the model."""
from uuid import uuid4

from .adaptive_execution import run_adaptive
from .storage import atomic_write_json


def planning_decision(result):
    value = result.get("planning_decision", {"action": "act", "duration_seconds": 0, "reason": ""})
    if not isinstance(value, dict):
        raise ValueError("planning_decision must be an object")
    # Read legacy act proposals; global waits are no longer executable choices.
    action = value.get("action")
    duration = value.get("duration_seconds", value.get("wait_seconds", 0))
    if action != "act" or type(duration) is not int or not isinstance(value.get("reason", ""), str):
        raise ValueError("Choose a concrete activity; rest and wait are unavailable")
    if not isinstance(result.get("name", ""), str) or not isinstance(result.get("goal", ""), str) or not (result.get("name", "").strip() or result.get("goal", "").strip()):
        raise ValueError("An activity needs a model-chosen name or goal")
    if duration != 0:
        raise ValueError("Ordinary activity uses duration_seconds=0")
    profile = result.get("activity_profile") or {}
    if isinstance(profile, dict) and profile.get("mode") == "rest":
        raise ValueError("Use leisure for a real recreational activity")
    return {"action": action, "duration_seconds": duration, "reason": value.get("reason", "")}


def plan(host, service, request, cancel, plugins):
    """At most one structural repair; never use a second model as a prose grader."""
    request = dict(request)
    folder = service.agent.root / "runtime/planning-proposals" / uuid4().hex
    for attempt in range(2):
        result = run_adaptive(host, service, request, cancel, plugins=plugins)
        atomic_write_json(folder / f"{attempt}-proposal.json", result)
        try:
            planning_decision(result)
            return result
        except ValueError as exc:
            atomic_write_json(folder / f"{attempt}-rejection.json", {"error": str(exc)})
            if attempt:
                raise
            request["planning_protocol_error"] = str(exc)
            request["rejected_proposal"] = result
            request["repair_instruction"] = "保持自主生活：由你选择下一件具体活动。需要恢复精力时，选与用户兴趣相关的实际娱乐，并将 activity_profile.mode 设为 leisure。只修正协议，不要求写报告或选择固定主题。"
