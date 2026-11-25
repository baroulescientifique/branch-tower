from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import signal
import subprocess
from tempfile import NamedTemporaryFile


class Error(Exception):
    """A failure an operator can act on."""


def require(condition, message):
    if not condition:
        raise Error(message)


def encode(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, text, *, exclusive=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            if exclusive:
                try:
                    os.link(temporary, path)
                except FileExistsError as error:
                    raise Error(f"The record already exists at {path}.") from error
            else:
                temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


@contextmanager
def lock(path, wait=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError as error:
            raise Error(f"Another process is using {path.parent}.") from error
        yield handle.fileno()


def command(args, cwd, *, env=None, stdin=None, timeout=None, fds=()):
    with subprocess.Popen(
        args, cwd=cwd, env=env, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True, pass_fds=fds,
    ) as process:
        try:
            stdout, stderr = process.communicate(stdin, timeout)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            try:
                os.killpg(process.pid, signal.SIGINT)
                process.communicate(timeout=5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
            raise
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def git_env():
    local = {
        "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_DIR",
        "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX", "GIT_NAMESPACE",
    }
    return {key: value for key, value in os.environ.items() if key not in local}


def git_output(repo, *args):
    result = subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *args],
        cwd=repo, env=git_env(), capture_output=True,
    )
    require(result.returncode == 0, result.stderr.decode("utf-8", "replace").strip()
            or f"Git {args[0]} failed.")
    return result.stdout


def git(repo, *args):
    return git_output(repo, *args).decode("utf-8", "surrogateescape").removesuffix("\n")


def root(path):
    return Path(git(path, "rev-parse", "--show-toplevel")).resolve()


def git_dir(path):
    return Path(git(path, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()


def head(path):
    return git(path, "rev-parse", "--verify", "HEAD^{commit}")


def status(path):
    changes = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--no-renames")
    return {entry[3:] for entry in changes.split("\0") if entry}


def changed(path, start, end):
    listing = git(path, "diff", "--name-only", "-z", "--no-renames", start, end, "--")
    return {name for name in listing.split("\0") if name}


def ancestor(path, old, new):
    return not git(path, "rev-list", f"{new}..{old}", "--")


def checkout(repo, path, commit):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--detach", "--", str(path), commit)
    require(path.is_dir() and not path.is_symlink() and root(path) == path
            and git_dir(path) == git_dir(repo), f"Invalid Git worktree at {path}.")


def commit(path, title, body=None):
    args = ["commit", "--no-gpg-sign", "--cleanup=verbatim", "-m", title]
    if body:
        args.extend(["-m", body])
    git(path, *args)
    return head(path)
