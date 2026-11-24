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
from pipeline import Run
from plan import digest, text

SKILL = Path(__file__).resolve().parents[1]


def paths(repo):
    default = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "sdlc"
    store = Path(os.environ.get("SDLC_STATE_DIR", default)).expanduser().resolve()
    common = git_dir(repo)
    require(not store.is_relative_to(repo) and not store.is_relative_to(common),
            "Place SDLC_STATE_DIR outside the app and its Git metadata.")
    return store / digest(str(common)) / "runs", common


def new_run(repo, feature, request, host, max_fixes, package=SKILL):
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
        "repo": str(repo), "git_dir": str(common), "base": base, "protocol": 1,
    }))
    return Run(directory)


def load(repo, run_id):
    require(re.fullmatch(r"[0-9a-f]{16}", run_id), "Invalid run ID.")
    storage, common = paths(repo)
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
        require(handoff_exists, "Spec Kit completed without a verified handoff.")
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
    return {**result, "run_id": run.data["id"], "feature": run.data["feature"], "host": run.data["host"]}


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


def execute(repo, args):
    subcommand = args.command
    if subcommand == "release":
        return release(repo, args)
    if subcommand == "start":
        run = new_run(repo, args.feature, args.request, args.host, args.max_fixes)
    else:
        run = load(repo, args.run_id)
    if subcommand == "status":
        return summary(run)

    with lock(run.directory / "locks" / "engine.lock") as lock_fd:
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
            replacement = new_run(repo, run.data["feature"], args.request, run.data["host"], run.data["max_fixes"])
            run.save("superseded.json", {"run": replacement.data["id"]})
            with lock(replacement.directory / "locks" / "engine.lock") as replacement_fd:
                return summary(replacement, engine.launch(replacement, new=True, lock_fd=replacement_fd))
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
    result = argparse.ArgumentParser(description="Run the SDLC plugin in an application Git repository.")
    result.add_argument("--repo", type=Path, default=Path.cwd())
    commands = result.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--feature", required=True)
    start.add_argument("--request", required=True)
    start.add_argument("--host", choices=("codex", "claude"), required=True)
    start.add_argument("--max-fixes", type=int, default=2)
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
    if result.get("status") in {"startup_failed", "failed"} or (
        "criteria" in result and any(row["status"] != "pass" for row in result["criteria"])
    ):
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (Error, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print(f"sdlc: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("sdlc: Interrupted. Use status to inspect the run.", file=sys.stderr)
        sys.exit(130)
