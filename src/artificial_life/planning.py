"""Typed behavior proposals and deterministic validation/application rules."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .models import as_utc, iso


PLANNING_ACTIONS = {
    "start_activity",
    "resume_activity",
    "create_activity",
    "reserved_free",
}


@dataclass(frozen=True)
class PlanningProposal:
    """An LLM proposal which has not yet become a historical fact."""

    action: str
    activity_id: str | None = None
    name: str | None = None
    goal: str = ""
    parent_activity: str | None = None
    schedule_id: str | None = None
    expected_duration_seconds: int | None = None
    reason: str = ""
    source_refs: tuple[str, ...] = ()
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"unsupported planning proposal schema_version={self.schema_version}")
        if self.action not in PLANNING_ACTIONS:
            raise ValueError(f"unsupported planning action: {self.action}")
        if self.expected_duration_seconds is not None and self.expected_duration_seconds <= 0:
            raise ValueError("expected_duration_seconds must be positive")

    @classmethod
    def from_result(cls, result: Any) -> "PlanningProposal":
        """Parse the provider result without allowing arbitrary fields through."""

        value = result.structured.get("proposal") if getattr(result, "structured", None) else None
        if not isinstance(value, dict):
            value = result.structured.get("selected") if getattr(result, "structured", None) else None
        if not isinstance(value, dict):
            value = _parse_json_object(getattr(result, "text", ""))
        if not isinstance(value, dict):
            raise ValueError("planning provider returned no JSON proposal")
        if "action" not in value:
            # Backward-compatible normalization for the original Mock
            # provider, whose result selected a candidate object directly.
            if value.get("candidate_kind") == "schedule_context" or value.get("status") == "scheduled":
                value = {**value, "action": "start_activity"}
            elif value.get("status") == "interrupted":
                value = {**value, "action": "resume_activity"}
        return cls(
            schema_version=value.get("schema_version", 1),
            action=str(value.get("action", "")),
            activity_id=_optional_string(value.get("activity_id")),
            name=_optional_string(value.get("name")),
            goal=str(value.get("goal", "")),
            parent_activity=_optional_string(value.get("parent_activity")),
            schedule_id=_optional_string(value.get("schedule_id")),
            expected_duration_seconds=_optional_int(value.get("expected_duration_seconds")),
            reason=str(value.get("reason", "")),
            source_refs=tuple(str(item) for item in value.get("source_refs", []) if item),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "activity_id": self.activity_id,
            "name": self.name,
            "goal": self.goal,
            "parent_activity": self.parent_activity,
            "schedule_id": self.schedule_id,
            "expected_duration_seconds": self.expected_duration_seconds,
            "reason": self.reason,
            "source_refs": list(self.source_refs),
        }


@dataclass(frozen=True)
class PlanningDecision:
    candidates: tuple[dict[str, Any], ...]
    proposal: PlanningProposal | None
    accepted: bool
    reason: str
    provider: str
    errors: tuple[str, ...] = ()
    candidate_fingerprint: str = ""
    decided_at: datetime | None = None
    evidence_source_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "candidates": list(self.candidates),
            "proposal": self.proposal.to_dict() if self.proposal else None,
            "accepted": self.accepted,
            "reason": self.reason,
            "provider": self.provider,
            "errors": list(self.errors),
            "candidate_fingerprint": self.candidate_fingerprint,
            "decided_at": iso(self.decided_at),
            "evidence_source_refs": list(self.evidence_source_refs),
        }


class PlanningService:
    """Create candidates, validate LLM proposals, and apply accepted commands."""

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def candidates(self, now: datetime | None = None) -> list[dict[str, Any]]:
        moment = as_utc(now or self.agent.clock.now())
        candidates: list[dict[str, Any]] = []
        for activity in self.agent.activities.unfinished():
            if activity.status not in {"planned", "interrupted"}:
                continue
            candidates.append(
                {
                    "activity_id": activity.activity_id,
                    "name": activity.name,
                    "goal": activity.goal,
                    "parent_activity": activity.parent_activity,
                    "status": activity.status,
                    "schedule_id": activity.metadata.get("schedule_id"),
                    "planned_start_time": iso(activity.planned_start_time),
                    "expected_end_time": iso(activity.expected_end_time),
                    "available": activity.planned_start_time is None or activity.planned_start_time <= moment,
                    "resumable": activity.expected_end_time is None or moment < activity.expected_end_time,
                    "candidate_kind": "unfinished_activity",
                }
            )

        existing_schedule_ids = {
            activity.metadata.get("schedule_id")
            for activity in self.agent.activities.all()
            if activity.metadata.get("schedule_id")
        }
        for block in self.agent.runtime.schedule.all():
            if block.status.value != "planned" or block.id in existing_schedule_ids:
                continue
            try:
                goal_active = self.agent.runtime.goals.active(block.goal_id)
            except KeyError:
                goal_active = False
            if not goal_active:
                continue
            candidates.append(
                {
                    "activity_id": f"schedule:{block.id}",
                    "schedule_id": block.id,
                    "name": block.activity_type,
                    "goal_id": block.goal_id,
                    "status": "scheduled",
                    "planned_start": iso(block.start_at),
                    "planned_end": iso(block.end_at),
                    "available": block.contains(moment),
                    "expired": moment >= block.end_at,
                    "candidate_kind": "schedule_context",
                }
            )
        candidates.sort(key=lambda item: (0 if item.get("status") == "interrupted" else 1, str(item.get("activity_id"))))
        return candidates

    @staticmethod
    def fingerprint(candidates: list[dict[str, Any]]) -> str:
        payload = json.dumps(candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def decide(
        self,
        context: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        reason: str,
        at: datetime | None = None,
    ) -> PlanningDecision:
        moment = as_utc(at or self.agent.clock.now())
        fingerprint = self.fingerprint(candidates)
        provider_name = str(getattr(self.agent.provider, "provider_name", type(self.agent.provider).__name__))
        if not candidates:
            return PlanningDecision(
                (),
                None,
                False,
                "no_candidates",
                provider_name,
                candidate_fingerprint=fingerprint,
                decided_at=moment,
                evidence_source_refs=tuple(str(ref) for ref in context.get("source_refs", ())),
            )
        errors: list[str] = []
        proposal: PlanningProposal | None = None
        try:
            result = self.agent.provider.plan(context, candidates)
            proposal = PlanningProposal.from_result(result)
            errors.extend(self.validate(proposal, candidates))
            provider_name = str(getattr(result, "provider", provider_name))
        except Exception as exc:  # provider failure must not stop deterministic life progression
            errors.append(f"planning provider failed: {exc}")
        return PlanningDecision(
            candidates=tuple(candidates),
            proposal=proposal,
            accepted=not errors and proposal is not None,
            reason=reason,
            provider=provider_name,
            errors=tuple(errors),
            candidate_fingerprint=fingerprint,
            decided_at=moment,
            evidence_source_refs=tuple(str(ref) for ref in context.get("source_refs", ())),
        )

    def validate(self, proposal: PlanningProposal, candidates: list[dict[str, Any]]) -> list[str]:
        errors: list[str] = []
        matching = [
            item
            for item in candidates
            if (proposal.activity_id is not None and item.get("activity_id") == proposal.activity_id)
            or (proposal.schedule_id is not None and item.get("schedule_id") == proposal.schedule_id)
        ]
        if proposal.action == "reserved_free":
            return errors
        if proposal.action != "create_activity" and not matching:
            errors.append("proposal does not reference a current candidate")
            return errors
        if proposal.action in {"start_activity", "resume_activity"}:
            candidate = matching[0]
            if proposal.action == "start_activity" and candidate.get("status") not in {"planned", "scheduled"}:
                errors.append(f"cannot start candidate in status {candidate.get('status')}")
            if proposal.action == "start_activity" and candidate.get("candidate_kind") == "schedule_context" and candidate.get("available") is not True:
                errors.append("schedule candidate is outside its planned time window")
            if proposal.action == "start_activity" and candidate.get("available") is False:
                errors.append("activity is not yet available at the planning time")
            if proposal.action == "resume_activity" and candidate.get("status") != "interrupted":
                errors.append(f"cannot resume candidate in status {candidate.get('status')}")
            if proposal.action == "resume_activity" and candidate.get("resumable") is False:
                errors.append("candidate reached its expected end and requires a new plan")
        if proposal.action == "create_activity" and not proposal.name:
            errors.append("created activity requires a name")
        if proposal.parent_activity:
            try:
                parent = self.agent.activities.get(proposal.parent_activity)
                if parent.status in {"completed", "cancelled"}:
                    errors.append("cannot create an activity below a closed parent")
            except (KeyError, ValueError):
                errors.append("parent_activity does not exist")
        return errors

    def apply(self, decision: PlanningDecision) -> Any:
        """Apply only a previously validated proposal through Agent services."""

        if not decision.accepted or decision.proposal is None:
            return None
        proposal = decision.proposal
        moment = decision.decided_at or self.agent.clock.now()
        if proposal.action == "reserved_free":
            return None
        if proposal.action == "create_activity":
            activity_id = proposal.activity_id or f"planned:{moment:%Y%m%dT%H%M%S}:{decision.candidate_fingerprint[:8]}"
            expected_end = (
                moment + timedelta(seconds=proposal.expected_duration_seconds)
                if proposal.expected_duration_seconds is not None
                else None
            )
            activity = self.agent.create_activity(
                activity_id=activity_id,
                name=proposal.name or "未命名活动",
                goal=proposal.goal,
                parent_activity=proposal.parent_activity,
                expected_end_time=expected_end,
                metadata={"schedule_id": proposal.schedule_id, "created_from": "llm_proposal"},
            )
            return self.agent.start_activity(activity.activity_id, moment)
        candidate = next(
            item
            for item in decision.candidates
            if (proposal.activity_id is not None and item.get("activity_id") == proposal.activity_id)
            or (proposal.schedule_id is not None and item.get("schedule_id") == proposal.schedule_id)
        )
        activity_id = str(candidate["activity_id"])
        if activity_id.startswith("schedule:"):
            block = self.agent.runtime.schedule.get(str(candidate["schedule_id"]))
            activity_id = f"act_{block.id}_{moment:%Y%m%dT%H%M%S}"
            activity = self.agent.create_activity(
                activity_id=activity_id,
                name=block.activity_type,
                goal=f"执行计划 {block.id}",
                planned_start_time=moment,
                expected_end_time=block.end_at,
                metadata={"schedule_id": block.id, "goal_id": block.goal_id, "created_from": "schedule_context"},
            )
        if proposal.action == "resume_activity":
            return self.agent.resume_activity(activity_id, moment)
        return self.agent.start_activity(activity_id, moment)


def _optional_string(value: Any) -> str | None:
    return None if value in (None, "") else str(value)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _parse_json_object(text: str) -> dict[str, Any] | None:
    value = text.strip()
    if value.startswith("```"):
        value = value.strip("`").strip()
        if value.startswith("json"):
            value = value[4:].strip()
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None
