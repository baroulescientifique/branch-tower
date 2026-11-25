#!/usr/bin/env python3
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid

from common import Error, encode, git, git_dir, head, lock, read_json, require, root, write
import engine
from handoff import passing_report, publish, release
from native import advance, result_input
from pipeline import Run
from plan import digest, text

SKILL = Path(__file__).resolve().parents[1]


def paths(repo, run_id=None):
    state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    configured = os.environ.get("BRANCHTOWER_STATE_DIR") or os.environ.get("SDLC_STATE_DIR")
    store = Path(configured) if configured else state / "branchtower"
    common = git_dir(repo)
    relative = Path(digest(str(common))) / "runs"
    if run_id and not configured and not (store / relative / run_id).exists():
        previous = state / "sdlc"
        if (previous / relative / run_id / "run.json").is_file():
            store = previous
    store = store.expanduser().resolve()
    require(not store.is_relative_to(repo) and not store.is_relative_to(common),
            "Place BRANCHTOWER_STATE_DIR outside the app and its Git metadata.")
    return store / relative, common


def new_run(repo, feature, request, host, max_fixes, backend="cli", package=SKILL):
    require(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", feature) and len(feature) <= 60,
            "Use a lowercase feature ID up to 60 characters, such as json-output.")
    require(text(request), "A feature request is required.")
    require(0 <= max_fixes <= 5, "Set --max-fixes between 0 and 5.")
    base = head(repo)
    git(repo, "var", "GIT_AUTHOR_IDENT")
    git(repo, "var", "GIT_COMMITTER_IDENT")
    storage, common = paths(repo)
    run_id = uuid.uuid4().hex[:16]
    directory = storage / run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    shutil.copytree(package, directory / "skill", ignore=shutil.ignore_patterns("__pycache__"))
    write(directory / "run.json", encode({
        "id": run_id, "feature": feature, "request": request, "host": host, "max_fixes": max_fixes,
        "repo": str(repo), "git_dir": str(common), "base": base, "protocol": 1, "backend": backend,
    }))
    return Run(directory)


def load(repo, run_id):
    require(re.fullmatch(r"[0-9a-f]{16}", run_id), "Invalid run ID.")
    storage, common = paths(repo, run_id)
    directory = storage / run_id
    require((directory / "run.json").is_file() and not directory.is_symlink(), "Run not found for this repo.")
    run = Run(directory)
    require(run.git_dir == common and run.data["id"] == run_id and run.data["protocol"] == 1,
            "The run does not match the repository or supported protocol.")
    return run


def summary(run, output=None):
    attempt = engine.latest_attempt(run)
    current = engine.engine_status(attempt) if attempt else {}
    last = run.directory / "last-engine-result.json"
    result = dict(output or (read_json(last) if last.is_file() else {}))
    if current.get("status"):
        if output is None:
            matches = (result.get("run_id"), result.get("status")) == (current["id"], current["status"])
            if not matches:
                detail = result.get("detail") if result.get("status") == "startup_failed" else None
                result = {"last_attempt_error": detail} if detail else {}
            result["status"] = current["status"]
        result["engine_status"] = current["status"]
        result["engine_run_id"] = current["id"]
    if run.native:
        progress = run.native.progress()
        result = {**progress, **(output or {})}
        result["jobs"] = [run.native.describe(run.native.read(job_id)) for job_id in progress["jobs"]]
    if not result:
        result["status"] = "created"
    if (run.directory / "rejected.json").exists():
        result["status"] = "aborted"
    if (run.directory / "draft.json").is_file():
        result.update({
            "review_dir": str(run.review_tree / run.feature_dir),
            "draft_digest": digest(run.read("draft.json")),
        })
    handoff_exists = (run.directory / "handoff.json").is_file()
    if result["status"] == "completed":
        require(handoff_exists, "The workflow completed without a verified handoff.")
    if handoff_exists:
        handoff = run.read("handoff.json")
        result.update(handoff)
        result["handoff"] = str(run.directory / "handoff.md")
        result["handoff_usable"] = False
        try:
            passing_report(run, handoff)
            result["handoff_usable"] = not (run.directory / "superseded.json").exists()
        except (Error, OSError, ValueError, KeyError, TypeError):
            pass
    if (run.directory / "latest-verification.json").is_file():
        result["latest_evidence"] = str(run.directory / run.read("latest-verification.json")["report"])
    if (run.directory / "superseded.json").is_file():
        result["superseded_by"] = run.read("superseded.json")["run"]
    return {**result, **(output or {}), "run_id": run.data["id"], "feature": run.data["feature"],
            "host": run.data["host"],
            "backend": run.data.get("backend", "cli")}


