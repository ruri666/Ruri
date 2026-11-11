"""Observable empty-result feedback and bounded scheduler waits; never grade prose."""
import hashlib
import json

from .exploration import exchanges, settings


def input_signature(agent):
    selected = settings(agent.root)
    signals = {"exploration": selected,
               "dialogue_refs": [v["source_refs"] for v in exchanges(agent.raw, selected["conversation_id"])],
               "files": {}}
    for name in ("memory/user_core_memory.md", "memory/core_memory.md", "persona.json", "config/activity-plugins.json"):
        path = agent.root / name
        signals["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
    return hashlib.sha256(json.dumps(signals, sort_keys=True).encode()).hexdigest()


def feedback(events):
    results = [e for e in events if e.event_type == "lifecycle.artifact_committed"][-10:]
    calls = {e.payload.get("parent_activity_id") or e.payload.get("activity_id")
             for e in events if e.event_type == "lifecycle.plugin_completed"}
    rows = [{"activity_id": e.payload["activity_id"], "source_ref": "timeline:" + e.id,
             "body_present": e.payload.get("body_present", True),
             "file_count": len(e.payload.get("deliverables", [])),
             "tool_receipt_present": e.payload["activity_id"] in calls,
             "input_signature": e.payload.get("input_signature")} for e in results]
    streak = 0
    for row in reversed(rows):
        if row["body_present"] or row["file_count"] or row["tool_receipt_present"]:
            break
        streak += 1
    return {"recent_results": rows, "consecutive_no_material_results": streak,
            "interpretation": "只统计正文、文件和实际工具回执是否存在，不评价思想、休息或内容价值。重复空结果不代表用户要求停止探索。"}


def metadata_only_activities(events, activities):
    """Do not let completed empty records occupy the bounded current-work view."""
    calls = {e.payload.get("parent_activity_id") or e.payload.get("activity_id")
             for e in events if e.event_type == "lifecycle.plugin_completed"}
    empty = {e.payload["activity_id"] for e in events if e.event_type == "lifecycle.artifact_committed"
             and e.payload.get("body_present") is False and not e.payload.get("deliverables")
             and e.payload["activity_id"] not in calls}
    changed = True
    while changed:
        changed = False
        for activity in activities:
            if activity.status == "completed" and activity.children and all(child in empty for child in activity.children) and activity.activity_id not in empty:
                empty.add(activity.activity_id)
                changed = True
    return empty


def decision(result):
    value = result.get("planning_decision", {"action": "act", "wait_seconds": 0, "reason": ""})
    if not isinstance(value, dict) or set(value) != {"action", "wait_seconds", "reason"}:
        raise ValueError("Invalid planning_decision")
    if value["action"] not in {"act", "wait"} or type(value["wait_seconds"]) is not int or not isinstance(value["reason"], str):
        raise ValueError("Invalid planning_decision fields")
    if value["action"] == "act":
        if value["wait_seconds"] != 0:
            raise ValueError("Act decision must use wait_seconds=0")
    else:
        if not 300 <= value["wait_seconds"] <= 86400 or not value["reason"].strip():
            raise ValueError("Wait requires a reason and 300..86400 seconds")
        if any(result.get(k) for k in ("name", "goal", "body", "reply", "steps", "discoveries", "plugin_actions", "deliverables", "tool_calls")):
            raise ValueError("Wait cannot also propose activities, outputs or tool calls")
    return value
