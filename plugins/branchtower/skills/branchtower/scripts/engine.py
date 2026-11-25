import json
import os
from pathlib import Path
import re

from common import command, read_json, require

# Spec Kit v1.4.4
SPEC_KIT = "git+https://github.com/github/spec-kit.git@c8133f463a586879e3af6dfd2864beb8995a8764"


def attempts(run):
    paths = (run.directory / "engine").glob("attempt-*")
    return sorted(paths, key=lambda path: int(path.name.split("-")[1]))


def engine_status(directory):
    states = list((directory / ".specify" / "workflows" / "runs").glob("*/state.json"))
    if not states:
        return {}
    require(len(states) == 1, f"Expected one Spec Kit run in {directory}.")
    path = states[0]
    state = read_json(path)
    run_id = state.get("run_id") or path.parent.name
    require(isinstance(run_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", run_id),
            "Invalid Spec Kit run identifier.")
    return {"id": run_id, "status": state.get("status")}


def latest_attempt(run):
    paths = attempts(run)
    return paths[-1] if paths else None


def launch(run, *, new, verdict=None, lock_fd):
    attempt = latest_attempt(run)
    if new:
        sequence = int(attempt.name.split("-")[1]) + 1 if attempt else 1
        attempt = run.directory / "engine" / f"attempt-{sequence}"
        (attempt / ".specify").mkdir(parents=True)
        args = ["run", str(run.skill / "scripts" / "workflow.yml"),
                "--input", f"run_dir={run.directory}"]
    else:
        require(attempt is not None, "This run has no Spec Kit attempt. Use recover.")
        current = engine_status(attempt)
        require(current.get("id"), "Spec Kit did not start. Fix the startup error and use recover.")
        require(current["status"] in {"paused", "failed"},
                f"Spec Kit reports {current['status']}. Use status or recover.")
        args = ["resume", current["id"]]
    if verdict:
        args.extend(["--input", f"review={verdict}"])
    env = {key: value for key, value in os.environ.items() if key != "SPECIFY_INIT_DIR"}
    error_file = run.directory / "last-error.json"
    error_file.unlink(missing_ok=True)
    try:
        result = command(
            ["uvx", "--python", ">=3.11", "--from", SPEC_KIT, "specify", "workflow", *args, "--json"],
            attempt, env=env, fds=(lock_fd,),
        )
    except OSError as error:
        output = {"status": "startup_failed", "detail": str(error)}
        run.save("last-engine-result.json", output)
        return output
    try:
        output = json.loads(result.stdout)
    except ValueError:
        output = {}
    require(isinstance(output, dict), "Spec Kit returned an invalid status report.")
    if error_file.is_file():
        output["detail"] = read_json(error_file)["detail"]
    if result.returncode and not output.get("status"):
        output["status"] = "startup_failed"
        output["detail"] = output.get("detail") or result.stderr.strip()[-1800:] or "Spec Kit could not start."
    require(output.get("status") in {"paused", "completed", "failed", "aborted", "startup_failed"},
            f"Unexpected Spec Kit result: {output.get('error') or output.get('status')}.")
    run.save("last-engine-result.json", output)
    return output
