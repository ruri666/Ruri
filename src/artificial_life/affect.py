"""Model-owned affect with deterministic, non-binding load suggestions."""
import math


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def profile(value=None):
    value = {"mode": "work", "effort": 0.5} if value is None else value
    value = value if isinstance(value, dict) else {}
    effort = value.get("effort", .5)
    if type(effort) not in {int, float} or not math.isfinite(effort):
        effort = .5
    return {"mode": str(value.get("mode", "work")), "effort": float(effort)}


def experience(value=None):
    if value is None:
        return {"enjoyment": 0.0}
    if not isinstance(value, dict):
        return {"enjoyment": 0.0}
    result = {k: float(v) for k, v in value.items() if k in {"enjoyment", "mood_valence", "fatigue", "energy"}
              and type(v) in {int, float} and math.isfinite(v)}
    result.setdefault("enjoyment", 0.0)
    if isinstance(value.get("emotion"), str) and value["emotion"]:
        result["emotion"] = value["emotion"]
    return result


def signature(state):
    return f"{state.fatigue}:{state.emotion}:{state.mood_valence}"


def routing_context(state):
    pressure = clamp(state.fatigue + max(0, -state.mood_valence)*.25)
    return {"signature": signature(state), "fatigue": state.fatigue, "energy": state.energy,
            "emotion": state.emotion, "mood_valence": state.mood_valence,
            "work_streak": state.work_streak, "recovery_attraction": pressure,
            "social_attraction": round(.2 + .6*abs(state.mood_valence), 3),
            "optional_modes": {"work": "follow interests and useful questions", "leisure": "actual enjoyable play/reading or other available capabilities",
                               "rest": "low effort recovery", "social": "optionally send the user a real message via proactive_message; low mood seeks connection, positive mood shares joy"},
            "preferred_effort_ceiling": round(1-pressure*.8, 3),
            "policy": "Model-owned choice; recovery/entertainment becomes more attractive with load, never a forced topic or activity."}


def suggested_load(state, activity_profile, elapsed_seconds):
    """Optional simulation guidance. Never applied as a mandatory state change."""
    mode, effort = activity_profile["mode"], activity_profile["effort"]
    hours = clamp(elapsed_seconds, 0, 21600)/3600
    delta = .035*effort + hours*.15*effort if mode == "work" else hours*(.15*effort-.45*(1-effort))
    fatigue = round(clamp(state.fatigue+delta), 8)
    return {"fatigue": fatigue, "energy": round(1-fatigue, 8), "binding": False}


def reduce_work(state, activity_profile, reported_experience, elapsed_seconds, blocked=False):
    """Keep unspecified self-state; no categorical emotion or success penalty."""
    mode = activity_profile["mode"]
    streak = state.work_streak+1 if mode == "work" else 0
    return {"fatigue": reported_experience.get("fatigue", state.fatigue),
            "energy": reported_experience.get("energy", state.energy),
            "mood_valence": reported_experience.get("mood_valence", state.mood_valence),
            "emotion": reported_experience.get("emotion", state.emotion), "work_streak": streak}


def commit_work(agent, activity_id, activity_profile, reported_experience, elapsed_seconds, blocked=False):
    # Idempotence comes from persisted source events, including after restart.
    if any(e.event_type == "affect.updated" and e.payload.get("activity_id") == activity_id for e in agent.runtime.timeline.events()):
        return
    after = reduce_work(agent.runtime.state, activity_profile, reported_experience, elapsed_seconds, blocked)
    agent._append_dialogue_fact("affect.updated", agent.clock.now(), {
        "activity_id": activity_id, "before": routing_context(agent.runtime.state), "after": after,
        "activity_profile": activity_profile, "self_reported_experience": reported_experience,
        "suggested_load": suggested_load(agent.runtime.state, activity_profile, elapsed_seconds),
        "elapsed_seconds": elapsed_seconds, "blocked": blocked})
    for key, value in after.items():
        setattr(agent.runtime.state, key, value)
    agent.runtime._persist_projection()
