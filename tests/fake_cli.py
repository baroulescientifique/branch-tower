#!/usr/bin/env python3
"""Process doubles for Spec Kit and the two host command-line contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def workflow(args):
    assert "git+https://github.com/github/spec-kit.git@c8133f463a586879e3af6dfd2864beb8995a8764" in args
    assert "--json" in args
    assert "SPECIFY_INIT_DIR" not in os.environ
    action = args[args.index("workflow") + 1]
    inputs = {pair.split("=", 1)[0]: pair.split("=", 1)[1]
              for index, option in enumerate(args[:-1]) if option == "--input"
              for pair in [args[index + 1]]}
    if os.environ.get("SDLC_TEST_ENGINE_STARTUP_FAIL") == "1":
        sys.stderr.write("Fixture could not start Spec Kit.\n")
        return 1
    state = Path(".specify/workflows/runs/test-run/state.json")
    if action == "run":
        data = {"run_id": "test-run", "run_dir": inputs["run_dir"], "status": "running"}
    else:
        data = json.loads(state.read_text())
        assert data["status"] in ("paused", "failed")
    write(state, data)
    if os.environ.get("SDLC_TEST_INTERRUPT") == "1":
        return 1

    def phase(name):
        run = Path(data["run_dir"])
        file = run / "skill/scripts/workflow.yml"
        command = next(line.strip().removeprefix("run: '").removesuffix("'")
                       for line in file.read_text().splitlines() if f"_phase {name}'" in line)
        return subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True,
            env={**os.environ, "SDLC_RUN_DIR": str(run), "SPECKIT_WORKFLOW_DIR": str(file.parent)},
        )

    step = phase("prepare")
    if step.returncode:
        data["status"] = "failed"
    elif inputs.get("review") == "reject":
        data["status"] = "aborted"
    elif inputs.get("review") == "approve":
        data["status"] = "failed" if phase("execute").returncode else "completed"
    else:
        data["status"] = "paused"
    write(state, data)
    print(json.dumps({"run_id": data["run_id"], "status": data["status"]}))
    return int(data["status"] == "failed")


def planner():
    return {
        "spec": "Render the comparison report.",
        "plan": "Update inputs independently. Combine both before rendering.",
        "criteria": [
            {"id": "AC-01", "behavior": "Input values combine to 5", "check": "Read a and b."},
            {"id": "AC-02", "behavior": "The report equals 5", "check": "Read report[1].txt."},
        ],
        "tasks": [
            {"id": "T01", "description": "Write a.", "paths": ["a.txt"],
             "depends_on": [], "criteria": ["AC-01"]},
            {"id": "T02", "description": "Write b.", "paths": ["b.txt"],
             "depends_on": [], "criteria": ["AC-01"]},
            {"id": "T03", "description": "Render report.", "paths": ["report[1].txt"],
             "depends_on": ["T01", "T02"], "criteria": ["AC-02"]},
        ],
    }


def agent(prompt):
    if "Feature request\n" in prompt:
        return planner()
    if "Commit under test\n" in prompt:
        blocked = os.environ.get("SDLC_TEST_BLOCK") == "1"
        assert Path.cwd().name == "checkout"
        targeted = int(Path("a.txt").read_text()) + int(Path("b.txt").read_text())
        output = Path("report[1].txt").read_text()
        if os.environ.get("SDLC_TEST_BAD_VERDICT") == "1":
            targeted = 0
            output = "0\n"
        return {"criteria": [
            {"id": "AC-01", "status": ("blocked" if blocked else "pass" if targeted == 5 else "fail"),
             "check": "Read a.txt and b.txt", "observed": f"Sum {targeted}"},
            {"id": "AC-02", "status": ("blocked" if blocked else "pass" if output == "5\n" else "fail"),
             "check": "Read report[1].txt", "observed": repr(output)},
        ]}
    assigned = json.JSONDecoder().raw_decode(prompt.split("Assigned task\n", 1)[1])[0]
    path = Path(assigned["paths"][0])
    task = assigned["id"]
    if task in ("T01", "T02"):
        markers = Path(os.environ["SDLC_TEST_MARKERS"])
        (markers / task).touch()
        other = "T02" if task == "T01" else "T01"
        until = time.monotonic() + 8
        while not (markers / other).exists():
            assert time.monotonic() < until, "Independent worker was not started in parallel"
            time.sleep(0.025)
        path.write_text("2\n" if task == "T01" else "3\n")
        if os.environ.get("SDLC_TEST_SCOPE") == "1" and task == "T01":
            Path("unowned.txt").write_text("outside scope")
    else:
        assert (Path("a.txt").read_text(), Path("b.txt").read_text()) == ("2\n", "3\n")
        repaired = "Verifier feedback for this repair" in prompt
        path.write_text("5\n" if repaired else "4\n")
    return {"summary": f"Task {task}", "checks": [f"Read {path} after writing it."]}


def host(executable, args):
    prompt = sys.stdin.read()
    value = agent(prompt)
    if executable == "codex":
        assert "--ephemeral" in args
        assert args[args.index("--sandbox") + 1] in ("read-only", "workspace-write")
        assert Path(args[args.index("--cd") + 1]).resolve() == Path.cwd()
        output = args[args.index("--output-last-message") + 1]
        write(Path(output), value)
    else:
        assert "--restricted" in args
        assert "--strict-mcp-config" in args
        assert "CLAUDECODE" not in os.environ
        settings = json.loads(args[args.index("--settings") + 1])
        assert settings["sandbox"]["failIfUnavailable"]
        if os.environ.get("SDLC_TEST_CLAUDE_ERROR") == "1":
            print(json.dumps({"subtype": "success", "is_error": True, "result": "Not logged in"}))
            return 0
        print(json.dumps({"subtype": "success", "is_error": False, "structured_output": value}))
    return 0


if __name__ == "__main__":
    name = Path(sys.argv[0]).name
    sys.exit(workflow(sys.argv[1:]) if name == "uvx" else host(name, sys.argv[1:]))
