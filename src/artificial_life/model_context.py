"""Small model-facing projections; canonical plans and facts stay on disk."""
from copy import deepcopy

from .context import excerpt


def plan_branches(values, *, parent_key, limit=3):
    """Selected open branches in ancestor-first order, without saved metadata."""
    by_id = {v["id"]: v for v in values}
    parents = {v.get(parent_key) for v in values}
    leaves = [v for v in values if v["id"] not in parents]
    selected = []
    seen = set()

    def add(value):
        if value["id"] in seen:
            return
        seen.add(value["id"])
        parent = by_id.get(value.get(parent_key))
        if parent:
            add(parent)
        selected.append(value)

    for leaf in leaves[:limit]:
        add(leaf)
    return selected


def prepare_model_request(request):
    """Do not send full saved plans, accumulated IDs or old artifact bodies."""
    result = deepcopy(request)
    base = result.get("context", {}).get("base", {})
    if "goals" in base:
        goals = [{k: v.get(k) for k in ("id", "parent_id", "title", "status")}
                 for v in base["goals"] if v.get("status") in {"active", "proposed"}]
        base["goals"] = plan_branches(goals, parent_key="parent_id")
        for goal in base["goals"]:
            goal["title"] = excerpt(goal["title"] or "", 160)
            goal["source_ref"] = "goal:" + goal["id"]
    if "schedule" in base:
        schedules = [{**{k: v.get(k) for k in ("id", "goal_id", "start_at", "end_at", "activity_type", "status")},
                      "parent_schedule_id": v.get("parent_schedule_id") or v.get("metadata", {}).get("parent_schedule_id")}
                     for v in base["schedule"] if v.get("status") == "planned"]
        schedules.sort(key=lambda v: (v["start_at"] or "", v["id"]))
        base["schedule"] = plan_branches(schedules, parent_key="parent_schedule_id")
        for block in base["schedule"]:
            block["activity_type"] = excerpt(block["activity_type"] or "", 160)
            block["source_ref"] = "schedule:" + block["id"]
    # Continuity comes from the memory tree and owner-scoped work bridge, not
    # whichever unrelated document happens to have the latest modification time.
    result.pop("recent_work", None)
    # Historical tool bodies are not this step's observations. Explicit current
    # dependencies and results from tools requested in this turn remain intact.
    result.pop("recent_plugin_results", None)
    for key in ("plugin_capabilities", "available_capabilities"):
        if key in result:
            result[key] = [{"id": v["id"]} for v in result[key]]
    if "execution_feedback" in result:
        result["execution_feedback"]["recent_results"] = result["execution_feedback"].get("recent_results", [])[-2:]
    return result
