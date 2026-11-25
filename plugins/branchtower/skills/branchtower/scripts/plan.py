import hashlib
from pathlib import PurePosixPath
import re

from common import encode, require

DOCS = ("spec.md", "plan.md", "tasks.md", "tasks.json")


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def text(value):
    return isinstance(value, str) and bool(value.strip())


def unique_ids(rows, pattern):
    ids = [row["id"] for row in rows]
    require(ids and all(isinstance(item, str) and re.fullmatch(pattern, item) for item in ids)
            and len(set(ids)) == len(ids), f"Expected unique identifiers matching {pattern}.")
    return set(ids)


def exact_path(value):
    if not isinstance(value, str):
        return False
    path = PurePosixPath(value)
    parts = [part.casefold() for part in path.parts]
    return (bool(parts) and not path.is_absolute() and path.as_posix() == value
            and not {".git", ".sdlc", ".."}.intersection(parts)
            and parts[0] != "specs"
            and "\\" not in value and not any(ord(char) < 32 for char in value))


def batches(tasks):
    pending = {task["id"]: set(task["depends_on"]) for task in tasks}
    complete = set()
    while pending:
        ready = sorted(task for task, dependencies in pending.items() if dependencies <= complete)
        require(ready, "The task dependency graph contains a cycle.")
        yield ready
        for task in ready:
            del pending[task]
        complete.update(ready)


def validate(value):
    require(text(value["spec"]) and text(value["plan"]), "The spec and plan need text.")
    criteria = unique_ids(value["criteria"], r"AC-[0-9]{2,}")
    tasks = unique_ids(value["tasks"], r"T[0-9]{2,}")
    require(all(text(item["behavior"]) and text(item["check"]) for item in value["criteria"]),
            "Every acceptance criterion needs behavior and a check.")
    covered = set()
    owned = set()
    for task in value["tasks"]:
        links = set(task["criteria"])
        depends = set(task["depends_on"])
        paths = task["paths"]
        require(text(task["description"]) and links and links <= criteria,
                f"{task['id']} needs a description and known acceptance IDs.")
        require(depends <= tasks - {task["id"]}, f"{task['id']} has invalid dependencies.")
        require(paths and all(exact_path(path) for path in paths),
                f"{task['id']} needs exact repository-relative paths outside specs/.")
        for path in map(str.casefold, paths):
            require(all(other != path and not other.startswith(path + "/")
                        and not path.startswith(other + "/") for other in owned),
                    f"More than one file assignment overlaps {path}.")
            owned.add(path)
        covered.update(links)
    require(covered == criteria, "Every acceptance criterion needs an owning task.")
    list(batches(value["tasks"]))


def render_tasks(templates, tasks):
    blocks = []
    for task in tasks:
        paths = ", ".join(f"`{path}`" for path in task["paths"])
        dependencies = ", ".join(task["depends_on"]) or "None"
        criteria = ", ".join(task["criteria"])
        blocks.append(f"## {task['id']}\n\n{task['description']}\n\n"
                      f"- Files: {paths}\n- Prerequisites: {dependencies}\n- Acceptance: {criteria}")
    return (templates / "tasks.md").read_text(encoding="utf-8").format(tasks="\n\n".join(blocks))


def render(templates, value):
    criteria = "\n".join(f"- **{row['id']}** {row['behavior']} Check: {row['check']}"
                         for row in value["criteria"])
    return {
        "spec.md": (templates / "spec.md").read_text(encoding="utf-8").format(
            spec=value["spec"], criteria=criteria),
        "plan.md": (templates / "plan.md").read_text(encoding="utf-8").format(plan=value["plan"]),
        "tasks.md": render_tasks(templates, value["tasks"]),
        "tasks.json": encode({"criteria": value["criteria"], "tasks": value["tasks"]}),
    }
