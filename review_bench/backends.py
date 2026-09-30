"""Reviewer backends. Each returns (parsed result or None, usage dict, raw text).

Every backend reviews in a fresh context: no session, no history, no memory.
Tool-using backends (codex, claude, cursor, muse) get a read-only working
directory holding the candidate source. The API backend (any OpenAI-compatible
chat endpoint) only sees the prompt. Default models are examples; pass
name:model:effort explicitly for anything that matters.
"""
from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import re
import subprocess
import urllib.request

from .prompt import REVIEW_SCHEMA, canonical


class BackendError(Exception):
    """A call that produced no usable result; recorded, never retried silently."""


def parse_json_text(text):
    """Extract one JSON object from model text that may carry fences or prose."""
    if not isinstance(text, str):
        raise BackendError("no text to parse")
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", stripped, re.S)
    candidates = [fence.group(1)] if fence else []
    candidates.append(stripped)
    start, end = stripped.find("{"), stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            # Some runtimes emit the object several times back to back; take the first complete one.
            try:
                value, _ = json.JSONDecoder().raw_decode(candidate.lstrip())
            except ValueError:
                continue
        if isinstance(value, dict):
            return value
    # Some runtimes hand back the object as a quoted, escaped string literal one level too deep.
    try:
        literal = ast.literal_eval(stripped)
        if isinstance(literal, str):
            value = json.loads(literal)
            if isinstance(value, dict):
                return value
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        pass
    raise BackendError("no JSON object in model output")


def parse_spec(spec):
    """'name[:model[:effort]]' -> (name, model, effort)."""
    parts = spec.split(":")
    name = parts[0]
    if name not in BACKENDS:
        raise BackendError(f"unknown backend {name!r}; choose from {sorted(BACKENDS)}")
    model = parts[1] if len(parts) > 1 and parts[1] else DEFAULT_MODELS.get(name)
    effort = parts[2] if len(parts) > 2 and parts[2] else DEFAULT_EFFORT.get(name)
    return name, model, effort


def label(spec):
    name, model, effort = parse_spec(spec)
    return ":".join(p for p in (name, model, effort) if p)


def run_cli(argv, prompt, cwd, timeout, env=None):
    result = subprocess.run(argv, input=prompt.encode(), cwd=str(cwd), capture_output=True,
                            timeout=timeout, env=env, start_new_session=True)
    return result.returncode, result.stdout.decode(errors="replace"), result.stderr.decode(errors="replace")


def events(stdout):
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            yield event


# --- no-model backends -------------------------------------------------------

def null_backend(case, prompt, tree, model, effort, timeout, schema=None):
    """Reviewer that never finds anything: the floor every real reviewer must beat."""
    return {"candidate": case["candidate"], "covered_files": case["files"], "findings": []}, {}, ""


def oracle_backend(case, prompt, tree, model, effort, timeout, schema=None):
    """Reviewer that reports exactly the expected findings: the ceiling, for testing scoring."""
    findings = [{"file": e["file"], "severity": e["severity"], "summary": e["description"],
                 "failure_scenario": " ".join(e["keywords"])} for e in case["expected"]]
    return {"candidate": case["candidate"], "covered_files": case["files"], "findings": findings}, {}, ""


# --- CLI backends ------------------------------------------------------------

def codex_backend(case, prompt, tree, model, effort, timeout, schema=REVIEW_SCHEMA):
    schema_path = Path(tree).parent / f"{case['id']}-codex-schema-{os.getpid()}.json"
    output = Path(tree).parent / f"{case['id']}-codex-{os.getpid()}.json"
    schema_path.write_text(canonical(schema))
    argv = ["codex", "exec", "--json", "--sandbox", "read-only", "--cd", str(tree), "--model", model,
            "-c", f'model_reasoning_effort="{effort}"', "-c", 'approval_policy="never"',
            "--disable", "multi_agent", "--output-schema", str(schema_path), "-o", str(output), "-"]
    code, stdout, stderr = run_cli(argv, prompt, tree, timeout)
    usage = [e.get("usage") for e in events(stdout) if e.get("type") == "turn.completed"]
    if code or not output.is_file():
        raise BackendError(f"codex exit {code}: {stderr[-500:]}")
    text = output.read_text()
    output.unlink()
    return parse_json_text(text), {"turns": usage}, text


CLAUDE_SETTINGS = {"permissions": {"deny": ["Read(**/.env*)"]}}
CLAUDE_READING = ("Budget your context. Locate symbols with Grep, then Read only the line ranges you need. "
                  "Do not read whole large files or survey unrelated code.")


