import json

from adapters import environment
from common import command, encode, require


def invoke(cwd, role, prompt, schema_path, protected, scratch, fds=()):
    # File tools are confined by restricted mode. Bash gets its own filesystem sandbox.
    tools = ["Read", "Glob", "Grep"]
    if role != "planner":
        tools.append("Bash")
    if role == "worker":
        tools.extend(["Edit", "Write"])
    deny_write = [str(path) for path in protected]
    if role != "worker":
        deny_write.append(str(cwd))
    settings = {"sandbox": {
        "enabled": True, "failIfUnavailable": True, "allowUnsandboxedCommands": False,
        "autoAllowBashIfSandboxed": True, "filesystem": {"denyWrite": deny_write},
    }}
    env = environment()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)
    result = command(
        ["claude", "-p", "--restricted", "--strict-mcp-config", "--no-session-persistence",
         "--permission-prompts", "none", "--tools", ",".join(tools),
         "--allowedTools", ",".join(tools), "--disallowedTools", "mcp__*",
         "--settings", encode(settings), "--output-format", "json",
         "--json-schema", schema_path.read_text(encoding="utf-8")],
        cwd, env=env, stdin=prompt, timeout=1200, fds=fds,
    )
    try:
        envelope = json.loads(result.stdout)
    except (TypeError, ValueError):
        envelope = {}
    valid = isinstance(envelope, dict)
    message = (envelope.get("result") if valid and envelope.get("is_error") else None)
    require(result.returncode == 0 and valid and envelope.get("is_error") is False
            and envelope.get("subtype") == "success",
            f"Claude Code {role} failed: {str(message or result.stderr.strip())[-1600:]}")
    require("structured_output" in envelope, f"Claude Code {role} returned no structured result.")
    return envelope["structured_output"]
