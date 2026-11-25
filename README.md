# SDLC plugin

Install this plugin in Claude Code or Codex and run it inside an application Git repository. It prepares a feature draft, pauses for human approval, runs independent tasks in parallel Git worktrees, and asks a fresh agent to verify the combined commit. It can send failed checks back to the workers for correction.

## Install

Use a local checkout of this repo as the marketplace path. A Git repository URL can serve as the marketplace source after the plugin repo has been pushed.

**Codex**

```sh
codex plugin marketplace add /path/to/sdlc-plugin
codex plugin add sdlc@sdlc-tools
```

**Claude Code**

```sh
claude plugin marketplace add /path/to/sdlc-plugin
claude plugin install sdlc@sdlc-tools
```

Use Git with an author identity and Python 3.9 or newer. For the chat workflow, use a Codex or Claude Code session with native subagents enabled. The host must allow access to the runner's state directory and the exact Git worktrees it creates. Publishing a GitHub PR also requires authenticated `gh` and permission to push to the selected remote.

## Run a feature from your chat

From an agent session in the application repo, invoke `$sdlc` in Codex or `/sdlc:sdlc` in Claude Code and describe the change. The skill starts one planner subagent in that session. Review the resulting `spec.md`, `plan.md`, and `tasks.md` before you approve implementation.

After you approve, the chat launches named workers as native subagents. You can ask the parent chat to show their status or tell a worker to change course within its approved assignment. Tasks wait for their prerequisites. The runner checks file assignments, integrates the commits, and asks a fresh native verifier to test the combined result. Failed checks can go back to the owning workers for up to two repair rounds by default.

The [entry skill](plugins/sdlc/skills/sdlc/SKILL.md) and [native dispatch instructions](plugins/sdlc/skills/sdlc/references/native-agents.md) define the chat's responsibilities. The runner records the actual agent IDs and checks each completed handoff. Host permissions govern access to the worktrees. The Git scope checks govern what can be integrated.

| Command | Use |
|---|---|
| `status RUN_ID` | Show pending jobs, attached native IDs, and delivery status. |
| `approve RUN_ID --digest DRAFT_DIGEST` | Approve the exact feature draft you reviewed. |
| `reject RUN_ID` | End a pending review. |
| `revise RUN_ID --request REQUEST` | Start a new review after stopping and detaching any outstanding agents. |
| `resume RUN_ID` | Process native results or continue a paused or failed terminal run. |
| `verify-commit RUN_ID --commit COMMIT` | Check a different exact commit containing the approved spec. In chat mode this creates a fresh verifier job. |

Run state stays in `${XDG_STATE_HOME:-~/.local/state}/sdlc` by default. Set `SDLC_STATE_DIR` to use another location outside the app and its Git metadata. Review files are in a separate app worktree and are included on the verified branch. State also includes checks, task commits, and the pinned skill resources used to resume the run.

## Terminal automation

Set `SDLC_RUNNER` to the absolute path to the installed skill's `scripts/run.py`. The CLI backend runs outside an interactive chat using the authenticated host CLI and the pinned Spec Kit revision. It needs `uvx`, which selects Python 3.11 or newer for Spec Kit.

```sh
python3 "$SDLC_RUNNER" start --backend cli --host codex --feature json-output --request 'Add JSON output.'
python3 "$SDLC_RUNNER" status RUN_ID
python3 "$SDLC_RUNNER" approve RUN_ID --digest DRAFT_DIGEST
```

Use `recover RUN_ID` if Spec Kit could not start or left a stale running state. Confirm the draft and starting commit before approving either backend.

## PR and release handoff

A passing run provides a local branch, exact tested commit, evidence JSON, and PR summary. Use `open-pr RUN_ID --remote origin --base main` to publish that verified commit and open a PR. The command uses the selected GitHub remote, and existing PRs must still point to the verified commit. Your application's existing CI/CD handles merge, staging, promotion, and rollback.

If the commit that ships changes, run `verify-commit` against the actual merge candidate before relying on its evidence. Save the resulting JSON with your CI artifacts. Record the release after the smoke check has run.

```sh
python3 "$SDLC_RUNNER" release --evidence "$EVIDENCE_FILE" --commit "$SHIPPED_COMMIT" \
  --artifact "$ARTIFACT_ID" --owner 'Platform team' --result pass \
  --smoke-check 'Staging endpoint returned the expected response.' \
  --rollback 'Redeploy the previous artifact.' --out release.json
```

For a failed smoke check, use `--result fail --follow-up 'Describe the follow-up work'`. The command records the outcome. CI owns its deployment or rollback decision.

## Review and test the plugin

- [Shared rules](plugins/sdlc/skills/sdlc/references/rules.md)
- [Planner](plugins/sdlc/skills/sdlc/references/planner.md), [worker](plugins/sdlc/skills/sdlc/references/worker.md), and [verifier](plugins/sdlc/skills/sdlc/references/verifier.md) instructions
- [Release handoff](plugins/sdlc/skills/sdlc/references/release.md)
- [Spec Kit workflow](plugins/sdlc/skills/sdlc/scripts/workflow.yml)

Run `python3 -m unittest discover -s tests -v` for workflow and Git integration tests in temporary repositories. These tests exercise native job dispatch and handoffs, approval, repair, recovery, scope enforcement, and evidence in temporary Git repositories. The terminal integration tests use simulated agent and Spec Kit CLIs.
