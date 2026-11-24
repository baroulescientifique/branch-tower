---
name: sdlc
description: Run a feature through a reviewed specification, parallel implementation, independent verification, and release handoff in the current Git repository when the user requests the SDLC workflow.
---

# SDLC workflow

Use the runner at [scripts/run.py](scripts/run.py). Resolve that path from this installed skill, then run it with Python 3.9 or newer in the application's Git repository. Pass the CLI for the current host as `--host codex` or `--host claude`.

- Start with `start --host HOST --feature FEATURE --request REQUEST`. Use a short lowercase feature name such as `json-output`.
- When the runner pauses, show the user the review documents and summarize the proposed behavior, checks, and file assignments. Wait for their approval of that draft. Pass the returned digest to `approve RUN_ID --digest DIGEST` only after the user approves it. Use `reject RUN_ID` to close a rejected run or `revise RUN_ID --request REQUEST` to prepare a fresh draft.
- Use `status RUN_ID` to inspect a run, `resume RUN_ID` after a recoverable failure, and `recover RUN_ID` when Spec Kit left an interrupted run marked running.
- Report the verified branch, exact commit, and evidence path on success. Report blocked checks and the evidence path if verification cannot pass.
- Follow [release guidance](references/release.md) when the user asks for a PR or release handoff. Obtain authorization before publishing a branch or PR.

The runner reads [shared rules](references/rules.md) and the [planner](references/planner.md), [worker](references/worker.md), and [verifier](references/verifier.md) instructions for their respective stages.
