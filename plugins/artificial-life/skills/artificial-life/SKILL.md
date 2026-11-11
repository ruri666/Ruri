---
name: artificial-life
description: Manage and debug the Artificial Life (ai-companion-new) portable memory, heartbeat, and autonomous agent lifecycle.
---

# Artificial Life Companion Skill

Use this skill when interacting with the `ai-companion-new` (Artificial Life) companion engine.

## Overview
- **Web UI & Debugging Endpoint**: `http://127.0.0.1:8771/`
- **Engine**: Native Host (`--engine native`)
- **Host Agent**: The existing host agent and its configured model
- **Data Root**: `~/.artificial-life/native` by default, or the service's configured `--root`

## Primary Tools
- `structure_get`: Retrieve memory trees, activity tree, schedule tree, and heartbeat state.
- `health`: Check provider status, logical time, and lifecycle phase.
- `state`: Retrieve current energy, fatigue, location, mood, activities, and goals.
- `execution_claim`: Claim due autonomous or dialogue tasks from the lifecycle engine.
- `execution_complete`: Submit finished work body, deliverables, or planning steps.
- `memory_search`: Search episodic, temporal, and activity memory.
