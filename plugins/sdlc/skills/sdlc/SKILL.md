---
name: sdlc
description: Run a feature through a reviewed specification, visible native subagents, independent verification, and release handoff in the current Git repository when the user requests the SDLC workflow.
---

# SDLC workflow

Use the runner at [scripts/run.py](scripts/run.py). Resolve it from this installed skill and execute it with Python 3.9 or newer from the application Git repository. Start with `start --backend native --host HOST --feature FEATURE --request REQUEST`, choosing `codex` or `claude` for the current host. Run the commands below through this same runner.

1. When the runner returns `waiting_for_agents`, handle the jobs using [native agent dispatch](references/native-agents.md). Report the worker labels and native IDs in the current chat so the user can inspect or steer them.
2. When the runner returns `paused`, show the review files and summarize the proposed behavior, checks, and file assignments. Wait for explicit approval. Pass the displayed digest to `approve RUN_ID --digest DIGEST`. Use `reject RUN_ID` to close a rejected draft.
3. Use `resume RUN_ID` after the native agents' results have arrived and been submitted. Continue through implementation, verification, and any scoped repairs. Report the verified branch, exact commit, and evidence path, or the blocker.
4. For scope changes, stop and detach outstanding native agents before `revise RUN_ID --request REQUEST` and obtain a fresh approval. Use `status RUN_ID` to reconnect to active assignments after a turn ends.

Apply [release guidance](references/release.md) when the user asks for a PR or release handoff. Obtain authorization before publishing a branch or PR.

The runner bundles the [rules](references/rules.md), [planner](references/planner.md), [worker](references/worker.md), and [verifier](references/verifier.md) instructions into its jobs.
