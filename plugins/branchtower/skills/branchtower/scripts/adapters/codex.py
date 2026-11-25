from pathlib import Path
from tempfile import TemporaryDirectory

from adapters import environment
from common import command, read_json, require


def invoke(cwd, role, prompt, schema_path, protected, scratch, fds=()):
    mode = "workspace-write" if role == "worker" else "read-only"
    with TemporaryDirectory(prefix="sdlc-codex-", dir=scratch) as folder:
        result_path = Path(folder) / "response.json"
        result = command(
            ["codex", "-a", "never", "exec", "--ephemeral", "--sandbox", mode, "--cd", str(cwd),
             "--output-schema", str(schema_path), "--output-last-message", str(result_path), "-"],
            cwd, env=environment(), stdin=prompt, timeout=1200, fds=fds,
        )
        require(result.returncode == 0, f"Codex {role} failed: {result.stderr.strip()[-1600:]}")
        require(result_path.is_file(), f"Codex {role} returned no structured result.")
        return read_json(result_path)
