Project: Artificial Life Agent



Persona:

\- 当前默认 persona 是轻小说作家。



Core invariants:

\- Timeline facts are append-only.

\- Schedule describes plans; Timeline describes facts.

\- Memory files are the source of truth.

\- Summaries must reference their sources.

\- LLM cannot directly mutate historical facts.

\- Deterministic logic belongs in code when possible.

\- Activity and Dialogue share Base Context but have separate working context.

\- User messages do not automatically imply immediate replies.

\- All randomness must be seedable in tests.

\- Avoid unnecessary LLM calls.

\- Prefer simple, testable mechanisms over complex abstractions.



Before changing architecture:

\- Read docs/artificial\_life\_architecture.md.

