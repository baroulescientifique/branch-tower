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

Use Git with an author identity, Python 3.9 or newer, and `uvx`. The plugin runs on macOS or Linux with the selected, authenticated host CLI and its sandbox support. The runner fetches the pinned Spec Kit 1.4.4 revision through `uvx` on first use. Spec Kit needs Python 3.11 or newer, which `uvx` selects separately. Publishing a GitHub PR also requires authenticated `gh` and permission to push to the selected remote.

## Run a feature

From an agent session in the application repo, invoke `$sdlc` in Codex or `/sdlc:sdlc` in Claude Code. Describe what you want to change.

The [entry skill](plugins/sdlc/skills/sdlc/SKILL.md) launches the same runner you can use from a terminal. Set `SDLC_RUNNER` to the absolute path to the installed skill's `scripts/run.py`, or to that script in the source checkout.

```sh
python3 "$SDLC_RUNNER" start --host codex --feature json-output --request 'Add JSON output.'
python3 "$SDLC_RUNNER" status RUN_ID
python3 "$SDLC_RUNNER" approve RUN_ID --digest DRAFT_DIGEST
```

The first command returns the review folder and draft digest. Review `spec.md`, `plan.md`, and `tasks.md` in that folder before approving. The folder is in a separate worktree of your app repo. The approved files are included on the verified branch, and your original checkout is left as it was. The runner cancels approval if the draft bytes or starting commit have changed.

| Command | Use |
|---|---|
| `reject RUN_ID` | End a pending review. |
| `revise RUN_ID --request REQUEST` | Start a new review with updated instructions or a new starting commit. |
| `resume RUN_ID` | Continue a paused or failed Spec Kit run. |
| `recover RUN_ID` | Start a replacement engine attempt after a startup failure or stale running state. Accepted worker commits are reused. |
| `verify-commit RUN_ID --commit COMMIT` | Check a different exact commit containing the approved spec, such as the revision CI will ship. This writes new evidence and exits nonzero if any check fails or is blocked. |

Tasks can name prerequisites. The runner executes independent tasks in batches of up to two workers and integrates each batch before starting its dependents. Each file has one owning task. A post-run Git diff check determines which changes may be integrated. The selected host's sandbox controls what its tools can access during execution.

The verifier records the check and observed result for every acceptance ID. A failed check can trigger a repair in the owning tasks' approved files, followed by a fresh verification of the new commit. The default maximum is two repair rounds. Set `--max-fixes` when starting a run. An environment blocker pauses delivery until the check can run.

Run state stays in `${XDG_STATE_HOME:-~/.local/state}/sdlc` by default. Set `SDLC_STATE_DIR` to use another location outside the app and its Git metadata. State includes the review worktree, worker commits, independent verification reports, and a pinned copy of the skill resources for recovery across upgrades.

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

Run `python3 -m unittest discover -s tests -v` for workflow and Git integration tests in temporary repositories. These tests use simulated agent and Spec Kit CLIs to exercise approval, repair, recovery, scope enforcement, and evidence. A live smoke test also needs Spec Kit access, an authenticated host, and a separate Git app.
