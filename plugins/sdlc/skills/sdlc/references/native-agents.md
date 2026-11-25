# Native agent dispatch and steering

The parent chat owns native delegation. The runner prepares jobs and validates the results. Use the current host's native agent tool so the agents appear under this conversation.

## Dispatch

1. Read `jobs` in the runner output. Launch only jobs in `ready` status. Call `reserve-job RUN_ID --job JOB_ID` first and keep its token. A reserved job may already have a child whose ID has yet to be recorded. Reconcile it against the host's active agents before you dispatch another child.
2. Name the job in the child description, such as `SDLC T01 (initial-1-T01)`. Give it the full runner-provided prompt and schema, and the exact checkout path. Start independent workers in the same batch before waiting for either. Give each verifier a fresh context with the verifier prompt.
3. In **Codex**, use native subagent delegation. Choose a read-only role for planner and verifier if the host offers one that can perform the checks. In **Claude Code**, use the native Agent tool with the packaged `sdlc:planner`, `sdlc:worker`, or `sdlc:verifier` role in the background. Put the job label in the description. Use the assigned checkout, since host-managed worktree isolation could create a second checkout at a different commit.
4. Record the actual host ID immediately with `attach-agent RUN_ID --job JOB_ID --token TOKEN --agent-id AGENT_ID`. Once workers are live, show their labels and IDs in the parent chat before waiting. If the subagent tool or the worker checkout is unavailable under the current permissions, report that blocker.
5. Wait for completed native results. Submit the actual JSON from the bound agent with `submit-job RUN_ID --job JOB_ID --token TOKEN --agent-id AGENT_ID --result-file FILE`. The parent can use `--result-file -` to supply JSON on standard input. After submitting the result, call `resume RUN_ID` to advance. A finished child may return an invalid JSON response. Ask that child to correct its handoff before submitting it.

## Steer or recover

- If the user asks to steer an active worker, use the host's native messaging or steering tool with the recorded ID. In Claude Code use SendMessage. In Codex use its subagent messaging tool. Route updates to the existing child and keep them within the approved task and files.
- For an attached job, reuse its recorded child. For a reserved job without an ID, check the host's activity for the job label before taking action. Once you confirm the old child is stopped or absent, use `detach-agent RUN_ID --job JOB_ID --token TOKEN`. The job can then be reserved for a new child.
- A submitted job is closed to steering. A new user request that changes the approved behavior or files needs a revised run and another approval.
- Work in the exact runner-provided checkout. Use absolute paths for file tools and specify that checkout on every shell call. Follow host permission prompts for access and report denied access as blocked.
