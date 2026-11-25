---
name: branchtower
description: Run a feature through a reviewed specification, visible native subagents, independent verification, and release handoff in the current Git repository when the user requests the BranchTower workflow.
---

# BranchTower workflow

Run [scripts/run.py](scripts/run.py) with Python 3.9 or newer from the application's Git repository. Start with `start --backend native --host HOST --feature FEATURE --request REQUEST`, using `codex` or `claude` for the current chat. The runner prepares assignments, validates results, and reports the next step. The parent chat launches and steers the agents.

## Dispatch from this chat

When the runner returns `waiting_for_agents`, read its `jobs`.

1. For each `ready` job, run `reserve-job RUN_ID --job JOB_ID` and keep the token. Include the job ID in the child’s label. Read the job’s `prompt_file` and `schema_file`, then give the child their contents and the exact checkout path. Tell it to use absolute file paths and select that checkout for every shell command.
2. In Codex, launch a native subagent under this chat. Choose a read-only role for planners and verifiers when it supports their checks. In Claude Code, use the background Agent tool with the matching `branchtower:planner`, `branchtower:worker`, or `branchtower:verifier` role. Use the assigned checkout directly. Give each verifier a fresh context.
3. Immediately record each child's actual host ID with `attach-agent RUN_ID --job JOB_ID --token TOKEN --agent-id AGENT_ID`. Launch independent ready workers before waiting for any of them. Show their labels and IDs in the chat while the user can still steer them. Report any host permission or checkout blocker.
4. Submit the bound child's JSON with `submit-job RUN_ID --job JOB_ID --token TOKEN --agent-id AGENT_ID --result-file FILE`. Use `--result-file -` for standard input. If a response is malformed, ask that child to correct it. Run `resume RUN_ID` after submitting results.

## Review and approval

When the runner returns `paused`, show the specification, plan, and task files. Summarize the behavior, acceptance checks, and who owns each file. Wait for the user's explicit approval before running `approve RUN_ID --digest DIGEST` with the displayed digest. Use `reject RUN_ID` if the user rejects the draft.

Continue dispatching workers, independent verification, and scoped repairs as requested by the runner. Report the verified branch, exact commit, and evidence path, or explain the blocker.

## Steering and recovery

- Use `status RUN_ID` to reconnect. Reuse the recorded child for an `attached` job. For a `submitted` job, continue with `resume RUN_ID`. Route user instructions to the attached worker by its recorded ID using Codex subagent messaging or Claude Code's SendMessage. Steering is available until the result is submitted and must stay within the approved assignment.
- A reserved job can already have a live child. Look it up by its job label and attach it if found. Only after confirming a previous child has stopped or is absent, use `detach-agent RUN_ID --job JOB_ID --token TOKEN` and reserve the job again.
- If the user changes the approved behavior or files, stop and detach any outstanding agents, run `revise RUN_ID --request REQUEST`, and obtain fresh approval.

## PR and release

With the user’s authorization to publish, run `open-pr RUN_ID --remote REMOTE --base BASE` for the target repository. A new PR includes the tested commit, reviewed draft digest, and observed results for each acceptance check. If the verified PR already exists, the runner returns its URL. Use the application's existing review and CI/CD process. If the shipping commit changes, verify it with `verify-commit RUN_ID --commit COMMIT`.

After deployment, use `release` to record the shipped commit, artifact, owner, rollback procedure, and observed smoke check. Record follow-up work when the check fails.

The runner includes the shared rules and role instructions in the assignments it sends. [Shared rules](references/rules.md) · [Planner](references/planner.md) · [Worker](references/worker.md) · [Verifier](references/verifier.md)
