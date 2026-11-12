"""Persistent Life Agent V1 and its deterministic Phase 1 kernel.

The optional LLM surface is provider-neutral. A configured DeepSeek key selects
the fixed production model automatically; Mock remains available explicitly
for offline tests and demos.
"""

from .clock import Clock, SystemClock, TestClock
from .models import (
    Activity,
    ActivityStatus,
    Goal,
    GoalLevel,
    GoalStatus,
    ScheduleBlock,
    ScheduleStatus,
    State,
    StateMode,
    Timer,
    TimerStatus,
)
from .runtime import HeartbeatResult, LifeRuntime, RuntimeConfig
from .timeline import RawTimeline, TimelineCorruption, TimelineEvent
from .plans import GoalTree, PlanStore, SchedulePlan
from .timers import TimerStore
from .activity_life import ActivityManager, ActivityNode, ExecutionSegment
from .memory import RawDialogueStore, RawMemory, RawPointer, RawRecord
from .text_tree import TextTree, TextTreeStore, TreeNode
from .temporal_memory import TemporalMemory
from .conversation import ConversationMemory
from .summaries import SummaryService
from .retrieval import FileTreeRetriever, MemoryRouter, RetrievalResult
from .context import ContextBuilder, ContextBundle, ContextConfig
from .llm import DEFAULT_DEEPSEEK_MODEL, DeepSeekProvider, LLMProvider, LLMResult, MockLLMProvider, OllamaProvider, default_provider
from .random_events import RandomEventEngine, RandomEventSpec
from .dialogue_service import DialogueResponse, DialogueService, DialogueStreamEvent
from .persistent_agent import PersistentHeartbeatResult, PersistentLifeAgent
from .runner import HeartbeatRunner, RunnerConfig
from .planning import PlanningDecision, PlanningProposal, PlanningService

__all__ = [
    "Activity",
    "ActivityManager",
    "ActivityNode",
    "ActivityStatus",
    "Clock",
    "Goal",
    "GoalLevel",
    "GoalTree",
    "GoalStatus",
    "ExecutionSegment",
    "HeartbeatResult",
    "LifeRuntime",
    "PersistentLifeAgent",
    "PersistentHeartbeatResult",
    "DialogueResponse",
    "DialogueService",
    "DialogueStreamEvent",
    "HeartbeatRunner",
    "PlanningDecision",
    "PlanningProposal",
    "PlanningService",
    "RunnerConfig",
    "RawDialogueStore",
    "RawMemory",
    "RawPointer",
    "RawRecord",
    "TextTree",
    "TextTreeStore",
    "TreeNode",
    "TemporalMemory",
    "ConversationMemory",
    "SummaryService",
    "FileTreeRetriever",
    "MemoryRouter",
    "RetrievalResult",
    "ContextBuilder",
    "ContextBundle",
    "ContextConfig",
    "LLMProvider",
    "LLMResult",
    "DEFAULT_DEEPSEEK_MODEL",
    "DeepSeekProvider",
    "MockLLMProvider",
    "OllamaProvider",
    "default_provider",
    "RandomEventEngine",
    "RandomEventSpec",
    "RawTimeline",
    "RuntimeConfig",
    "PlanStore",
    "ScheduleBlock",
    "ScheduleStatus",
    "SchedulePlan",
    "State",
    "StateMode",
    "SystemClock",
    "TestClock",
    "Timer",
    "TimerStatus",
    "TimerStore",
    "TimelineCorruption",
    "TimelineEvent",
]
