from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import uuid

from adapters import claude, codex, validate
from common import ancestor, changed, checkout, commit, encode, git, head, lock, read_json
from common import git_output, require, status, write
from handoff import markdown
from native import Jobs, Pending, validate_verdict
from plan import DOCS, batches, digest, render, text, validate as validate_plan


class Run:
    def __init__(self, directory):
        self.directory = directory.resolve()
        self.data = read_json(self.directory / "run.json")
        self.repo = Path(self.data["repo"])
        self.git_dir = Path(self.data["git_dir"])
        self.skill = self.directory / "skill"
        self.scratch = self.directory / "scratch"
        self.scratch.mkdir(exist_ok=True)
        self.base = self.data["base"]
        self.feature_dir = Path("specs") / self.data["feature"]
        self.review_tree = self.directory / "worktrees" / "review"
        self.candidate = self.directory / "worktrees" / "candidate"
        self.templates = self.skill / "assets" / "templates"
        self.host = {"codex": codex, "claude": claude}[self.data["host"]]
        self.native = Jobs(self) if self.data.get("backend") == "native" else None

    def read(self, name):
        return read_json(self.directory / name)

    def save(self, name, value):
        write(self.directory / name, encode(value))

    def agent(self, role, cwd, context, schema, job_id=None, *, paths=(), criteria=()):
        references = self.skill / "references"
        instructions = [
            (references / f"{name}.md").read_text(encoding="utf-8")
            for name in ("rules", role)
        ]
        prompt = "\n\n".join([*instructions, *context])
        schema_path = self.skill / "assets" / "schemas" / f"{schema}.json"
        if self.native:
            return self.native.request(job_id, role, cwd, prompt, schema_path, paths=paths, criteria=criteria)
        result = self.host.invoke(
            cwd, role, prompt, schema_path, (self.repo, self.git_dir, self.skill), self.scratch,
            fds=(self.phase_fd,) if hasattr(self, "phase_fd") else (),
        )
        validate(result, read_json(schema_path))
        return result

    def review_file(self, tree, name):
        path = tree / self.feature_dir / name
        require(path.resolve() == path, f"Expected a regular review path at {path}.")
        return path

    def prepare(self):
        checkout(self.repo, self.review_tree, self.base)
        draft_path = self.directory / "draft.json"
        if not draft_path.exists():
            check_files = [self.review_file(self.review_tree, name) for name in DOCS]
            require(not any(path.exists() for path in check_files),
                    f"Review files already exist at {self.feature_dir} in the starting commit.")
            require(head(self.review_tree) == self.base and not status(self.review_tree),
                    "The planner needs a clean checkout of the starting commit.")
            value = self.agent(
                "planner", self.review_tree, ["Feature request\n" + self.data["request"]], "plan", "planner",
            )
            require(head(self.review_tree) == self.base and not status(self.review_tree),
                    "The planner changed its checkout.")
            validate_plan(value)
            draft = {"run": self.data, "files": render(self.templates, value), "plan": value}
            self.save("draft.json", draft)
        draft = self.read("draft.json")
        for name, contents in draft["files"].items():
            path = self.review_file(self.review_tree, name)
            if not path.exists():
                write(path, contents)
        self.check_draft(draft)
        return {"review_dir": str(self.review_tree / self.feature_dir), "digest": digest(draft)}

    def check_draft(self, draft):
        require(draft["run"] == self.data, "The stored draft belongs to a different run.")
        require(head(self.repo) == self.base, "The starting commit changed. Revise the run.")
        require(head(self.review_tree) == self.base, "The review checkout changed. Revise the run.")
        for name, contents in draft["files"].items():
            path = self.review_file(self.review_tree, name)
            require(path.is_file() and path.read_bytes() == contents.encode("utf-8"),
                    f"{path.name} changed since the draft was prepared. Revise the run.")

    def approve(self, review_digest):
        require(not (self.directory / "rejected.json").exists(), "This run was rejected.")
        draft = self.read("draft.json")
        require(digest(draft) == review_digest, "Approval must identify the displayed draft digest.")
        self.check_draft(draft)
        self.save("approval.json", {"run": self.data["id"], "digest": review_digest})

    def approved(self, check_current=True):
        require(not (self.directory / "rejected.json").exists(), "This run was rejected.")
        require((self.directory / "approval.json").is_file(), "The draft needs explicit approval.")
        draft = self.read("draft.json")
        saved = self.read("approval.json")
        require(saved == {"run": self.data["id"], "digest": digest(draft)}, "Invalid approval record.")
        if check_current:
            self.check_draft(draft)
        return draft

    def worker(self, task, key, base, draft, feedback):
        task_id = task["id"]
        name = f"{key}-{task_id}"
        tree = self.directory / "worktrees" / name
        record_path = f"workers/{name}.json"
        title = f"BranchTower {self.data['id']} {name}"
        approved_paths = set(task["paths"])
        with lock(self.directory / "locks" / f"{name}.lock"):
            checkout(self.repo, tree, base)
            current = head(tree)
            if (self.directory / record_path).exists():
                receipt = self.read(record_path)
                require(receipt["base"] == base and receipt["commit"] == current,
                        f"The saved worker result changed for {task_id}.")
            elif current == base:
                require(status(tree) <= approved_paths, f"{task_id} has edits outside its assignment.")
                result = self.agent("worker", tree, [
                    "Assigned task\n" + encode(task), "Approved spec\n" + draft["files"]["spec.md"],
                    "Approved plan\n" + draft["files"]["plan.md"],
                    "Verifier feedback for this repair\n" + encode(feedback) if feedback else
                    "Implement the assigned task.",
                ], "worker", name, paths=task["paths"])
                require(text(result["summary"]) and result["checks"]
                        and all(text(check) for check in result["checks"]),
                        f"{task_id} returned an incomplete handoff.")
                require(head(tree) == base, f"{task_id} changed its starting commit.")
                paths = status(tree)
                require(paths <= approved_paths,
                        f"{task_id} changed unapproved paths: {sorted(paths - approved_paths)}.")
                require(paths or feedback, f"{task_id} produced no implementation changes.")
                if paths:
                    git(tree, "add", "-A", "--", *sorted(paths))
                if self.native:
                    require(git(tree, "write-tree") == self.native.read(name)["tree"],
                            f"{task_id} changed after its agent submitted the result.")
                if paths:
                    current = commit(tree, title, encode(result))
                else:
                    self.save(record_path, {"task": task_id, "base": base, "commit": base,
                                            "files": [], "report": result})
            if current != base:
                parents = git(tree, "rev-list", "--parents", "-n", "1", current).split()
                require(parents == [current, base] and git(tree, "show", "-s", "--format=%s", current) == title
                        and not status(tree), f"{task_id} has an unexpected commit or dirty checkout.")
                files = changed(tree, base, current)
                require(files and files <= approved_paths, f"{task_id} committed unapproved files.")
                result = json.loads(git(tree, "show", "-s", "--format=%b", current))
                if self.native:
                    job = self.native.read(name)
                    require(job["result"] is not None,
                            f"{task_id} committed without an accepted native result.")
                    require(result == job["result"] and git(tree, "rev-parse", f"{current}^{{tree}}") == job["tree"],
                            f"{task_id} committed a different result than the native handoff.")
                self.save(record_path, {"task": task_id, "base": base, "commit": current,
                                        "files": sorted(files), "report": result})
            require(not status(tree), f"{task_id} left a dirty checkout.")
            return self.read(record_path)

    def wave(self, key, tasks, draft, feedback=(), base=None):
        name = f"waves/{key}.json"
        if (self.directory / name).exists():
            record = self.read(name)
        else:
            record = {"base": head(self.candidate), "result": None}
            require(base is None or record["base"] == base, "The repair candidate changed.")
            self.save(name, record)
        if record["result"] is not None:
            require(ancestor(self.candidate, record["result"], head(self.candidate)),
                    "A saved integration result is missing from the candidate.")
            return record["result"]
        require(ancestor(self.candidate, record["base"], head(self.candidate)),
                "The integration checkout changed its starting point.")
        if self.native:
            receipts = []
            waiting = []
            for task in tasks:
                try:
                    receipts.append(self.worker(task, key, record["base"], draft, feedback))
                except Pending as pending:
                    waiting.extend(pending.jobs)
                    if len(waiting) == 2:
                        break
            if waiting:
                raise Pending(waiting)
        else:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(self.worker, task, key, record["base"], draft, feedback)
                           for task in tasks]
                receipts = [future.result() for future in futures]
        require(any(row["files"] for row in receipts), "No approved worker could correct the failure.")
        for receipt in receipts:
            if not ancestor(self.candidate, receipt["commit"], head(self.candidate)):
                require(not status(self.candidate), "The integration checkout has uncommitted changes.")
                git(self.candidate, "merge", "--no-edit", "--no-gpg-sign", receipt["commit"])
        record["result"] = head(self.candidate)
        self.save(name, record)
        return record["result"]

    def implement(self, draft):
        checkout(self.repo, self.candidate, self.base)
        if (self.directory / "integration.json").exists():
            return self.read("integration.json")["commit"]
        tasks = {row["id"]: row for row in draft["plan"]["tasks"]}
        for index, group in enumerate(batches(tasks.values()), 1):
            self.wave(f"initial-{index}", [tasks[task] for task in group], draft)
        paths = [str(self.feature_dir / name) for name in DOCS]
        require(status(self.candidate) <= set(paths), "The integration checkout contains unrelated edits.")
        for name, contents in draft["files"].items():
            path = self.review_file(self.candidate, name)
            if path.exists():
                require(path.read_bytes() == contents.encode("utf-8"), f"Unexpected spec content at {path}.")
            else:
                write(path, contents)
        git(self.candidate, "add", "--", *paths)
        if status(self.candidate):
            commit(self.candidate, f"Record approved spec for {self.data['feature']}")
        result = head(self.candidate)
        self.save("integration.json", {"commit": result})
        return result

    def verify(self, draft, candidate, reuse=False):
        prefix = f"verification/{candidate}"
        latest = self.directory / prefix / "latest.json"
        if reuse and latest.is_file():
            saved = self.read(read_json(latest)["report"])
            if all(row["status"] != "blocked" for row in saved["criteria"]):
                return saved
        for name, expected in draft["files"].items():
            actual = git_output(self.repo, "show", f"{candidate}:{self.feature_dir.as_posix()}/{name}")
            require(actual == expected.encode("utf-8"),
                    f"The tested commit lacks the approved contents of {name}.")
        criteria = [row["id"] for row in draft["plan"]["criteria"]]
        context = [f"Commit under test\n{candidate}", "Approved specification\n" + draft["files"]["spec.md"]]
        if self.native:
            # Keep the same checkout and identity while the parent chat waits for the verifier.
            sequence = self.latest_report(candidate).get("attempt", 0) + 1 if latest.is_file() else 1
            job_id = f"verify-{candidate}-{sequence}"
            tree = self.directory / "worktrees" / job_id
            checkout(self.repo, tree, candidate)
            result = self.agent("verifier", tree, context, "verification", job_id, criteria=criteria)
            require(head(tree) == candidate and not status(tree), "The verifier changed its checkout.")
        else:
            with TemporaryDirectory(prefix="verify-", dir=self.scratch) as folder:
                tree = Path(folder) / "checkout"
                checkout(self.repo, tree, candidate)
                try:
                    result = self.agent("verifier", tree, context, "verification")
                    require(head(tree) == candidate and not status(tree), "The verifier changed its checkout.")
                finally:
                    git(self.repo, "worktree", "remove", "--force", "--", str(tree))
        rows = result["criteria"]
        validate_verdict(rows, criteria)
        identifier = job_id if self.native else uuid.uuid4().hex
        name = f"{prefix}/{identifier}.json"
        report = {"run_id": self.data["id"], "repo": str(self.repo), "commit": candidate,
                  "feature": self.data["feature"], "approved_digest": digest(draft), "criteria": rows, "file": name}
        if self.native:
            report["attempt"] = sequence
        self.save(name, report)
        self.save(f"{prefix}/latest.json", {"report": name})
        self.save("latest-verification.json", {"report": name})
        return report

    def latest_report(self, candidate):
        name = f"verification/{candidate}/latest.json"
        return self.read(self.read(name)["report"])

    def handoff(self, draft, candidate, report):
        self.check_draft(draft)
        require(head(self.candidate) == candidate and not status(self.candidate),
                "The candidate changed after verification.")
        allowed = {path for row in draft["plan"]["tasks"] for path in row["paths"]}
        allowed |= {str(self.feature_dir / name) for name in DOCS}
        require(ancestor(self.repo, self.base, candidate) and changed(self.repo, self.base, candidate) <= allowed,
                "The candidate contains changes outside the approved plan.")
        branch = f"branchtower/{self.data['feature']}/{self.data['id']}"
        branches = git(self.repo, "branch", "--list", branch)
        if branches:
            require(git(self.repo, "rev-parse", f"refs/heads/{branch}") == candidate,
                    f"Existing branch {branch} points to another commit.")
        else:
            git(self.repo, "branch", branch, candidate)
        evidence = self.directory / report["file"]
        result = {"branch": branch, "commit": candidate, "evidence": str(evidence)}
        write(self.directory / "handoff.md", markdown(self.data["feature"], report))
        self.save("handoff.json", result)
        return result

    def execute(self):
        draft = self.approved()
        candidate = self.implement(draft)
        maximum = self.data["max_fixes"]
        for attempt in range(maximum + 1):
            report = self.verify(draft, candidate, reuse=True)
            failed = [row for row in report["criteria"] if row["status"] != "pass"]
            if not failed:
                return self.handoff(draft, candidate, report)
            evidence = self.directory / report["file"]
            require(all(row["status"] == "fail" for row in failed),
                    f"Verification is blocked. See {evidence}. Resolve the blocker and resume.")
            require(attempt < maximum, f"Verification failed after {maximum} repairs. See {evidence}.")
            failed_ids = {row["id"] for row in failed}
            tasks = [row for row in draft["plan"]["tasks"] if failed_ids.intersection(row["criteria"])]
            candidate = self.wave(f"repair-{attempt + 1}", tasks, draft, failed, base=candidate)
