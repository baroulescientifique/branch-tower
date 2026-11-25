"""Persist the jobs and handoffs for subagents launched by the parent chat."""
import json
from pathlib import Path
import re
import sys
import uuid

from adapters import validate
from common import Error, git, head, read_json, require, status, write
from plan import digest, text, validate as validate_plan


class Pending(Exception):
    def __init__(self, jobs):
        super().__init__("Waiting for native agents.")
        self.jobs = jobs


def validate_verdict(rows, criteria):
    require(sorted(row["id"] for row in rows) == sorted(criteria)
            and all(text(row["check"]) and text(row["observed"]) for row in rows),
            "Verification needs one observed result for each approved acceptance criterion.")


class Jobs:
    def __init__(self, run):
        self.run = run
        self.directory = run.directory / "native-jobs"

    def path(self, job_id):
        require(re.fullmatch(r"[a-zA-Z0-9-]{1,100}", job_id), "Invalid job ID.")
        return self.directory / f"{job_id}.json"

    def read(self, job_id):
        return read_json(self.path(job_id))

    def save(self, job):
        self.run.save(f"native-jobs/{job['id']}.json", job)

    def progress(self):
        path = self.run.directory / "native-progress.json"
        return read_json(path) if path.is_file() else {"status": "created", "jobs": []}

    def active(self, job_id):
        require(job_id in self.progress()["jobs"], "This job is no longer pending in the workflow.")

    def request(self, job_id, role, cwd, prompt, schema, *, paths=(), criteria=()):
        path = self.path(job_id)
        prompt_file = path.with_suffix(".md")
        instructions = (
            f"# Job {job_id}\n\nWork only in this checkout: {cwd}\n"
            f"Starting commit: {head(cwd)}\n"
            f"Return a JSON result matching {schema}. Give it to the parent chat when done.\n\n{prompt}\n"
        )
        details = {
            "id": job_id, "role": role, "checkout": str(cwd), "commit": head(cwd),
            "schema_file": str(schema), "prompt_file": str(prompt_file),
            "prompt_digest": digest(instructions), "paths": list(paths), "criteria": list(criteria),
        }
        if path.is_file():
            job = self.read(job_id)
            require(all(job[key] == value for key, value in details.items()),
                    f"Expected the original definition of native job {job_id}.")
            require(prompt_file.read_bytes() == instructions.encode("utf-8"),
                    f"The prompt for {job_id} changed.")
        else:
            write(prompt_file, instructions)
            job = {**details, "agent_id": None, "token": None, "result": None}
            self.save(job)
        if job["result"] is None:
            raise Pending([job_id])
        return job["result"]

    def describe(self, job):
        stage = "submitted" if job["result"] is not None else (
            "attached" if job["agent_id"] else "reserved" if job["token"] else "ready"
        )
        return {key: job[key] for key in (
            "id", "role", "checkout", "commit", "prompt_file", "schema_file", "agent_id", "token"
        )} | {"status": stage}

    def reserve(self, job_id):
        self.active(job_id)
        job = self.read(job_id)
        require(job["result"] is None, "This job already has a result.")
        require(job["token"] is None,
                "This job is already reserved. Inspect its native agent before dispatching again.")
        job["token"] = uuid.uuid4().hex
        self.save(job)
        return self.describe(job)

    def attach(self, job_id, token, agent_id):
        self.active(job_id)
        job = self.read(job_id)
        require(job["token"] and token == job["token"] and text(agent_id) and job["result"] is None,
                "Use the current reservation token to attach a native agent.")
        require(job["agent_id"] in (None, agent_id), "A different agent is already attached.")
        for path in self.directory.glob("*.json"):
            other = read_json(path)
            require(other["id"] == job_id or other["agent_id"] != agent_id,
                    "This agent is already assigned to another job.")
        job["agent_id"] = agent_id
        self.save(job)
        return self.describe(job)

    def detach(self, job_id, token):
        self.active(job_id)
        job = self.read(job_id)
        require(job["token"] and job["token"] == token and job["result"] is None,
                "Use the current reservation token for a job without a result.")
        job["agent_id"] = None
        job["token"] = None
        self.save(job)
        return self.describe(job)

    def submit(self, job_id, token, agent_id, result):
        job = self.read(job_id)
        require(job["token"] and token == job["token"] and job["agent_id"] == agent_id and text(agent_id),
                "Result must come from the currently attached agent and reservation.")
        if job["result"] is not None:
            require(job["result"] == result, "This job already has a different result.")
            return self.describe(job)
        self.active(job_id)
        prompt = Path(job["prompt_file"]).read_bytes().decode("utf-8")
        require(digest(prompt) == job["prompt_digest"], "The job prompt changed after dispatch.")
        validate(result, read_json(Path(job["schema_file"])))
        checkout = Path(job["checkout"])
        require(head(checkout) == job["commit"], "The agent changed the job's starting commit.")
        changed = status(checkout)
        if job["role"] == "planner":
            require(not changed, "The planner changed its checkout.")
            validate_plan(result)
        elif job["role"] == "worker":
            require(changed <= set(job["paths"]),
                    f"The worker changed unapproved paths: {sorted(changed - set(job['paths']))}.")
            require(text(result["summary"]) and result["checks"]
                    and all(text(check) for check in result["checks"]), "The worker needs a check and summary.")
            require(changed or job_id.startswith("repair-"), "The worker produced no implementation changes.")
        else:
            require(not changed, "The verifier changed its checkout.")
            validate_verdict(result["criteria"], job["criteria"])
        if job["role"] == "worker":
            if changed:
                git(checkout, "add", "-A", "--", *sorted(changed))
            job["tree"] = git(checkout, "write-tree")
        job["result"] = result
        self.save(job)
        return self.describe(job)


def advance(run, candidate=None):
    jobs = run.native
    previous = jobs.progress()
    try:
        if candidate:
            report = run.verify(run.approved(check_current=False), candidate)
            outcome = {**report, "evidence": str(run.directory / report["file"])}
            progress = {"status": previous.get("return_status") or previous["status"], "jobs": []}
        elif (run.directory / "approval.json").is_file():
            run.execute()
            progress = {"status": "completed", "jobs": []}
            outcome = {}
        else:
            run.prepare()
            progress = {"status": "paused", "jobs": []}
            outcome = {}
    except Pending as waiting:
        progress = {"status": "waiting_for_agents", "jobs": waiting.jobs}
        if candidate:
            progress.update(candidate=candidate, return_status=previous.get("return_status") or previous["status"])
        outcome = {}
    except (Error, OSError, ValueError, KeyError, TypeError) as error:
        progress = {**previous, "status": "failed", "detail": str(error)}
        if candidate:
            progress.update(candidate=candidate, return_status=previous.get("return_status") or previous["status"])
        outcome = {}
    run.save("native-progress.json", progress)
    return outcome


def result_input(path):
    if path == "-":
        return json.load(sys.stdin)
    return read_json(Path(path))
