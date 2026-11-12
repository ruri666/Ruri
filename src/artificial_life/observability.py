"""User-visible projections of the four life structures; raw history stays intact."""
from dataclasses import asdict
from datetime import timezone, timedelta

from .models import iso
from .storage import atomic_write_json, atomic_write_text, read_json

LOCAL = timezone(timedelta(hours=8))


def structures(service):
    agent = service.agent
    def memory(tree):
        return [dict(node_id=n.node_id, parent_id=n.parent_id, title=n.title, status=n.status,
                     summary=n.final_summary or n.live_summary or n.summary,
                     source_refs=n.metadata.get("summary_source_refs", []),
                     raw_pointer=n.raw_pointer.to_dict() if n.raw_pointer else None) for n in tree.nodes.values()]
    activities = [dict(node_id=a.activity_id, parent_id=a.parent_activity, title=a.name, status=a.status,
                       goal=a.goal, activity_profile=a.metadata.get("activity_profile"), actual_start=iso(a.actual_start_time), actual_end=iso(a.actual_end_time),
                       segments=[s.to_dict() for s in a.execution_segments]) for a in agent.activities.all()]
    activity_status = {a["node_id"]: a["status"] for a in activities}
    activity_map = {a["node_id"]: a for a in activities}
    schedule = []
    groups = set()
    # Schedule timeline only contains concrete activities, completely decoupled from heartbeat/control windows
    blocks = [b for b in agent.runtime.schedule.all()
              if not b.id.startswith("control-")
              and not b.metadata.get("lifecycle_control")
              and b.activity_type not in {"plan", "route"}]
    ids = {b.id for b in blocks}
    for block in blocks:
        local = block.start_at.astimezone(LOCAL)
        parent = None
        for node_id, title in [(f"year:{local.year}", str(local.year)),
                               (f"month:{local:%Y-%m}", f"{local:%Y-%m}"),
                               (f"day:{local:%Y-%m-%d}", f"{local:%Y-%m-%d}")]:
            if node_id not in groups:
                schedule.append(dict(node_id=node_id, parent_id=parent, title=title, status="calendar"))
                groups.add(node_id)
            parent = node_id
        planned_parent = block.metadata.get("parent_schedule_id")
        if planned_parent in ids:
            parent = f"schedule:{planned_parent}"
        activity_id = block.metadata.get("activity_id") or block.metadata.get("activity_root_id")
        act = activity_map.get(activity_id)
        title = act["title"] if (act and act.get("title")) else block.activity_type
        schedule.append(dict(node_id=f"schedule:{block.id}", parent_id=parent, title=title,
                             status=block.status.value, start=iso(block.start_at), end=iso(block.end_at),
                             activity_id=activity_id, execution_status=activity_status.get(activity_id, "not_started"),
                             goal_id=block.goal_id, activity_profile=block.metadata.get("activity_profile"),
                             route_selected_at=block.metadata.get("route_selected_at"), route_reason=block.metadata.get("route_reason")))
    lifecycle = service.lifecycle.status() if service.lifecycle else read_json(agent.root / "state/lifecycle.json", {})
    timers = {key: timer.to_dict() for key, timer in agent.runtime.timer_store.load().items()}
    beats = [e.to_dict() for e in agent.runtime.timeline.events() if e.event_type == "heartbeat.completed"][-10:]
    return dict(schema_version=1, updated_at=iso(agent.clock.now()), root=str(agent.root),
                memory_tree=dict(activity=memory(agent.activities.tree), temporal=memory(agent.temporal.tree)),
                activity_tree=activities, schedule_tree=schedule,
                heartbeat=dict(running=service.runner.running, last_error=service.runner.last_error,
                               timers=timers, recent=beats, lifecycle=lifecycle))


def export_structures(service):
    data = structures(service)
    directory = service.agent.root / "trees"
    atomic_write_json(directory / "overview.json", data)
    atomic_write_json(service.agent.root / "plans/schedule_tree.json", {"derived_from": "schedule.json", "nodes": data["schedule_tree"]})
    for filename, title, nodes in [
        ("memory_activity_tree.md", "记忆树 · 活动层次", data["memory_tree"]["activity"]),
        ("memory_temporal_tree.md", "记忆树 · 时间层次", data["memory_tree"]["temporal"]),
        ("activity_tree.md", "活动树", data["activity_tree"]),
        ("schedule_tree.md", "日程树 · Asia/Shanghai", data["schedule_tree"]),
    ]:
        lines = [f"# {title}", "", "由核心数据生成；不是独立事实源。", ""]
        children = {}
        for node in nodes:
            children.setdefault(node["parent_id"], []).append(node)
        def render(parent, depth=0):
            for n in children.get(parent, []):
                lines.append("  " * depth + f"- {n['title']} [{n['status']}] ({n['node_id']})")
                for key in ("summary", "start", "end", "actual_start", "actual_end", "source_refs", "raw_pointer", "execution_status", "activity_profile", "route_selected_at", "route_reason"):
                    if n.get(key):
                        lines.append("  " * (depth+1) + f"{key}: {n[key]}")
                render(n["node_id"], depth+1)
        render(None)
        atomic_write_text(directory / filename, "\n".join(lines)+"\n")
    import json
    atomic_write_text(directory / "heartbeat.md", "# Heartbeat 与生命周期\n\n```json\n" + json.dumps(data["heartbeat"], ensure_ascii=False, indent=2) + "\n```\n")
    return data
