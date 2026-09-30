import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from review_bench import backends as B


CASE = {"id": "c", "candidate": "h" * 64, "files": ["a.py"], "expected": [
    {"id": "e", "file": "a.py", "severity": "blocking", "keywords": ["k"], "description": "d"}]}
RESULT = {"candidate": "h" * 64, "covered_files": ["a.py"], "findings": []}


class ParseTests(unittest.TestCase):
    def test_json_text_forms(self):
        self.assertEqual(B.parse_json_text('{"a": 1}'), {"a": 1})
        self.assertEqual(B.parse_json_text('Here you go:\n```json\n{"a": 1}\n```\nDone.'), {"a": 1})
        self.assertEqual(B.parse_json_text('prefix {"a": {"b": 2}} suffix'), {"a": {"b": 2}})
        wrapped = "'" + json.dumps({"patch": "diff --git a/x b/x\n+line\n", "summary": "s"}).replace("\\", "\\\\").replace("'", "\\'") + "'"
        self.assertEqual(B.parse_json_text(wrapped)["summary"], "s")
        twice = json.dumps({"patch": "p", "summary": "first"}) + json.dumps({"patch": "p", "summary": "second"})
        self.assertEqual(B.parse_json_text(twice)["summary"], "first")
        with self.assertRaises(B.BackendError):
            B.parse_json_text("no json here")
        with self.assertRaises(B.BackendError):
            B.parse_json_text("[1, 2]")

    def test_spec_parsing(self):
        self.assertEqual(B.parse_spec("codex"), ("codex", "gpt-5.6-sol", "high"))
        self.assertEqual(B.parse_spec("cursor:grok-4.7-xhigh"), ("cursor", "grok-4.7-xhigh", None))
        self.assertEqual(B.parse_spec("claude:claude-opus-5-5:medium"), ("claude", "claude-opus-5-5", "medium"))
        self.assertEqual(B.label("cursor:grok-4.7-xhigh"), "cursor:grok-4.7-xhigh")
        with self.assertRaises(B.BackendError):
            B.parse_spec("gemini")

    def test_null_and_oracle(self):
        self.assertEqual(B.null_backend(CASE, "", None, None, None, 1)[0]["findings"], [])
        oracle, _, _ = B.oracle_backend(CASE, "", None, None, None, 1)
        self.assertEqual(oracle["findings"][0]["file"], "a.py")
        self.assertIn("k", oracle["findings"][0]["failure_scenario"])


class FakeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.bin = self.home / "bin"
        self.bin.mkdir()
        self.tree = self.home / "tree"
        self.tree.mkdir()

    def fake(self, name, code):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + code)
        path.chmod(0o755)
        return patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ["PATH"]})

    def test_claude_reads_structured_output(self):
        code = r"""import json, sys
args = sys.argv[1:]
assert '--restricted' in args and args[args.index('--tools') + 1] == 'Read,Grep,Glob'
assert args[args.index('--model') + 1] == 'claude-opus-5-5' and args[args.index('--effort') + 1] == 'high'
assert 'candidate' in json.loads(args[args.index('--json-schema') + 1])['properties']
assert 'DIFF:' in sys.stdin.read()
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': RESULT,
                  'usage': {'input_tokens': 10, 'output_tokens': 2}}))
""".replace("RESULT", repr(RESULT))
        with self.fake("claude", code):
            result, usage, _ = B.claude_backend(CASE, "prompt DIFF:", self.tree, "claude-opus-5-5", "high", 30)
        self.assertEqual((result, usage["input_tokens"]), (RESULT, 10))

    def test_claude_error_is_reported(self):
        code = "import json; print(json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'result': 'x'}))"
        with self.fake("claude", code), self.assertRaises(B.BackendError):
            B.claude_backend(CASE, "p", self.tree, "claude-opus-5-5", "high", 30)

    def test_cursor_parses_result_text(self):
        code = r"""import json, sys
args = sys.argv[1:]
assert '--trust' in args and args[args.index('--mode') + 1] == 'ask'
assert args[args.index('--model') + 1] == 'grok-4.7-high'
assert args[-1] != 'prompt DIFF:' and 'DIFF:' in sys.stdin.read()
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'result': '```json\n' + json.dumps(RESULT) + '\n```', 'usage': {'inputTokens': 5, 'outputTokens': 1}}))
""".replace("RESULT", repr(RESULT))
        with self.fake("agent", code):
            result, usage, raw = B.cursor_backend(CASE, "prompt DIFF:", self.tree, "grok-4.7-high", None, 30)
        self.assertEqual((result, usage["inputTokens"]), (RESULT, 5))
        self.assertIn("```", raw)

    def test_muse_reads_terminal_event(self):
        code = r"""import json, sys
args = sys.argv[1:]
assert args[:2] == ['exec', '--json']
for flag in ('--disable-write', '--disable-shell', '--disable-web-tools', '--no-session-log'):
    assert flag in args, flag
assert args[args.index('--approval-mode') + 1] == 'never'
assert args[args.index('--model') + 1] == 'muse-spark-1.3-contributor'
assert args[args.index('--reasoning-effort') + 1] == 'high'
assert 'findings' in json.load(open(args[args.index('--output-schema') + 1]))['properties']
assert 'DIFF:' in open(args[args.index('--prompt-file') + 1]).read()
print(json.dumps({'payload_type': 'run.output.delta', 'payload': {'text': 'thinking'}}))
print(json.dumps({'payload_type': 'run.terminal.completed', 'payload': {'terminal': 'completed', 'text': json.dumps(RESULT)}}))
""".replace("RESULT", repr(RESULT))
        with self.fake("muse", code):
            result, usage, raw = B.muse_backend(CASE, "prompt DIFF:", self.tree, "muse-spark-1.3-contributor", "high", 30)
        self.assertEqual((result, usage), (RESULT, {}))
        self.assertFalse(list(self.tree.parent.glob("*-muse-prompt-*.txt")))

    def test_muse_failed_run_is_reported(self):
        code = "import json; print(json.dumps({'payload_type': 'run.terminal.completed', 'payload': {'terminal': 'failed', 'reason': 'budget'}}))"
        with self.fake("muse", code), self.assertRaisesRegex(B.BackendError, "failed"):
            B.muse_backend(CASE, "p", self.tree, "muse-spark-1.3-contributor", "high", 30)

    def test_codex_reads_output_file(self):
        code = r"""import json, sys
args = sys.argv[1:]
assert args[args.index('--sandbox') + 1] == 'read-only' and args[args.index('--model') + 1] == 'gpt-5.6-sol'
assert 'model_reasoning_effort="high"' in args
schema = json.load(open(args[args.index('--output-schema') + 1]))
assert 'findings' in schema['properties']
open(args[args.index('-o') + 1], 'w').write(json.dumps(RESULT))
print(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 7, 'output_tokens': 3}}))
""".replace("RESULT", repr(RESULT))
        with self.fake("codex", code):
            result, usage, _ = B.codex_backend(CASE, "p", self.tree, "gpt-5.6-sol", "high", 30)
        self.assertEqual((result, usage["turns"][0]["input_tokens"]), (RESULT, 7))


class OpenAICompatibleTests(unittest.TestCase):
    def test_requires_configuration(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(B.BackendError):
            B.openai_compatible_backend(CASE, "p", None, "gpt", None, 5)

    def test_parses_chat_completion(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": json.dumps(RESULT)}}],
                                   "usage": {"prompt_tokens": 9}}).encode()

        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = json.loads(request.data)
            captured["auth"] = request.get_header("Authorization")
            return Response()

        with patch.dict(os.environ, {"OPENAI_BASE_URL": "https://muse.example/v1/", "OPENAI_API_KEY": "k"}), \
                patch("review_bench.backends.urllib.request.urlopen", fake_urlopen):
            result, usage, _ = B.openai_compatible_backend(CASE, "prompt", None, "muse-1", "high", 5)
        self.assertEqual((result, usage["prompt_tokens"]), (RESULT, 9))
        self.assertEqual(captured["url"], "https://muse.example/v1/chat/completions")
        self.assertEqual(captured["auth"], "Bearer k")
        self.assertEqual(captured["body"]["model"], "muse-1")
        self.assertEqual(captured["body"]["reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
