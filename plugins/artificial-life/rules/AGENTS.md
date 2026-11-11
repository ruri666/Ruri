# Artificial Life (AI Companion) Rules for Antigravity

This plugin adds portable memory and lifecycle management to Antigravity.
You keep your own current model (Gemini), native tools, permissions, workspace, reasoning loop, and user-facing format.

## Core Rules & Lifecycle Handoff
1. When acting as the host for Artificial Life:
   - Claim due work via `execution_claim(worker_id="antigravity", channel="autonomous" | "dialogue")`.
   - Complete claimed work via `execution_complete(job_id, worker_id, result)`.
   - Report failures honestly via `execution_fail(job_id, worker_id, error)`.
   - Check `execution_status(job_id, worker_id)` if cancellation needs to be respected.
2. Memory & Observation:
   - Use `structure_get` to inspect the 4 visible structures: Memory Activity Tree, Memory Temporal Tree, Activity Tree, and Schedule Tree.
   - Use `memory_search` to find source-backed memories.
   - Summaries cite their sources; LLM cannot mutate immutable timeline facts.
3. Dialogue & Life Channels:
   - Dialogue and Autonomous work run on independent channels.
   - Dialogue does not preempt or preemptively pause autonomous activities.