def phase(name):
    run = Run(Path(os.environ["SDLC_RUN_DIR"]))
    require(Path(__file__).resolve().is_relative_to(run.skill), "Workflow phase must run from the pinned skill.")
    with lock(run.directory / "locks" / "phase.lock") as lock_fd:
        run.phase_fd = lock_fd
        try:
            return {"prepare": run.prepare, "execute": run.execute}[name]()
        except (Error, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
            run.save("last-error.json", {"phase": name, "detail": str(error)})
            raise


def native_command(run, args):
    action = args.command
    jobs = run.native
    progress = jobs.progress()
    if action == "verify-commit":
        require(progress["status"] == "completed" and not progress["jobs"],
                "Finish the current native workflow before asking for a separate commit check.")
        run.approved(check_current=False)
        require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.commit), "Use the full commit ID.")
        candidate = git(run.repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
        return summary(run, advance(run, candidate))
    if action == "resume":
        if progress["status"] == "completed":
            return summary(run)
        return summary(run, advance(run, progress.get("candidate")))
    if action == "approve":
        require(progress["status"] == "paused", "Approval requires a paused review.")
        run.approve(args.digest)
        return summary(run, advance(run))
    if action == "reject":
        require(progress["status"] == "paused" and not (run.directory / "approval.json").exists(),
                "Rejection requires a pending review.")
        run.save("rejected.json", {"run": run.data["id"]})
        return summary(run)
    if action in {"reserve-job", "attach-agent", "submit-job", "detach-agent"}:
        if action != "detach-agent" and jobs.read(args.job)["role"] != "planner":
            run.approved(check_current=not progress.get("candidate"))
        if action == "reserve-job":
            job = jobs.reserve(args.job)
        elif action == "attach-agent":
            job = jobs.attach(args.job, args.token, args.agent_id)
        elif action == "submit-job":
            job = jobs.submit(args.job, args.token, args.agent_id, result_input(args.result_file))
        else:
            job = jobs.detach(args.job, args.token)
        return {**summary(run), "job": job}
    require(False, "Use resume to continue a native run.")


def revise(run, request):
    if run.native:
        reserved = [run.native.read(job_id) for job_id in run.native.progress()["jobs"]]
        require(not any(job["token"] and job["result"] is None for job in reserved),
                "Stop and detach outstanding native agents before revising the run.")
    replacement = new_run(run.repo, run.data["feature"], request, run.data["host"],
                          run.data["max_fixes"], run.data.get("backend", "cli"))
    run.save("superseded.json", {"run": replacement.data["id"]})
    with lock(replacement.directory / "locks" / "engine.lock") as replacement_fd:
        if replacement.native:
            return summary(replacement, advance(replacement))
        return summary(replacement, engine.launch(replacement, new=True, lock_fd=replacement_fd))


def execute(repo, args):
    subcommand = args.command
    if subcommand == "release":
        return release(repo, args)
    if subcommand == "start":
        run = new_run(repo, args.feature, args.request, args.host, args.max_fixes, args.backend)
    else:
        run = load(repo, args.run_id)
        pinned = run.skill / "scripts" / "run.py"
        if run.native and Path(__file__).resolve() != pinned:
            # Earlier pinned runners read only the original state directory setting.
            env = dict(os.environ, SDLC_STATE_DIR=str(run.directory.parents[2]))
            os.execve(sys.executable, [sys.executable, str(pinned), *sys.argv[1:]], env)
    if subcommand == "status":
        return summary(run)

    with lock(run.directory / "locks" / "engine.lock") as lock_fd:
        if run.native:
            require(not (run.directory / "superseded.json").exists(), "This run was superseded.")
            if subcommand == "revise":
                return revise(run, args.request)
            if subcommand == "start":
                return summary(run, advance(run))
            require(not (run.directory / "rejected.json").exists(), "This run was rejected. Revise it to start again.")
            if subcommand == "open-pr":
                return publish(run, args.remote, args.base)
            return native_command(run, args)
        require(subcommand not in {"reserve-job", "attach-agent", "submit-job", "detach-agent"},
                "Agent jobs are available in native runs.")
        if subcommand == "verify-commit":
            draft = run.approved(check_current=False)
            require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.commit), "Use the full commit ID.")
            candidate = git(run.repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
            run.phase_fd = lock_fd
            report = run.verify(draft, candidate)
            return {**report, "evidence": str(run.directory / report["file"])}
        require(not (run.directory / "superseded.json").exists(), "This run was superseded.")
        if subcommand == "open-pr":
            return publish(run, args.remote, args.base)
        # An engine or orphaned phase can still be finishing after its launcher died.
        with lock(run.directory / "locks" / "phase.lock"):
            pass
        if subcommand == "revise":
            return revise(run, args.request)
        if subcommand == "start":
            return summary(run, engine.launch(run, new=True, lock_fd=lock_fd))
        require(not (run.directory / "rejected.json").exists(), "This run was rejected. Revise it to start again.")
        attempt = engine.latest_attempt(run)
        state = engine.engine_status(attempt) if attempt else {}
        if subcommand == "approve":
            require(state.get("status") == "paused", "Approval requires a paused review.")
            run.approve(args.digest)
        if subcommand == "reject":
            require(state.get("status") == "paused" and not (run.directory / "approval.json").exists(),
                    "Rejection requires a pending review without a saved approval.")
            run.save("rejected.json", {"run": run.data["id"]})
            return summary(run, engine.launch(run, new=False, verdict="reject", lock_fd=lock_fd))
        approved = (run.directory / "approval.json").is_file()
        if approved:
            run.approved()
        verdict = "approve" if approved else None
        if subcommand == "recover":
            require(state.get("status") in {None, "created", "running"},
                    "Use resume for a paused or failed run.")
            return summary(run, engine.launch(run, new=True, verdict=verdict, lock_fd=lock_fd))
        return summary(run, engine.launch(run, new=False, verdict=verdict, lock_fd=lock_fd))


def parser():
    result = argparse.ArgumentParser(description="Run BranchTower in an application Git repository.")
    result.add_argument("--repo", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--feature", required=True)
    start.add_argument("--request", required=True)
    start.add_argument("--host", choices=("codex", "claude"), required=True)
    start.add_argument("--max-fixes", type=int, default=2)
    start.add_argument("--backend", choices=("native", "cli"), default="cli")
    for name in ("status", "approve", "reject", "resume", "recover", "revise", "open-pr", "verify-commit"):
        sub = commands.add_parser(name)
        sub.add_argument("run_id")
        if name == "approve":
            sub.add_argument("--digest", required=True)
        if name == "revise":
            sub.add_argument("--request", required=True)
        if name == "open-pr":
            sub.add_argument("--remote", default="origin")
            sub.add_argument("--base")
        if name == "verify-commit":
            sub.add_argument("--commit", required=True)
    for name in ("reserve-job", "attach-agent", "submit-job", "detach-agent"):
        job = commands.add_parser(name)
        job.add_argument("run_id")
        job.add_argument("--job", required=True)
        if name != "reserve-job":
            job.add_argument("--token", required=True)
        if name in {"attach-agent", "submit-job"}:
            job.add_argument("--agent-id", required=True)
        if name == "submit-job":
            job.add_argument("--result-file", required=True)
    deploy = commands.add_parser("release")
    for key in ("evidence", "commit", "artifact", "owner", "smoke-check", "rollback", "out"):
        deploy.add_argument(f"--{key}", required=True)
    deploy.add_argument("--result", choices=("pass", "fail"), required=True)
    deploy.add_argument("--follow-up")
    return result


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "_phase":
        result = phase(sys.argv[2])
    else:
        args = parser().parse_args()
        result = execute(root(args.repo), args)
    print(encode(result), end="")
    if "job" in result:
        return 0
    if result.get("status") in {"startup_failed", "failed"} or (
        "criteria" in result and any(row["status"] != "pass" for row in result["criteria"])
    ):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"branchtower: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("branchtower: Interrupted. Use status to inspect the run.", file=sys.stderr)
        sys.exit(130)