def claude_backend(case, prompt, tree, model, effort, timeout, schema=REVIEW_SCHEMA):
    argv = ["claude", "-p", "--restricted", "--strict-mcp-config", "--tools", "Read,Grep,Glob",
            "--permission-mode", "dontAsk", "--no-session-persistence", "--disable-slash-commands",
            "--settings", canonical(CLAUDE_SETTINGS), "--append-system-prompt", CLAUDE_READING,
            "--output-format", "json", "--json-schema", canonical(schema), "--model", model,
            "--effort", effort]
    code, stdout, stderr = run_cli(argv, prompt, tree, timeout)
    result = None
    for event in reversed(list(events(stdout))):
        if event.get("type") == "result":
            result = event
            break
    if result is None:
        raise BackendError(f"claude exit {code}, no result event: {stderr[-500:]}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise BackendError(f"claude {result.get('subtype')}: {str(result.get('result'))[:300]}")
    output = result.get("structured_output")
    if not isinstance(output, dict):
        output = parse_json_text(result.get("result"))
    return output, result.get("usage") or {}, canonical(output)


def cursor_backend(case, prompt, tree, model, effort, timeout, schema=None):
    # Ask mode is read-only. --trust skips the workspace prompt for the throwaway snapshot.
    # The prompt goes on stdin: a large diff would exceed the single-argument size limit.
    argv = ["agent", "-p", "--trust", "--output-format", "json", "--mode", "ask",
            "--model", model, "--workspace", str(tree)]
    code, stdout, stderr = run_cli(argv, prompt, tree, timeout)
    result = None
    for event in reversed(list(events(stdout))):
        if event.get("type") == "result":
            result = event
            break
    if result is None:
        raise BackendError(f"cursor exit {code}, no result event: {(stderr or stdout)[-500:]}")
    if result.get("is_error") or result.get("subtype") != "success":
        raise BackendError(f"cursor {result.get('subtype')}: {str(result.get('result'))[:300]}")
    return parse_json_text(result.get("result")), result.get("usage") or {}, str(result.get("result"))


def muse_backend(case, prompt, tree, model, effort, timeout, schema=REVIEW_SCHEMA):
    """Muse Code CLI, headless: read-only workspace tools, no shell, no web, no session log."""
    schema_path = Path(tree).parent / f"{case['id']}-muse-schema-{os.getpid()}.json"
    prompt_path = Path(tree).parent / f"{case['id']}-muse-prompt-{os.getpid()}.txt"
    schema_path.write_text(canonical(schema))
    prompt_path.write_text(prompt)
    argv = ["muse", "exec", "--json", "--output-schema", str(schema_path), "--prompt-file", str(prompt_path),
            "--approval-mode", "never", "--disable-write", "--disable-shell", "--disable-web-tools",
            "--no-session-log", "--workspace", str(tree), "--model", model]
    if effort:
        argv += ["--reasoning-effort", effort]
    try:
        code, stdout, stderr = run_cli(argv, "", tree, timeout)
    finally:
        prompt_path.unlink(missing_ok=True)
    terminal = None
    for event in events(stdout):
        if event.get("payload_type") == "run.terminal.completed":
            terminal = event.get("payload") or {}
    if terminal is None:
        raise BackendError(f"muse exit {code}, no terminal event: {stderr[-500:]}")
    if terminal.get("terminal") != "completed":
        raise BackendError(f"muse run ended {terminal.get('terminal')}: {str(terminal.get('reason'))[:300]}")
    text = terminal.get("text")
    return parse_json_text(text), {}, str(text)


# --- API backend ---------------------------------------------------------------

def openai_compatible_backend(case, prompt, tree, model, effort, timeout, schema=REVIEW_SCHEMA, *, prefix="OPENAI"):
    """Diff-only chat-completions call to any OpenAI-compatible endpoint; reads <PREFIX>_BASE_URL/_API_KEY."""
    base_url = os.environ.get(f"{prefix}_BASE_URL")
    api_key = os.environ.get(f"{prefix}_API_KEY")
    if not base_url or not api_key:
        raise BackendError(f"set {prefix}_BASE_URL and {prefix}_API_KEY in the environment")
    body = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": 0}
    if effort:
        body["reasoning_effort"] = effort
    body["response_format"] = {"type": "json_schema", "json_schema": {"name": "result", "schema": schema, "strict": True}}
    request = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": "Bearer " + api_key})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except OSError as exc:
        raise BackendError(f"{prefix} request failed: {exc}")
    try:
        text = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        raise BackendError(f"{prefix} response had no message content")
    return parse_json_text(text), payload.get("usage") or {}, text


BACKENDS = {"null": null_backend, "oracle": oracle_backend, "codex": codex_backend,
            "claude": claude_backend, "cursor": cursor_backend, "muse": muse_backend,
            "openai": openai_compatible_backend}
HAS_TOOLS = {"codex", "claude", "cursor", "muse"}
DEFAULT_MODELS = {"codex": "gpt-5.6-sol", "claude": "claude-opus-5-5", "cursor": "grok-4.7-high",
                  "muse": "muse-spark-1.3-contributor", "openai": os.environ.get("OPENAI_MODEL", "gpt-5.6-sol")}
DEFAULT_EFFORT = {"codex": "high", "claude": "high", "muse": "high"}
