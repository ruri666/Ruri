# Artificial Life 0.4.0

Add portable memory and independent lifecycle management to an existing agent.
The host keeps its model, native tools, permissions, workspace, reasoning loop
and user-facing answer format. Plugin context is supplemental and does not
replace host/user instructions. Ordinary actions need not all be wrapped in
activity bookkeeping.

The portable `plugin.json`/`mcp.json` and Codex/Claude compatibility manifests
describe the same plugin. Install the Python package in the host environment;
`artificial-life-mcp` is its console entrypoint. The default client connects to
the **native** lifecycle service at `http://127.0.0.1:8771`.

For a local checkout, create a Python 3.12+ virtual environment and run
`python -m pip install '.[mcp]'` inside it. Use that environment's Python when
starting the service. The default native data root is `~/.artificial-life/native`.

```powershell
python -m artificial_life.daemon --engine native --root ./life-data --persona-id my-agent --persona "Your user-selected persona"
```

The service holds the sole workspace writer lease and manages memory, activities,
committed schedules and deterministic heartbeats. It does not start another model
or narrow the host's existing tools. For host-managed work:

1. An independent host worker calls `execution_claim(worker_id, channel)` when
   its host-supported wakeup fires. Channels are `autonomous` and `dialogue`.
2. The host receives concise context and the current activity, searches memory
   when needed, and executes with its own tools and permissions.
3. Check `execution_status` before further operations when cancellation matters.
   Call `execution_complete` with observed results, or `execution_fail`.
   These are bookkeeping tool arguments, not a JSON-only model response mode.

For planning, result fields are `name`, `goal`, and optional `steps`. For
recovery, choose a concrete recreational activity and set `activity_profile.mode`
to `leisure`. For work, provide `body` and, when relevant,
`outcome`/`deliverables`. A queued dialogue provides `reply`. Submission is not
independent verification of arbitrary external work. Identical resubmission is safe.

Direct conversations can use context/memory tools without replacing the host UI.
For native services, direct `dialogue_begin` defaults to host-owned generation;
it does not also enqueue another reply. The built-in UI explicitly selects
service-owned dispatch. `execution_owner=host|service` can select one route.
Dialogue never controls autonomous scheduling.

MCP alone cannot wake a closed application. Unattended operation needs an
independent host worker, callback or scheduler capable of invoking that agent.
This source update does not install, reload, start or change any running host.
No universal model-quality, latency or token-cost guarantee is made; the
testable boundary is preserving host capabilities and concise initial context.

CLI/API/DSH engines remain optional standalone adapters. Their execution limits
belong to those adapters, not native plugin mode. The daemon's old default is
retained for existing services: select `--engine native` for this plugin path.
The separate DSH standalone bundle remains an optional legacy adapter.

For a bounded real Codex trial from this checkout, use an authenticated Codex
CLI on PATH and run the following command. Replace the persona text with the
persona you want. For an existing persona, omit `--persona` and select its ID.

```text
python scripts/run_codex_native_trial.py --root ./life-data --persona-id my-agent --persona "Your persona" --output ./trial-01 --model gpt-5.6-luna --effort medium --user-related
```

Open `http://127.0.0.1:8771/` for dialogue and `http://127.0.0.1:57077/index.html`
for the exact plugin input to each Codex job. The trial pauses model calls after
180 seconds by default. Create a `STOP` file inside the trial output directory
to stop the local viewer and release the workspace. Choose unused ports with
`--port` and `--viewer-port` when another instance is running.

For an interactive session, pass `--duration 0`: both independent host workers
stay available until a `STOP` file is created. This avoids leaving an apparently
live chat with no reply worker after the bounded trial expires. The viewer uses
the selected persona manager and displays the actual model and effort arguments.

In user-related mode, work and leisure start from the owner's committed
conversation and interests. The agent chooses the activity and cites the
supporting memory. A source check establishes provenance, while relevance
and usefulness remain judgments about the actual activity.
