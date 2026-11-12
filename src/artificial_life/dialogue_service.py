"""Unified dialogue orchestration and streaming transport events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterator

from .context import ContextBundle
from .memory import RawRecord
from .models import as_utc, iso


@dataclass(frozen=True)
class DialogueResponse:
    """Auditable result of one explicit user dialogue turn."""

    user: RawRecord
    agent: RawRecord
    provider: str
    context_source_refs: tuple[str, ...] = ()
    interrupted_activity_id: str | None = None
    checkpoint_id: str | None = None
    resumed_activity_id: str | None = None
    provider_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "user": self.user.to_dict(),
            "agent": self.agent.to_dict(),
            "provider": self.provider,
            "context_source_refs": list(self.context_source_refs),
            "interrupted_activity_id": self.interrupted_activity_id,
            "checkpoint_id": self.checkpoint_id,
            "resumed_activity_id": self.resumed_activity_id,
            "provider_error": self.provider_error,
        }


@dataclass(frozen=True)
class DialogueStreamEvent:
    """Versioned SSE event emitted while a response is being generated."""

    event: str
    data: dict[str, Any]
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "event": self.event, "data": self.data}

    def to_sse(self) -> bytes:
        payload = json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))
        return f"event: {self.event}\ndata: {payload}\n\n".encode("utf-8")


@dataclass(frozen=True)
class _PreparedTurn:
    moment: datetime
    conversation_id: str
    user: RawRecord
    context: ContextBundle
    checkpoint: dict[str, Any] | None
    interrupted_activity_id: str | None
    checkpoint_id: str | None


class DialogueService:
    """Single application service for synchronous and streaming dialogue.

    The service owns the dialogue transaction boundary: ingest the user input,
    build context, call the provider, and persist the assistant output without
    changing autonomous execution. Historical checkpoint readers remain for
    recovery of old turns. The
    injected agent remains the canonical writer for timeline/state facts.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    def respond(
        self,
        text: str,
        *,
        conversation_id: str = "default",
        at: datetime | None = None,
    ) -> DialogueResponse:
        prepared = self._validate(text, conversation_id, at)
        with self.agent.lock:
            turn = self._prepare(prepared[0], prepared[1], prepared[2])
        reply, provider, provider_error = self._generate(turn)
        with self.agent.lock:
            return self._finish(turn, reply, provider, provider_error)

    def stream(
        self,
        text: str,
        *,
        conversation_id: str = "default",
        at: datetime | None = None,
    ) -> Iterator[DialogueStreamEvent]:
        """Yield start/delta/done events and persist the final response.

        Only context capture and final commit hold the canonical writer lock.
        """

        raw_text, normalized_conversation, moment = self._validate(text, conversation_id, at)
        with self.agent.lock:
            turn = self._prepare(raw_text, normalized_conversation, moment)
        try:
            yield DialogueStreamEvent(
                "start",
                {
                    "conversation_id": turn.conversation_id,
                    "user": turn.user.to_dict(),
                    "interrupted_activity_id": turn.interrupted_activity_id,
                    "checkpoint_id": turn.checkpoint_id,
                },
            )

            chunks: list[str] = []
            provider = self._provider_name()
            provider_error: str | None = None
            try:
                if provider == "mock":
                    result = self.agent.provider.generate(
                        raw_text, purpose="dialogue_response", context=turn.context.to_dict()
                    )
                    reply = f"收到，我已记下：{raw_text}"
                    for chunk in _chunks(reply):
                        chunks.append(chunk)
                        yield DialogueStreamEvent("delta", {"text": chunk})
                else:
                    stream_method = getattr(self.agent.provider, "generate_stream", None)
                    if not callable(stream_method):
                        result = self.agent.provider.generate(
                            raw_text, purpose="dialogue_response", context=turn.context.to_dict()
                        )
                        reply = result.text.strip()
                        if not reply:
                            raise RuntimeError("provider returned an empty response")
                        for chunk in _chunks(reply):
                            chunks.append(chunk)
                            yield DialogueStreamEvent("delta", {"text": chunk})
                    else:
                        for chunk in stream_method(
                            raw_text, purpose="dialogue_response", context=turn.context.to_dict()
                        ):
                            if chunk:
                                text_chunk = str(chunk)
                                chunks.append(text_chunk)
                                yield DialogueStreamEvent("delta", {"text": text_chunk})
                        reply = "".join(chunks).strip()
                        if not reply:
                            raise RuntimeError("provider returned an empty response")
            except Exception as exc:  # keep the canonical turn durable on provider failure
                provider_error = str(exc)
                reply = "我现在暂时无法生成完整回复，但我已经记下了这句话。"
                chunks.clear()
                for chunk in _chunks(reply):
                    chunks.append(chunk)
                    yield DialogueStreamEvent("delta", {"text": chunk})

            with self.agent.lock:
                response = self._finish(turn, reply, provider, provider_error)
            yield DialogueStreamEvent("done", {"response": response.to_dict()})
        finally:
            if turn.checkpoint and self.agent.activities.get(turn.checkpoint["activity_id"]).status == "interrupted":
                self.agent._append_dialogue_fact("dialogue.cancelled", turn.moment, {"conversation_id": turn.conversation_id, "checkpoint_id": turn.checkpoint_id})
                self.agent._resume_dialogue_checkpoint(turn.checkpoint, turn.moment)

    def _validate(
        self, text: str, conversation_id: str, at: datetime | None
    ) -> tuple[str, str, datetime]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("text must be a non-empty string")
        normalized_conversation = str(conversation_id).strip()
        if not normalized_conversation:
            raise ValueError("conversation_id cannot be empty")
        moment = as_utc(at or self.agent.clock.now())
        return text.strip(), normalized_conversation, moment

    def _prepare(self, text: str, conversation_id: str, moment: datetime) -> _PreparedTurn:
        # Dialogue does not advance autonomous heartbeat or checkpoint activities.
        # Each channel shares committed facts, not execution ownership.
        checkpoint = None
        interrupted_id = checkpoint.get("activity_id") if checkpoint else None
        checkpoint_id = checkpoint.get("checkpoint_id") if checkpoint else None
        user_record = self.agent.conversations.append_user(
            text,
            conversation_id=conversation_id,
            at=moment,
            activity_id=interrupted_id,
            metadata={"checkpoint_id": checkpoint_id} if checkpoint_id else None,
        )
        self.agent._sync_raw_memory()
        self.agent._append_dialogue_fact(
            "dialogue.message_received",
            moment,
            {"conversation_id": conversation_id, "record_id": user_record.record_id, "checkpoint_id": checkpoint_id},
        )
        context = self.agent.build_user_context(conversation_id=conversation_id, now=moment)
        return _PreparedTurn(moment, conversation_id, user_record, context, checkpoint, interrupted_id, checkpoint_id)

    def _generate(self, turn: _PreparedTurn) -> tuple[str, str, str | None]:
        provider_name = self._provider_name()
        provider_error: str | None = None
        try:
            result = self.agent.provider.generate(
                turn.user.text,
                purpose="dialogue_response",
                context=turn.context.to_dict(),
            )
            reply = result.text.strip()
            if not reply:
                raise RuntimeError("provider returned an empty response")
            if result.provider == "mock":
                reply = f"收到，我已记下：{turn.user.text}"
            provider_name = result.provider
        except Exception as exc:
            provider_error = str(exc)
            reply = "我现在暂时无法生成完整回复，但我已经记下了这句话。"
        return reply, provider_name, provider_error

    def _finish(
        self,
        turn: _PreparedTurn,
        reply: str,
        provider: str,
        provider_error: str | None,
    ) -> DialogueResponse:
        metadata: dict[str, Any] = {}
        metadata["reply_to_user_record_id"] = turn.user.record_id
        if turn.checkpoint_id:
            metadata["checkpoint_id"] = turn.checkpoint_id
        if provider_error:
            metadata["provider_error"] = provider_error
        agent_record = self.agent.conversations.append_agent(
            reply,
            conversation_id=turn.conversation_id,
            at=turn.moment,
            activity_id=turn.interrupted_activity_id,
            metadata=metadata or None,
        )
        self.agent._sync_raw_memory()
        self.agent._append_dialogue_fact(
            "dialogue.response_sent",
            turn.moment,
            {
                "conversation_id": turn.conversation_id,
                "record_id": agent_record.record_id,
                "provider": provider,
                "checkpoint_id": turn.checkpoint_id,
                "provider_error": provider_error,
            },
        )
        resumed_id = self.agent._resume_dialogue_checkpoint(turn.checkpoint, turn.moment) if turn.checkpoint else None
        return DialogueResponse(
            user=turn.user,
            agent=agent_record,
            provider=provider,
            context_source_refs=turn.context.source_refs,
            interrupted_activity_id=turn.interrupted_activity_id,
            checkpoint_id=turn.checkpoint_id,
            resumed_activity_id=resumed_id,
            provider_error=provider_error,
        )

    def _provider_name(self) -> str:
        return str(getattr(self.agent.provider, "provider_name", type(self.agent.provider).__name__.replace("Provider", "").lower()))


def _chunks(text: str, size: int = 24) -> Iterator[str]:
    for index in range(0, len(text), size):
        yield text[index : index + size]
