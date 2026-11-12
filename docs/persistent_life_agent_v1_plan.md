# Persistent Life Agent V1 Plan

Status: implementation plan for the V1 Goal  
Date: 2026-08-18  
Persona: 轻小说作家

## Scope

V1 closes this loop without a vector database or external model dependency:

```text
Clock -> Heartbeat -> deterministic planning/activity execution
      -> append-only Raw Dialogue/Timeline
      -> file-backed Activity/Temporal/Conversation summaries
      -> Self/User Context
      -> coarse-to-fine textual retrieval -> next planning/input
```

The implementation extends the existing Phase 1 runtime. Existing Goal,
Schedule, State, Timer, Timeline, and TestClock contracts remain valid. New
memory and context services are optional adapters around that kernel.

## Module boundaries

```text
src/artificial_life/
  models.py             # existing Phase 1 value objects
  runtime.py            # existing deterministic heartbeat kernel
  timeline.py           # existing checksummed L0 fact log
  memory.py             # Raw Dialogue append-only stores and raw pointers
  text_tree.py          # one-file Markdown tree parser/renderer
  activity_life.py      # dynamic Activity tree and Execution Segments
  summaries.py          # incremental/live/final summary lifecycle
  temporal_memory.py    # Year -> Month -> Week -> Day tree maintenance
  conversation.py       # raw conversation episodes and idle close
  core_memory.py        # core_memory.md and user_core_memory.md
  retrieval.py          # router, tree navigation, raw fallback
  context.py            # Self Context and User Context builders
  llm.py                # provider protocol and deterministic MockLLMProvider
  persistent_agent.py   # V1 facade: lifecycle + memory + context
  random_events.py      # seeded optional random-event hook, disabled by default
```

Rules:

- `runtime.py` owns deterministic time, state, timers, and the original Raw Timeline.
- `memory.py` owns the complete raw dialogue streams; raw records are never edited.
- `text_tree.py` owns serialization only. Each memory type has exactly one Markdown file.
- Summary services can create derived nodes but cannot create historical facts.
- `persistent_agent.py` composes services; it does not become a second source of truth.
- No module imports a vector database, embedding index, or provider SDK; the
  optional local `webui.py` adapter uses only the standard library.

## Filesystem contract

```text
<agent_root>/
  manifest.json
  config/runtime.json
  plans/goals.json
  plans/schedule.json
  timeline/YYYY/MM/YYYY-MM-DD.jsonl       # existing append-only facts
  state/state.json
  state/activities.json
  memory/
    core_memory.md
    user_core_memory.md
    activity_memory.md
    temporal_memory.md
    conversation_memory.md
    agent_raw_dialogue.jsonl
    user_raw_dialogue.jsonl
```

The three tree files are not directories and not indexes. They use a stable,
human-readable block format:

```markdown
# Activity Memory
<!-- tree_type:activity schema_version:1 -->

## Node: activity:novel
Title: 写小说
Parent: null
Status: active
Summary: ...
Live Summary: ...
Final Summary:
Raw: agent_raw_dialogue.jsonl;start_record=ar_000001;end_record=ar_000008
Meta: {"goal_id":"goal-small"}
```

Node IDs, parent IDs, statuses, summaries, and Raw pointers are mandatory
machine-readable fields. Headings are rendered from tree depth; parent links
remain authoritative. Save is atomic and rewrites only the derived tree file,
never the raw JSONL.

## Raw record schema

```json
{
  "schema_version": 1,
  "record_id": "ar_000001",
  "stream": "agent",
  "recorded_at": "2026-08-18T01:00:00Z",
  "kind": "thought | dialogue | execution | system",
  "role": "agent | user | system",
  "text": "...",
  "activity_id": "activity:novel",
  "conversation_id": null,
  "timeline_seq": 42,
  "metadata": {}
}
```

Raw stores support lookup by record ID, inclusive record range, and UTC time
range. `raw_range` in every summary is a pointer, not copied prose.

## Activity model

