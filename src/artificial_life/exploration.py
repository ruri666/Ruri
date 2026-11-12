"""Workspace exploration motive and source-backed user-work context, without scheduling."""
import hashlib
import re
from pathlib import Path

from .storage import atomic_write_json, read_json


USER_RELEVANCE = (
    "工作与娱乐都从用户正在做的事或明确表达的兴趣出发。优先参考近期用户原话，"
    "需要更多线索时按需检索。娱乐可以一起探索用户感兴趣的玩法、作品或话题，不必产出工作成果。"
    "新计划用service_intent写明关联、预期价值和source_refs。把推测与事实分开；"
    "查询用户记忆时使用配置的conversation_id。没有新消息时，已表达的兴趣仍可作为线索。"
    "旧请求受阻时换一个有依据的方向，避免重复追问。activity_memory_index是历史索引。"
    "当前活动路径为空时，自主选择新方向。恢复精力时选择与用户兴趣相关的娱乐，例如探索相关视频。避免把历史活动当作当前目标。"
    "线索不足时可先查一次相关记忆或自然了解兴趣，避免编造关联。"
)


def settings(root):
    value = read_json(root / "config/exploration.json", {"mode": "persona", "conversation_id": "default"})
    if (value.get("mode") not in {"persona", "user_service"}
            or not isinstance(value.get("conversation_id"), str)
            or not re.fullmatch(r"[\w-]{1,100}", value["conversation_id"])):
        raise ValueError("Invalid exploration settings")
    return {"mode": value["mode"], "conversation_id": value["conversation_id"]}


def configure(agent, mode, conversation_id="default"):
    if mode not in {"persona", "user_service"} or not isinstance(conversation_id, str) or not re.fullmatch(r"[\w-]{1,100}", conversation_id):
        raise ValueError("Invalid exploration mode or conversation_id")
    before = settings(agent.root)
    after = {"mode": mode, "conversation_id": conversation_id}
    if before != after:
        atomic_write_json(agent.root / "config/exploration.json", after)
        agent._append_dialogue_fact("exploration.mode_changed", agent.clock.now(), {"before": before, "after": after})
    return after


def exchanges(raw, conversation_id):
    users = {r.record_id: r for r in raw.user.records() if r.conversation_id == conversation_id}
    result = []
    for reply in raw.agent.records():
        user = users.get(reply.metadata.get("reply_to_user_record_id"))
        if user is not None and reply.conversation_id == conversation_id:
            result.append({"user_text": user.text[:1800], "reply": reply.text[:600],
                           "recorded_at": user.recorded_at.isoformat(),
                           "truncated": len(user.text) > 1800 or len(reply.text) > 600,
                           "source_refs": [f"raw:user:{user.record_id}", f"raw:agent:{reply.record_id}"]})
    return result


def recent_results(root, events, conversation_id, limit=5):
    results = []
    for event in events:
        if event.event_type != "lifecycle.artifact_committed":
            continue
        payload = event.payload
        # Empty experience/metadata records remain Timeline facts, not a
        # deliverable to display or a substantive shared-work excerpt.
        if payload.get("body_present") is False and not payload.get("deliverables"):
            continue
        support = payload.get("user_service", {})
        if support.get("conversation_id") != conversation_id:
            continue
        artifact = Path(payload["path"])
        if not artifact.is_relative_to(root.resolve() / "works/autonomous") or not artifact.is_file():
            continue
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != payload["sha256"]:
            continue
        results.append({"activity_id": payload["activity_id"], "outcome": payload["outcome"],
                        "intent": support, "path": str(artifact.relative_to(root)),
                        "sha256": payload["sha256"], "excerpt": artifact.read_text(encoding="utf-8")[:1200],
                        "deliverables": payload.get("deliverables", []), "source_ref": "timeline:" + event.id})
    return results[-limit:]


def assess_intent(raw, conversation_id, value):
    """Validate provenance only. Need and benefit remain model hypotheses."""
    value = value if isinstance(value, dict) else {}
    refs = value.get("source_refs", [])
    refs = list(dict.fromkeys(r for r in refs if isinstance(r, str))) if isinstance(refs, list) else []
    available = {ref for item in exchanges(raw, conversation_id) for ref in item["source_refs"]}
    from .core_memory import CoreMemoryStore, DEFAULT_USER_CORE
    core = CoreMemoryStore(raw.root / "user_core_memory.md", title="User Core Memory", defaults=DEFAULT_USER_CORE)
    preferences = core.read()["Stable Preferences"]
    if preferences.strip():
        available.add("memory/user_core_memory.md#Stable Preferences")
    from .user_preferences import read_import
    for ref in refs:
        match = re.fullmatch(r"memory/preference_imports/([0-9a-f]{64})\.json#entries/(\d+)", ref)
        if match and f"memory/preference_imports/{match[1]}.json" in preferences:
            try:
                entries = read_import(raw.root.parent, match[1])
            except (OSError, ValueError, KeyError):
                continue
            if int(match[2]) < len(entries):
                available.add(ref)
    return {"conversation_id": conversation_id, "need": str(value.get("need", "")),
            "expected_help": str(value.get("expected_help", "")), "declared_source_refs": refs,
            "verified_source_refs": [r for r in refs if r in available],
            "unverified_source_refs": [r for r in refs if r not in available],
            "benefit_verified": False}
