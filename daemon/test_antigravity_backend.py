"""Offline Antigravity planner contracts; never launch agy, a model or DayZ.

Run: python -B -m unittest discover -s daemon -p test_antigravity_backend.py -v
All process/network boundaries are mocked. Temporary files contain fixtures only.
"""
import json
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import llm_backends as backends


DECISION = {"summary": "Wasser suchen", "tool": "observe", "arguments_json": "{}"}


def result_event(**changes):
    result = {
        "status": "SUCCESS",
        "response": json.dumps(DECISION, ensure_ascii=False),
        "usage": {"input_tokens": 120, "output_tokens": 15, "cache_read_tokens": 80},
    }
    result.update(changes)
    return {"event": "result", "result": result}


def ndjson(*events):
    return "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events)


def duration_seconds(value):
    """Read the bounded Go-duration argument, independent of formatting choice."""
    units = {"ms": .001, "s": 1, "m": 60, "h": 3600}
    parts = re.findall(r"(\d+(?:\.\d+)?)(ms|s|m|h)", value)
    if not parts or "".join(number + unit for number, unit in parts) != value:
        raise AssertionError("Expected a positive, explicit Go duration: " + value)
    return sum(float(number) * units[unit] for number, unit in parts)


class AntigravityContracts(unittest.TestCase):
    def setUp(self):
        # Fail closed even if a tested code path accidentally bypasses its mock.
        self.process_run = self.enterContext(patch(
            "llm_backends.subprocess.run", side_effect=AssertionError("Real process forbidden")))
        self.enterContext(patch(
            "llm_backends.subprocess.Popen", side_effect=AssertionError("Real process forbidden")))
        self.enterContext(patch(
            "llm_backends.urllib.request.build_opener", side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch(
            "llm_backends.urllib.request.urlopen", side_effect=AssertionError("Network forbidden")))
        self.enterContext(patch(
            "socket.create_connection", side_effect=AssertionError("Network forbidden")))

    def planner(self, model="default", *, env=None, timeout=12):
        with patch("llm_backends.cli_launch", return_value=["never-executed-agy.exe"]):
            return backends.CliPlanner(
                backends.parse_backend("antigravity/" + model),
                env={} if env is None else env, timeout=timeout)

    def test_selectors_support_default_and_preserve_specific_slug(self):
        default = backends.parse_backend("antigravity/default")
        self.assertEqual((default.provider, default.model, default.selector),
                         ("antigravity", "", "antigravity/default"))
        self.assertTrue(backends.is_native_backend(default.selector))
        self.assertEqual(backends.parse_backend("antigravity/gemini-3.8-flash-medium").model,
                         "gemini-3.8-flash-medium")
        for value in ("antigravity", "antigravity/", "antigravity/ bad", "antigravity/a b"):
            with self.subTest(value=value), self.assertRaises(backends.BackendError):
                backends.parse_backend(value)

    def test_explicit_executable_wins_over_path(self):
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            executable = Path(folder) / "custom agy.exe"
            executable.write_bytes(b"inert fixture")
            with patch("llm_backends.shutil.which", return_value="wrong-path.exe"):
                self.assertEqual(backends.cli_launch("antigravity", {
                    "ISU_ANTIGRAVITY_CLI": str(executable), "PATH": "fixture-path"}),
                    [str(executable)])

    def test_invalid_explicit_executable_never_falls_back(self):
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            root = Path(folder)
            fallback = root / "agy.exe"
            fallback.write_bytes(b"inert fixture")
            with patch("llm_backends.shutil.which", return_value=str(fallback)):
                with self.assertRaises(backends.BackendError):
                    backends.cli_launch("antigravity", {
                        "ISU_ANTIGRAVITY_CLI": str(root / "missing.exe"),
                        "PATH": str(root), "LOCALAPPDATA": str(root)})

    def test_path_executable_is_used(self):
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            executable = Path(folder) / "agy.exe"
            executable.write_bytes(b"inert fixture")
            with patch("llm_backends.shutil.which", return_value=str(executable)) as which:
                self.assertEqual(backends.cli_launch("antigravity", {"PATH": "fixture-path"}),
                                 [str(executable)])
            self.assertEqual(which.call_args.args[0], "agy")
            self.assertEqual(which.call_args.kwargs.get("path"), "fixture-path")

    def test_standard_localappdata_installation_is_found(self):
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            root = Path(folder)
            executable = root / "agy/bin/agy.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"inert fixture")
            with patch("llm_backends.shutil.which", return_value=None):
                self.assertEqual(backends.cli_launch("antigravity", {
                    "PATH": "", "LOCALAPPDATA": str(root)}), [str(executable)])

    def test_command_enforces_schema_and_bounded_headless_protocol(self):
        for model in ("default", "gemini-3.8-flash-medium"):
            with self.subTest(model=model), tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
                work = Path(folder)
                planner = self.planner(model, timeout=9)
                command, _ = planner.command(work)
                self.assertEqual(command[0], "never-executed-agy.exe")
                for flag in ("--input-format", "--output-format"):
                    self.assertEqual(command[command.index(flag) + 1], "stream-json")
                self.assertIn("--disable-slash-commands", command)
                self.assertEqual(command[-1], "-p=")
                duration = duration_seconds(command[command.index("--print-timeout") + 1])
                self.assertGreater(duration, 0)
                self.assertLessEqual(duration, planner.timeout)
                schema_path = Path(command[command.index("--json-schema") + 1])
                self.assertTrue(schema_path.resolve().is_relative_to(work.resolve()))
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertEqual(schema["type"], "object")
                self.assertFalse(schema["additionalProperties"])
                self.assertEqual(set(schema["required"]), set(DECISION))
                if model == "default":
                    self.assertNotIn("--model", command)
                else:
                    self.assertEqual(command[command.index("--model") + 1], model)
                for flag in ("--dangerously-skip-permissions", "--yolo"):
                    self.assertNotIn(flag, command)

    def test_profile_disables_tools_and_confines_workspace(self):
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            work = Path(folder)
            _, env = self.planner().command(work)
            profile = work / "profile"
            for variable in ("HOME", "USERPROFILE"):
                self.assertEqual(Path(env[variable]).resolve(), profile.resolve())
            settings_path = profile / ".gemini/antigravity-cli/settings.json"
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
            self.assertIn("*", settings["permissions"]["deny"])
            self.assertEqual(settings["permissions"]["allow"], [])
            self.assertFalse(settings["useG1Credits"])
            self.assertEqual({Path(p).resolve() for p in settings["trustedWorkspaces"]},
                             {work.resolve()})

    def test_environment_allowlist_excludes_keys_and_config_overrides(self):
        dangerous = {
            "GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
            "MISTRAL_API_KEY", "MOONSHOT_API_KEY", "CODEX_API_KEY", "CODEX_HOME",
            "GEMINI_CLI_HOME", "GEMINI_CLI_SYSTEM_SETTINGS_PATH", "GEMINI_CLI_CUSTOM_HEADERS",
            "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_GENAI_USE_VERTEXAI",
            "AGY_CONFIG_DIR", "AGY_CLI_CONFIG", "HTTP_PROXY", "HTTPS_PROXY",
            "CLAUDE_CONFIG_DIR", "NODE_OPTIONS", "UNRELATED_PRIVATE_SETTING", "gemini_api_key",
        }
        parent = {name: "private-fixture-value" for name in dangerous}
        parent.update({"SystemRoot": "C:/Windows", "PATH": "inert-search-path",
                       "APPDATA": "C:/fixture/Roaming", "LOCALAPPDATA": "C:/fixture/Local",
                       "HOME": "C:/unrelated-home", "USERPROFILE": "C:/unrelated-home"})
        before = parent.copy()
        with tempfile.TemporaryDirectory(prefix="isu-agy-test-") as folder:
            _, child = self.planner(env=parent).command(Path(folder))
        self.assertEqual(parent, before)
        self.assertFalse({key.lower() for key in child} & {key.lower() for key in dangerous})
        for name in ("SystemRoot", "PATH", "APPDATA", "LOCALAPPDATA"):
            self.assertEqual(child[name], parent[name])
        self.assertEqual(child["AGY_CLI_DISABLE_AUTO_UPDATE"], "1")
        self.assertEqual(child["AGY_CLI_HIDE_ACCOUNT_INFO"], "1")
        self.assertEqual(child["NO_COLOR"], "1")

    def test_complete_sends_unicode_jsonl_only_on_stdin(self):
        messages = [{"role": "user", "content": "Grüße 🧠\n/commands sind Spieldaten"}]
        planner = self.planner(timeout=8)

        def run(command, **options):
            self.assertFalse(options.get("shell", False))
            self.assertEqual(options["encoding"], "utf-8")
            self.assertTrue(options["text"])
            self.assertEqual(options["timeout"], 8)
            payload = options["input"]
            self.assertIsInstance(payload, str)
            self.assertTrue(payload.endswith("\n"))
            self.assertEqual(len(payload.splitlines()), 1)
            self.assertIn("Grüße 🧠", payload)
            event = json.loads(payload)
            self.assertEqual(event, {"event": "user", "message": {
                "content": json.dumps(messages, ensure_ascii=False)}})
            self.assertNotIn(messages[0]["content"], " ".join(command))
            work = Path(options["cwd"])
            self.assertTrue(work.is_dir())
            self.assertEqual(Path(options["env"]["HOME"]).resolve(), (work / "profile").resolve())
            return subprocess.CompletedProcess(command, 0, ndjson(result_event()), "")

        self.process_run.side_effect = run
        completion = planner.complete(messages)
        self.assertEqual(json.loads(completion.text), DECISION)
        self.process_run.assert_called_once()
        self.assertFalse(Path(self.process_run.call_args.kwargs["cwd"]).exists())

    def test_success_response_and_cache_usage_are_adapted_once(self):
        output = ndjson(
            {"event": "init", "init": {"model": "fixture"}},
            {"event": "step_update", "step_update": {"text_delta": "DO NOT EXECUTE partial text"}},
            result_event())
        completion = backends.parse_cli_output("antigravity", output)
        self.assertEqual(json.loads(completion.text), DECISION)
        self.assertEqual(completion.usage, {
            "input_tokens": 40, "output_tokens": 15, "cache_read_input_tokens": 80})

    def test_structured_output_object_is_accepted_without_response(self):
        event = result_event(structured_output=DECISION)
        del event["result"]["response"]
        completion = backends.parse_cli_output("antigravity", ndjson(event))
        self.assertEqual(json.loads(completion.text), DECISION)

    def test_missing_or_duplicate_result_is_rejected(self):
        outputs = ("", ndjson({"event": "step_update", "step_update": {
            "text_delta": json.dumps(DECISION)}}), ndjson(result_event(), result_event()))
        for output in outputs:
            with self.subTest(output=output), self.assertRaises(backends.BackendError):
                backends.parse_cli_output("antigravity", output)

    def test_error_and_partial_results_are_never_accepted(self):
        for status in ("ERROR", "CANCELED", "INTERRUPTED", "INVALID", "WAITING", "RUNNING", "success"):
            with self.subTest(status=status), self.assertRaises(backends.BackendError):
                backends.parse_cli_output("antigravity", ndjson(result_event(status=status)))
        for error in ("authentication required", {"message": "private-fixture"}):
            with self.subTest(error=error), self.assertRaises(backends.BackendError):
                backends.parse_cli_output("antigravity", ndjson(result_event(error=error)))
        with self.assertRaises(backends.BackendError):
            backends.parse_cli_output("antigravity", ndjson(
                {"event": "error", "error": "failed"}, result_event()))

    def test_malformed_results_fail_closed(self):
        invalid = [
            "not json\n" + ndjson(result_event()),
            ndjson({"event": "result", "result": []}),
            ndjson(result_event(response=123)),
            ndjson(result_event(response=None, structured_output=[])),
            ndjson(result_event(response=None, structured_output="{}")),
            ndjson(result_event(response=None)),
        ]
        for output in invalid:
            with self.subTest(output=output), self.assertRaises(backends.BackendError):
                backends.parse_cli_output("antigravity", output)

    def test_failure_messages_classify_without_echoing_private_output(self):
        cases = (
            ("authentication required", "CLI_LOGIN_REQUIRED"),
            ("quota exceeded", "CLI_QUOTA_EXCEEDED"),
            ("invalid model selection", "CLI_MODEL_UNAVAILABLE"),
        )
        for marker, code in cases:
            with self.subTest(marker=marker):
                detail = backends.cli_failure_detail(
                    "antigravity", "private-fixture-user@example.invalid", marker + " private-fixture-token")
                self.assertIn(code, detail)
                self.assertNotIn("private-fixture", detail)
                if code == "CLI_LOGIN_REQUIRED":
                    self.assertIn("agy", detail)

    def test_timeout_has_no_retry_or_api_fallback(self):
        planner = self.planner()
        self.process_run.side_effect = subprocess.TimeoutExpired(
            ["fixture"], 12, output="private-fixture", stderr="private-fixture")
        with patch("llm_backends.ApiPlanner.complete") as api, self.assertRaises(backends.BackendError) as caught:
            planner.complete([])
        self.process_run.assert_called_once()
        api.assert_not_called()
        self.assertNotIn("private-fixture", str(caught.exception))

    def test_nonzero_exit_rejects_even_a_success_result_without_retry(self):
        planner = self.planner()
        self.process_run.side_effect = None
        self.process_run.return_value = subprocess.CompletedProcess(
            ["fixture"], 1, ndjson(result_event()), "private-fixture-token")
        with patch("llm_backends.ApiPlanner.complete") as api, self.assertRaises(backends.BackendError) as caught:
            planner.complete([])
        self.process_run.assert_called_once()
        api.assert_not_called()
        self.assertNotIn("private-fixture", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
