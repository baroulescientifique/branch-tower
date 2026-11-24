from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from common import command, encode, git, git_output, read_json, require, write
from plan import digest, text


def remote_repo(repo, remote):
    require(remote in git(repo, "remote").splitlines(), f"Unknown Git remote {remote}.")
    urls = git(repo, "remote", "get-url", "--push", "--all", remote).splitlines()
    require(len(urls) == 1, f"Use a remote with exactly one push URL for a PR: {remote}.")
    scp = re.fullmatch(r"(?:[^@/]+@)?([a-zA-Z0-9.-]+):/?([^/]+/[^/]+)", urls[0])
    if scp:
        hostname, path = scp.groups()
    else:
        parsed = urlsplit(urls[0])
        require(parsed.scheme in {"https", "ssh"} and parsed.hostname,
                "The PR remote needs a GitHub HTTPS or SSH URL.")
        hostname, path = parsed.hostname, parsed.path.strip("/")
    path = path.removesuffix(".git")
    require(re.fullmatch(r"[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+", path),
            "The PR remote needs an owner and repository name.")
    return f"{hostname}/{path}"


def gh(repo, *args):
    result = command(["gh", *args], repo, timeout=120)
    require(result.returncode == 0, result.stderr.strip() or f"GitHub CLI {args[0]} failed.")
    return result.stdout


def passing_report(run, result):
    branch, oid = result["branch"], result["commit"]
    draft = run.approved(check_current=False)
    require(git(run.repo, "rev-parse", f"refs/heads/{branch}") == oid,
            "The handoff branch moved. Publish the original handoff or start a new run.")
    report = run.latest_report(oid)
    criteria = [row["id"] for row in draft["plan"]["criteria"]]
    rows = report["criteria"]
    require(report["run_id"] == run.data["id"] and report["approved_digest"] == digest(draft)
            and report["commit"] == oid and sorted(row["id"] for row in rows) == sorted(criteria)
            and all(row["status"] == "pass" and text(row["check"]) and text(row["observed"]) for row in rows),
            "Publishing requires the latest passing evidence for the exact branch commit.")
    return report


def markdown(feature, report):
    return (f"# {feature}\n\nVerified commit `{report['commit']}`\n\n"
            f"Approved draft `{report['approved_digest']}`\n\n"
            + "\n".join(f"- **{row['id']}** {row['check']}\n  Observed {row['observed']}"
                        for row in report["criteria"]) + "\n")


def publish(run, remote, base):
    result = run.read("handoff.json")
    report = passing_report(run, result)
    branch, oid = result["branch"], result["commit"]
    target = remote_repo(run.repo, remote)
    if base is None:
        lookup = gh(run.repo, "repo", "view", target, "--json", "defaultBranchRef")
        base = json.loads(lookup)["defaultBranchRef"]["name"]
    git(run.repo, "check-ref-format", "--branch", base)
    existing = json.loads(gh(run.repo, "pr", "list", "--repo", target, "--base", base, "--head", branch,
                             "--state", "open", "--json", "url,headRefOid"))
    if existing:
        require(len(existing) == 1 and existing[0]["headRefOid"] == oid,
                "The existing PR does not match the verified head.")
        return {**result, "evidence": str(run.directory / report["file"]), "pr": existing[0]["url"]}

    git(run.repo, "push", "--", remote, f"{oid}:refs/heads/{branch}")
    body = run.directory / "pr-body.md"
    write(body, markdown(run.data["feature"], report))
    opened = gh(run.repo, "pr", "create", "--repo", target, "--base", base, "--head", branch,
                "--title", run.data["feature"].replace("-", " "), "--body-file", str(body)).strip()
    created = json.loads(gh(run.repo, "pr", "view", opened, "--repo", target,
                            "--json", "url,headRefOid,baseRefName"))
    require(created["headRefOid"] == oid and created["baseRefName"] == base,
            "GitHub reports a different commit or base for the new PR.")
    return {**result, "evidence": str(run.directory / report["file"]), "pr": created["url"]}


def release(repo, args):
    report = read_json(Path(args.evidence))
    require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", args.commit), "Use the full shipped commit ID.")
    commit = git(repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}")
    feature = report["feature"]
    require(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", feature), "Invalid feature in verification evidence.")
    manifest = json.loads(git_output(repo, "show", f"{commit}:specs/{feature}/tasks.json"))
    expected = [row["id"] for row in manifest["criteria"]]
    rows = report["criteria"]
    ids = [row["id"] for row in rows]
    require(report["commit"] == commit and rows and len(set(expected)) == len(expected)
            and sorted(ids) == sorted(expected)
            and all(row["status"] == "pass" and text(row["check"]) and text(row["observed"]) for row in rows)
            and re.fullmatch(r"[0-9a-f]{64}", report["approved_digest"]),
            "The shipped commit needs passing evidence for every acceptance ID at that exact revision.")
    require(all(text(value) for value in (args.artifact, args.owner, args.smoke_check, args.rollback)),
            "The artifact, owner, smoke check, and rollback procedure are required.")
    require(args.result != "fail" or text(args.follow_up),
            "Provide a follow-up for a failed post-deploy check.")
    record = {
        "time": datetime.now(timezone.utc).isoformat(), "commit": commit,
        "artifact": args.artifact, "owner": args.owner, "rollback": args.rollback,
        "post_deploy": {"result": args.result, "evidence": args.smoke_check, "follow_up": args.follow_up},
        "verification": report,
    }
    output = Path(args.out).absolute()
    write(output, encode(record), exclusive=True)
    return {"release_record": str(output), "post_deploy": args.result}
