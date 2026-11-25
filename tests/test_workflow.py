from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

REPO = Path(__file__).resolve().parents[1]
RUNNER = REPO / "plugins/sdlc/skills/sdlc/scripts/run.py"
sys.path.insert(0, str(RUNNER.parent))
from pipeline import Run
from handoff import publish
from fake_cli import planner
from unittest.mock import patch


@dataclass
class Fixture:
    root: Path
    env: dict

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True).strip()

    def invoke(self, *args, expect=0, **extra):
        result = subprocess.run(
            [sys.executable, str(RUNNER), *args], cwd=self.root,
            env={**self.env, **extra}, text=True, capture_output=True, timeout=45,
        )
        if result.returncode != expect:
            raise AssertionError((args, result))
        if result.stdout:
            return json.loads(result.stdout)
        return result.stderr

    def start(self, host="codex", max_fixes=2, **extra):
        return self.invoke("start", "--host", host, "--feature", "report", "--request",
                           "Generate an input report", "--max-fixes", str(max_fixes), **extra)

    def approve(self, start, **kwargs):
        return self.invoke("approve", start["run_id"], "--digest", start["draft_digest"], **kwargs)

    def directory(self, data):
        return next(Path(self.env["SDLC_STATE_DIR"]).glob(f"*/runs/{data['run_id']}"))

    def branch(self, data):
        return f"sdlc/report/{data['run_id']}"


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory(prefix="sdlc-tests-")
        self.addCleanup(temp.cleanup)
        parent = Path(temp.name)
        root = parent / "app"
        root.mkdir()
        binary = parent / "bin"
        binary.mkdir()
        markers = parent / "markers"
        markers.mkdir()
        for executable in ("uvx", "codex", "claude"):
            path = binary / executable
            shutil.copyfile(REPO / "tests/fake_cli.py", path)
            path.chmod(0o755)
        env = {**os.environ, "PATH": f"{binary}:{os.environ['PATH']}", "SDLC_STATE_DIR": str(parent / "state"),
               "SDLC_TEST_MARKERS": str(markers), "SPECIFY_INIT_DIR": str(root), "CLAUDECODE": "outer"}
        self.fixture = Fixture(root, env)
        self.fixture.git("init", "-q", "-b", "main")
        self.fixture.git("config", "user.name", "SDLC Test")
        self.fixture.git("config", "user.email", "sdlc-test@localhost")
        for name in ("a.txt", "b.txt", "report[1].txt", "report1.txt"):
            (root / name).write_text("0\n")
        self.fixture.git("add", ".")
        self.fixture.git("commit", "-qm", "Fixture")

    def native_start(self, host="codex"):
        return self.fixture.invoke("start", "--backend", "native", "--host", host,
                                   "--feature", "report", "--request", "Generate a report")

    def native_attach(self, run_id, job_id, agent):
        f = self.fixture
        job = f.invoke("reserve-job", run_id, "--job", job_id)["job"]
        bound = f.invoke("attach-agent", run_id, "--job", job_id, "--token", job["token"],
                         "--agent-id", agent)["job"]
        self.assertEqual(bound["agent_id"], agent)
        return bound

    def native_submit(self, data, job, result, expect=0):
        f = self.fixture
        path = f.directory(data) / "test-result.json"
        path.write_text(json.dumps(result))
        return f.invoke("submit-job", data["run_id"], "--job", job["id"], "--token", job["token"],
                        "--agent-id", job["agent_id"], "--result-file", str(path), expect=expect)

    def test_native_jobs_finish_and_repair_without_running_a_background_cli(self):
        f = self.fixture
        started = self.native_start(host="claude")
        run_id = started["run_id"]
        self.assertEqual(started["status"], "waiting_for_agents")
        self.assertEqual([job["id"] for job in started["jobs"]], ["planner"])
        self.assertFalse((f.directory(started) / "engine").exists())

        plan_job = self.native_attach(run_id, "planner", "native-planner")
        self.native_submit(started, plan_job, planner())
        review = f.invoke("resume", run_id)
        self.assertEqual(review["status"], "paused")
        self.assertEqual(review["jobs"], [])
        self.assertFalse(f.git("branch", "--list", "sdlc/*"))
        ready = f.approve(review)
        self.assertEqual([job["id"] for job in ready["jobs"]], ["initial-1-T01", "initial-1-T02"])
        first = self.native_attach(run_id, "initial-1-T01", "native-a")
        second = self.native_attach(run_id, "initial-1-T02", "native-b")
        poll = f.invoke("resume", run_id)
        self.assertEqual([job["agent_id"] for job in poll["jobs"]], ["native-a", "native-b"])
        checks = {"summary": "Implemented", "checks": ["Read the output."]}
        (Path(first["checkout"]) / "a.txt").write_text("2\n")
        (Path(second["checkout"]) / "b.txt").write_text("3\n")
        self.native_submit(started, first, checks)
        self.native_submit(started, first, checks)
        self.assertEqual([job["id"] for job in f.invoke("resume", run_id)["jobs"]], [second["id"]])
        self.native_submit(started, second, checks)

        job = f.invoke("resume", run_id)["jobs"][0]
        self.assertEqual(job["id"], "initial-2-T03")
        combine = self.native_attach(run_id, job["id"], "native-combine")
        worktree = Path(combine["checkout"])
        self.assertEqual((worktree / "a.txt").read_text(), "2\n")
        self.assertEqual((worktree / "b.txt").read_text(), "3\n")
        (worktree / "report[1].txt").write_text("4\n")
        self.native_submit(started, combine, checks)

        job = f.invoke("resume", run_id)["jobs"][0]
        verification = self.native_attach(run_id, job["id"], "native-verify-first")
        self.assertEqual(f.invoke("resume", run_id)["jobs"][0]["agent_id"], "native-verify-first")
        criteria = [{"id": "AC-01", "check": "Sum a and b", "observed": "5", "status": "pass"},
                    {"id": "AC-02", "check": "Read report", "observed": "4", "status": "fail"}]
        self.native_submit(started, verification, {"criteria": criteria})
        repair = f.invoke("resume", run_id)["jobs"][0]
        self.assertEqual(repair["id"], "repair-1-T03")
        self.assertIn("Verifier feedback", Path(repair["prompt_file"]).read_text())
        repair = self.native_attach(run_id, repair["id"], "native-repair")
        (Path(repair["checkout"]) / "report[1].txt").write_text("5\n")
        self.native_submit(started, repair, checks)
        job = f.invoke("resume", run_id)["jobs"][0]
        self.assertNotEqual(job["commit"], verification["commit"])
        verification = self.native_attach(run_id, job["id"], "native-verify-after-repair")
        criteria[1].update(status="pass", observed="5")
        self.native_submit(started, verification, {"criteria": criteria})
        finished = f.invoke("resume", run_id)
        self.assertEqual(finished["status"], "completed")
        self.assertEqual(f.git("show", f"{finished['commit']}:report[1].txt"), "5")
        self.assertEqual(f.git("status", "--porcelain"), "")
        self.assertFalse((f.directory(started) / "engine").exists())

        f.git("checkout", "-q", finished["branch"])
        f.git("commit", "--allow-empty", "-qm", "Different merge candidate")
        merge_commit = f.git("rev-parse", "HEAD")
        scheduled = f.invoke("verify-commit", run_id, "--commit", merge_commit)["jobs"][0]
        self.assertEqual(scheduled["commit"], merge_commit)
        separate = self.native_attach(run_id, scheduled["id"], "native-ci-verify")
        self.native_submit(started, separate, {"criteria": criteria})
        checked = f.invoke("resume", run_id)
        self.assertEqual(checked["commit"], merge_commit)
        self.assertEqual(json.loads(Path(checked["evidence"]).read_text())["commit"], merge_commit)

    def test_native_reservation_rejects_duplicate_dispatch_and_stale_results(self):
        f = self.fixture
        started = self.native_start()
        run_id = started["run_id"]
        old = self.native_attach(run_id, "planner", "old-id")
        self.assertIn("already reserved",
                      f.invoke("reserve-job", run_id, "--job", "planner", expect=1))
        self.assertIn("Stop and detach",
                      f.invoke("revise", run_id, "--request", "New request", expect=1))
        f.invoke("detach-agent", run_id, "--job", old["id"], "--token", old["token"])
        new = self.native_attach(run_id, "planner", "replacement-id")
        self.assertNotEqual(old["token"], new["token"])
        self.assertIn("currently attached", self.native_submit(started, old, planner(), expect=1))
        self.native_submit(started, new, planner())

    def test_native_worker_receipt_rejects_unapproved_paths_and_later_edits(self):
        f = self.fixture
        started = self.native_start()
        run_id = started["run_id"]
        job = self.native_attach(run_id, "planner", "plan")
        self.native_submit(started, job, planner())
        f.approve(f.invoke("resume", run_id))
        first = self.native_attach(run_id, "initial-1-T01", "worker")
        tree = Path(first["checkout"])
        extra = tree / "outside.txt"
        extra.write_text("unauthorized")
        (tree / "a.txt").write_text("2\n")
        result = {"summary": "Work", "checks": ["Read a."]}
        self.assertIn("unapproved paths", self.native_submit(started, first, result, expect=1))
        extra.unlink()
        self.native_submit(started, first, result)
        (tree / "a.txt").write_text("3\n")
        failed = f.invoke("resume", run_id, expect=1)
        self.assertIn("changed after its agent submitted", failed["detail"])
        self.assertFalse(f.git("branch", "--list", "sdlc/*"))
        second = self.native_attach(run_id, "initial-1-T02", "independent-worker")
        (Path(second["checkout"]) / "b.txt").write_text("3\n")
        submitted = self.native_submit(started, second, result)
        self.assertEqual(submitted["job"]["status"], "submitted")
        self.assertEqual(submitted["status"], "failed")

    def test_native_worker_commit_without_submitted_result_cannot_be_recovered(self):
        f = self.fixture
        started = self.native_start()
        run_id = started["run_id"]
        planner_job = self.native_attach(run_id, "planner", "plan")
        self.native_submit(started, planner_job, planner())
        f.approve(f.invoke("resume", run_id))
        job = self.native_attach(run_id, "initial-1-T01", "premature-worker")
        tree = Path(job["checkout"])
        (tree / "a.txt").write_text("2\n")
        subprocess.run(["git", "add", "a.txt"], cwd=tree, check=True)
        subprocess.run(["git", "commit", "-qm", f"SDLC {run_id} {job['id']}", "-m",
                        json.dumps({"summary": "Predates submission", "checks": ["Read a.txt"]})],
                       cwd=tree, check=True)
        failed = f.invoke("resume", run_id, expect=1)
        self.assertIn("committed without an accepted native result", failed["detail"])
        self.assertFalse(f.git("branch", "--list", "sdlc/*"))

    def test_codex_approval_parallel_dependencies_verification_and_release(self):
        f = self.fixture
        start = f.start()
        self.assertEqual(start["status"], "paused")
        self.assertEqual(f.git("branch", "--list", "sdlc/*"), "")
        self.assertEqual(f.git("status", "--porcelain"), "")
        self.assertIn("T03", (Path(start["review_dir"]) / "tasks.md").read_text())
        denied = f.invoke("approve", start["run_id"], "--digest", "invalid", expect=1)
        self.assertIn("displayed draft digest", denied)
        finished = f.approve(start)
        self.assertEqual(finished["status"], "completed")
        oid = finished["commit"]
        self.assertEqual(f.git("rev-parse", f.branch(start)), oid)
        self.assertEqual(f.git("show", f"{oid}:report[1].txt"), "5")
        self.assertEqual(f.git("show", f"{oid}:report1.txt"), "0")
        self.assertEqual(f.git("status", "--porcelain"), "")
        state = f.directory(start)
        attempts = list(state.glob("verification/*/latest.json"))
        self.assertEqual(len(attempts), 2)
        reports = [json.loads((state / json.loads(pointer.read_text())["report"]).read_text())
                   for pointer in attempts]
        self.assertTrue(any(any(row["status"] == "fail" for row in report["criteria"]) for report in reports))
        self.assertEqual(len(list(state.glob("workers/*.json"))), 4)
        f.git("checkout", "-q", f.branch(start))
        (f.root / "release.txt").write_text("pipeline\n")
        f.git("add", "release.txt")
        f.git("commit", "-qm", "Simulate a different merge candidate")
        shipped = f.git("rev-parse", "HEAD")
        evidence = f.invoke("verify-commit", start["run_id"], "--commit", shipped)["evidence"]
        out = state / "release.json"
        args = ("release", "--evidence", evidence, "--commit", shipped, "--artifact", "image@digest",
                "--owner", "Ops", "--smoke-check", "observed a 5", "--rollback", "redeploy prior artifact",
                "--out", str(out))
        incomplete = state / "incomplete-evidence.json"
        evidence_data = json.loads(Path(evidence).read_text())
        evidence_data["criteria"].pop()
        incomplete.write_text(json.dumps(evidence_data))
        incomplete_args = (args[0], args[1], str(incomplete), *args[3:])
        self.assertIn("every acceptance ID", f.invoke(*incomplete_args, "--result", "pass", expect=1))
        self.assertIn("follow-up", f.invoke(*args, "--result", "fail", expect=1))
        self.assertEqual(f.invoke(*args, "--result", "fail", "--follow-up", "Investigate alert")["post_deploy"], "fail")
        self.assertIn("already exists", f.invoke(*args, "--result", "pass", expect=1))

    def test_claude_worker_contract_and_blocked_verification_resume(self):
        f = self.fixture
        start = f.start(host="claude")
        blocked = f.approve(start, expect=1, SDLC_TEST_BLOCK="1")
        self.assertIn("Verification is blocked", blocked["detail"])
        self.assertFalse(f.git("branch", "--list", f.branch(start)))
        recovered = f.invoke("resume", start["run_id"])
        self.assertEqual(recovered["status"], "completed")

    def test_claude_subtype_success_with_error_is_rejected(self):
        failed = self.fixture.start(host="claude", expect=1, SDLC_TEST_CLAUDE_ERROR="1")
        self.assertIn("Not logged in", failed["detail"])

    def test_changes_to_approved_bytes_and_base_are_detected(self):
        f = self.fixture
        start = f.start()
        path = Path(start["review_dir"]) / "spec.md"
        original = path.read_bytes()
        path.write_bytes(original.replace(b"\n", b"\r\n"))
        self.assertIn("changed since", f.approve(start, expect=1))
        path.write_bytes(original)
        f.git("commit", "--allow-empty", "-qm", "Move base")
        self.assertIn("starting commit changed", f.approve(start, expect=1))
        replacement = f.invoke("revise", start["run_id"], "--request", "Revise report")
        self.assertNotEqual(start["run_id"], replacement["run_id"])
        self.assertEqual(f.invoke("status", start["run_id"])["superseded_by"], replacement["run_id"])

    def test_out_of_scope_worker_edits_stop_before_integration(self):
        f = self.fixture
        start = f.start()
        failed = f.approve(start, expect=1, SDLC_TEST_SCOPE="1")
        self.assertIn("unapproved paths", failed["detail"])
        self.assertEqual(f.git("status", "--porcelain"), "")
        self.assertFalse(f.git("branch", "--list", f.branch(start)))

    def test_failed_verification_stops_without_repair_budget(self):
        f = self.fixture
        start = f.start(max_fixes=0)
        failed = f.approve(start, expect=1)
        self.assertIn("failed after 0 repairs", failed["detail"])
        self.assertFalse(f.git("branch", "--list", f.branch(start)))

    def test_recover_stale_engine_without_approval(self):
        f = self.fixture
        interrupted = f.start(expect=1, SDLC_TEST_INTERRUPT="1")
        self.assertEqual(interrupted["status"], "startup_failed")
        self.assertEqual(f.invoke("status", interrupted["run_id"])["status"], "running")
        resumed = f.invoke("recover", interrupted["run_id"])
        self.assertEqual(resumed["status"], "paused")
        self.assertFalse((f.directory(interrupted) / "approval.json").exists())
        self.assertFalse(f.git("branch", "--list", f.branch(interrupted)))

    def test_approval_reports_startup_failure_even_when_checkpoint_is_paused(self):
        f = self.fixture
        started = f.start()
        failed = f.approve(started, expect=1, SDLC_TEST_ENGINE_STARTUP_FAIL="1")
        self.assertEqual(failed["status"], "startup_failed")
        self.assertEqual(failed["engine_status"], "paused")
        status = f.invoke("status", started["run_id"])
        self.assertEqual(status["status"], "paused")
        self.assertIn("Fixture could not start", status["last_attempt_error"])
        self.assertEqual(f.invoke("resume", started["run_id"])["status"], "completed")

    def test_publish_targets_the_selected_repo_and_rechecks_remote_commit(self):
        f = self.fixture
        started = f.start()
        complete = f.approve(started)
        run = Run(f.directory(started))
        route = "github.example.test/owner/project"
        url = f"https://{route}/pull/42"
        oid = complete["commit"]

        def simulated_gh(_repo, *args):
            self.assertIn(route, args)
            if args[:2] == ("pr", "list"):
                return "[]"
            if args[:2] == ("pr", "create"):
                body = Path(args[args.index("--body-file") + 1]).read_text()
                self.assertIn("Read report[1].txt", body)
                return url + "\n"
            self.assertEqual(args[:2], ("pr", "view"))
            return json.dumps({"headRefOid": oid, "baseRefName": "main", "url": url})

        from common import git as real_git
        def simulated_git(repo, *args):
            if args == ("remote",):
                return "release"
            if args[:4] == ("remote", "get-url", "--push", "--all"):
                self.assertEqual(args[-1], "release")
                return "git@github.example.test:owner/project.git"
            if args[0] == "push":
                self.assertEqual(args, ("push", "--", "release", f"{oid}:refs/heads/{complete['branch']}"))
                return ""
            return real_git(repo, *args)

        with patch("handoff.gh", side_effect=simulated_gh), patch("handoff.git", side_effect=simulated_git):
            self.assertEqual(publish(run, "release", "main")["pr"], url)

        f.invoke("verify-commit", started["run_id"], "--commit", oid, expect=1, SDLC_TEST_BAD_VERDICT="1")
        self.assertFalse(f.invoke("status", started["run_id"])["handoff_usable"])
        self.assertIn("latest passing", f.invoke("open-pr", started["run_id"], expect=1))

    def test_status_prefers_actual_engine_checkpoint_after_wrapper_crash(self):
        f = self.fixture
        started = f.start()
        path = next(f.directory(started).glob("engine/attempt-*/.specify/workflows/runs/*/state.json"))
        state = json.loads(path.read_text())
        path.write_text(json.dumps({**state, "status": "failed"}))
        self.assertEqual(f.invoke("status", started["run_id"], expect=1)["status"], "failed")

    def test_worker_commit_is_recovered_when_receipt_is_missing(self):
        f = self.fixture
        data = f.start()
        f.approve(data)
        run = Run(f.directory(data))
        draft = run.approved()
        task = draft["plan"]["tasks"][0]
        path = run.directory / "workers/initial-1-T01.json"
        prior = json.loads(path.read_text())
        path.unlink()
        self.assertEqual(run.worker(task, "initial-1", run.base, draft, []), prior)

    def test_rejection_never_launches_workers(self):
        f = self.fixture
        started = f.start()
        result = f.invoke("reject", started["run_id"])
        self.assertEqual(result["status"], "aborted")
        self.assertFalse(list(f.directory(started).glob("workers/*.json")))


if __name__ == "__main__":
    unittest.main()