An Activity is a semantic node. An Execution Segment is one contiguous run.
Interrupting an activity closes its segment and changes the same node to
`interrupted`; it never creates a replacement child solely because of the
interruption.

```text
ActivityNode:
  activity_id, name, goal, parent_activity_id
  planned_start_time, expected_end_time
  actual_start_time, actual_end_time
  status: planned | active | interrupted | completed | cancelled
  summary, live_summary, final_summary
  children, execution_segments

ExecutionSegment:
  segment_id, activity_id, started_at, ended_at
  status: active | completed | interrupted
  interruption_reason
```

Candidate planning is deterministic before any LLM call:

```text
unfinished activities + scheduled activities
+ active parent next steps + optional LLM proposals
```

## Summary lifecycle

- Child completion finalizes the child and updates only its direct parent's Live Summary.
- A still-active parent never gets an ancestor-wide rewrite.
- Parent completion finalizes from direct child summaries, then updates only its direct parent.
- Temporal Day/Week/Month/Year nodes use the same Live/Final distinction at their own time scale.
- Conversation episodes close after a configurable idle timeout and point to raw user/agent ranges.
- The default V1 summarizer is deterministic; `MockLLMProvider` can supply typed summaries in tests.
- Heartbeat raw synchronization records day evidence without a provider call;
  activity finalization, conversation closure, and explicit planning are the
  provider boundaries.

## Retrieval

`MemoryRouter` classifies `activity`, `temporal`, `conversation`, or `mixed` using
deterministic lexical/time signals. `FileTreeRetriever` then:

1. scores each node locally and propagates discounted evidence to its ancestors;
2. selects winning coarse root/period branches;
3. expands only those branches toward fine event nodes;
4. follows each node's Raw pointer when the Summary is missing/insufficient;
5. returns source IDs and raw records for audit.

Mixed retrieval runs the relevant trees independently and combines results;
the context/LLM layer may merge language, but never merges source identity.

## Context contracts

Self Context contains Core Memory, Current State, complete active Activity Path,
Activity Plan Snapshot, recent sibling Activity summaries, Temporal Active Path,
recent Temporal summaries, bounded current-activity Raw Agent Dialogue, and
retrieved evidence.

User Context contains the shared Base Context plus User Core Memory,
Conversation Timeline Path, recent Conversation Episodes, and bounded Raw User
Conversation. Activity and dialogue working sections remain separate.

All context builders use a character/token budget and expose a `source_refs`
list. They never promote the full context back into memory.

## Lifecycle flow

```text
heartbeat(now)
  -> recover elapsed time and deterministic state
  -> inspect active Activity expected_end_time
  -> close/interrupt Execution Segment when needed
  -> create planned/unfinished candidate set
  -> optionally ask provider for a typed plan proposal
  -> validate and start/resume one Activity
  -> append execution/transition Raw records
  -> update Activity + Temporal Live Summaries
  -> close idle Conversation Episodes
  -> persist trees atomically
  -> build Self/User Context on demand
```

Random-event hooks run at a deterministic heartbeat boundary with an explicit
seed and probability. They are disabled by default, cannot mutate history
directly, and when enabled create a standalone Activity after interrupting the
current one.

## Test plan

- Raw append-only JSONL: IDs, ranges, time queries, checksum/corruption handling.
- Text tree: save/load, parent/child nesting, stable diff-friendly output.
- Activity: arbitrary-depth tree, active path, execution segments, interrupt/resume,
  completion, unfinished candidate inclusion, direct-parent-only summary updates.
- Temporal: date path, live daily updates, daily finalization and parent updates.
- Conversation: idle timeout, episode raw range, conversation timeline path.
- Context: all required self/user components, budgets, active path and raw bounds.
- Retrieval: activity, day, conversation, mixed, and summary-to-raw fallback.
- Heartbeat: no-input progress, expected-end handling, optional seeded random hook,
  and deterministic replay.
- Demo: wake -> write novel -> illness interrupt -> rest -> replan -> resume/defer,
  plus user message using User Context.
