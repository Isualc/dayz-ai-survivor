"""Offline provider contract tests: no paid calls, CLI login or DayZ actions.

Run: python -m unittest discover -s daemon -p test_llm_backends.py -v
"""
import asyncio
import io
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock
import urllib.error

from llm_backends import (ApiPlanner, BackendError, CliPlanner, Completion,
                          api_config, cli_launch, is_native_backend,
                          normalized_usage, parse_backend, parse_cli_output,
                          parse_decision, spawn_provider, validate_backend)
from provider_worker import (DayzTools, DecisionWorker, MemoryFiles,
                             has_queued_input, read_messages)


def decision(tool="", **arguments):
    return json.dumps({"summary": "Next action", "tool": tool, "arguments_json": json.dumps(arguments)})


class BackendContracts(unittest.TestCase):
    def test_selector_parsing_preserves_model(self):
        self.assertEqual(parse_backend("codex/default").model, "")
        self.assertEqual(parse_backend("gemini-cli/gemini-exp-123").model, "gemini-exp-123")
        self.assertEqual(parse_backend("moonshot/kimi-k3").selector, "moonshot/kimi-k3")
        self.assertFalse(is_native_backend("local/qwen"))
        for selector in ("codex", "codex/", "mistral/default", "unknown/m", "moonshot/a b"):
            with self.subTest(selector=selector), self.assertRaises(BackendError):
                parse_backend(selector)

    def test_api_config_fails_without_key_and_requires_https(self):
        backend = parse_backend("mistral/mistral-small-latest")
        for env in ({}, {"MISTRAL_API_KEY": "x", "MISTRAL_BASE_URL": "http://localhost/v1"},
                    {"MISTRAL_API_KEY": "x", "MISTRAL_BASE_URL": "https://user:secret@host/v1"}):
            with self.assertRaises(BackendError):
                api_config(backend, env)
        key, url = api_config(backend, {"MISTRAL_API_KEY": "x"})
        self.assertEqual((key, url), ("x", "https://api.mistral.ai/v1"))

    def test_native_validation_has_no_network_side_effect(self):
        with patch("llm_backends.urllib.request.build_opener") as network:
            backend = validate_backend("moonshot/kimi-k3", {"MOONSHOT_API_KEY": "test"})
        self.assertEqual(backend.model, "kimi-k3")
        network.assert_not_called()

    def test_npm_launcher_uses_node_without_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shim = root / "gemini.cmd"
            shim.write_text("unused")
            script = root / "node_modules/@google/gemini-cli/bundle/gemini.js"
            script.parent.mkdir(parents=True)
            script.write_text("unused")
            with patch("llm_backends.shutil.which", return_value="node.exe"):
                self.assertEqual(cli_launch("gemini-cli", {"ISU_GEMINI_CLI": str(shim)}),
                                 ["node.exe", str(script)])

    def test_cli_commands_disable_host_tools_and_api_key_billing(self):
        for provider in ("codex", "gemini-cli"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as folder:
                env = {"OPENAI_API_KEY": "private", "GEMINI_API_KEY": "private", "MOONSHOT_API_KEY": "private"}
                with patch("llm_backends.cli_launch", return_value=[provider]):
                    planner = CliPlanner(parse_backend(provider + "/default"), env=env)
                command, child_env = planner.command(Path(folder))
                self.assertNotIn("OPENAI_API_KEY", child_env)
                self.assertNotIn("GEMINI_API_KEY", child_env)
                self.assertNotIn("MOONSHOT_API_KEY", child_env)
                self.assertNotIn("--yolo", command)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
                if provider == "codex":
                    self.assertIn('forced_login_method="chatgpt"', command)
                    self.assertIn("--ignore-user-config", command)
                    self.assertIn("shell_tool", command)
                    self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
                else:
                    policy = Path(command[command.index("--admin-policy") + 1]).read_text()
                    self.assertIn('decision = "deny"', policy)
                    settings = json.loads(Path(child_env["GEMINI_CLI_SYSTEM_SETTINGS_PATH"]).read_text())
                    self.assertFalse(settings["hooksConfig"]["enabled"])
                    self.assertEqual(settings["security"]["auth"]["selectedType"], "oauth-personal")

    def test_codex_and_gemini_json_are_adapted(self):
        output = "\n".join(json.dumps(x) for x in [
            {"type": "thread.started", "thread_id": "x"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": decision("observe")}},
            {"type": "turn.completed", "usage": {"input_tokens": 200, "cached_input_tokens": 150, "output_tokens": 30}}])
        completion = parse_cli_output("codex", output)
        self.assertEqual(completion.usage["input_tokens"], 50)
        self.assertEqual(parse_decision(completion.text, {"observe"})[1], "observe")
        gemini = {"response": decision(), "stats": {"models": {"gemini": {
            "tokens": {"prompt": 120, "candidates": 20, "cached": 100}}}}}
        self.assertEqual(parse_cli_output("gemini-cli", json.dumps(gemini)).usage["output_tokens"], 20)

    def test_cli_errors_and_injected_tools_are_rejected(self):
        for provider, output in (("codex", '{"type":"turn.failed"}'),
                                 ("gemini-cli", '{"error":{"message":"failed"}}')):
            with self.assertRaises(BackendError):
                parse_cli_output(provider, output)
        for invalid in (decision("Bash", command="whoami"), '{"tool":"observe"}',
                        '[{"tool":"observe"}]', decision("observe") + "extra",
                        '{"summary":"x","tool":"observe","arguments_json":"[]"}'):
            with self.subTest(invalid=invalid), self.assertRaises(BackendError):
                parse_decision(invalid, {"observe"})

    def test_model_specific_kimi_parameters_and_budget(self):
        for model in ("kimi-k3", "kimi-k2.6", "kimi-k2.7-code", "kimi-k2.7-code-highspeed"):
            planner = ApiPlanner(parse_backend("moonshot/" + model), env={"MOONSHOT_API_KEY": "test"}, max_tokens=900)
            payload = planner.payload([])
            self.assertEqual(payload["max_tokens"], 900)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertNotIn("temperature", payload)
            if model == "kimi-k3":
                self.assertEqual(payload["reasoning_effort"], "low")
                self.assertNotIn("thinking", payload)
            elif model == "kimi-k2.6":
                self.assertEqual(payload["thinking"], {"type": "disabled"})
            else:
                self.assertNotIn("thinking", payload)
                self.assertNotIn("reasoning_effort", payload)

    def test_api_preserves_reasoning_and_never_retries(self):
        planner = ApiPlanner(parse_backend("moonshot/kimi-k3"), env={"MOONSHOT_API_KEY": "private"})
        result = {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": decision("observe"), "reasoning_content": "provider reasoning"}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 30}}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(result).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch("llm_backends.urllib.request.build_opener", return_value=opener):
            completion = planner.complete([{"role": "user", "content": "test"}])
        self.assertEqual(completion.assistant["reasoning_content"], "provider reasoning")
        self.assertEqual(opener.open.call_count, 1)
        opener.open.side_effect = urllib.error.HTTPError("https://api", 429, "private", {}, None)
        with patch("llm_backends.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(BackendError) as failure:
                planner.complete([])
        self.assertNotIn("private", str(failure.exception))
        self.assertEqual(opener.open.call_count, 2)

    def test_truncated_api_answer_is_not_executed(self):
        planner = ApiPlanner(parse_backend("mistral/mistral-small-latest"), env={"MISTRAL_API_KEY": "test"})
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({"choices": [{"finish_reason": "length", "message": {
            "content": decision("observe")}}]}).encode()
        with patch("llm_backends.urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(BackendError):
                planner.complete([])

    def test_cache_is_not_counted_twice(self):
        usage = normalized_usage({"prompt_tokens": 100, "completion_tokens": 12,
                                  "prompt_tokens_details": {"cached_tokens": 80}})
        self.assertEqual(usage, {"input_tokens": 20, "output_tokens": 12, "cache_read_input_tokens": 80})

    def test_spawn_contract_passes_persona_outside_command_line(self):
        with tempfile.TemporaryDirectory() as folder, patch("llm_backends.validate_backend"), \
                patch("llm_backends.subprocess.Popen") as popen:
            spawn_provider(str(Path(folder) / "mcp.json"), "codex/default", "private persona", folder, 4, env={})
            command = popen.call_args.args[0]
            options = popen.call_args.kwargs
            self.assertNotIn("private persona", command)
            self.assertEqual(Path(command[command.index("--persona-file") + 1]).read_text(), "private persona")
            self.assertEqual(options["stdin"], subprocess.PIPE)
            self.assertEqual(options["stdout"], subprocess.PIPE)
            self.assertEqual(options["encoding"], "utf-8")

    @unittest.skipUnless(os.name == "nt", "Windows cleanup job")
    def test_killing_worker_terminates_its_child_process(self):
        # Only two inert Python processes are started; neither contacts a CLI,
        # an API or DayZ. This exercises the abrupt proc.kill path used by runner.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        script = ("from provider_worker import install_child_job; import subprocess,sys,time; "
                  "job = install_child_job(); "
                  "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
                  "print(child.pid, flush=True); time.sleep(30)")
        parent = subprocess.Popen([sys.executable, "-c", script],
                                  cwd=str(Path(__file__).parent), stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        handle = None
        try:
            output = queue.Queue()
            threading.Thread(target=lambda: output.put(parent.stdout.readline()), daemon=True).start()
            out = output.get(timeout=5)
            self.assertTrue(out and out.strip().isdigit(), "Worker job setup did not print a child PID.")
            handle = kernel.OpenProcess(0x00100000, False, int(out.strip()))
            self.assertTrue(handle)
            parent.kill()
            parent.communicate(timeout=5)
            self.assertEqual(kernel.WaitForSingleObject(handle, 5000), 0)
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.communicate(timeout=5)
            if handle:
                kernel.CloseHandle(handle)


class MemoryContracts(unittest.TestCase):
    def test_memory_is_confined_to_survivor_markdown_notes(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = MemoryFiles(folder)
            for path in ("../escape.md", "journal/secret.log", "memory/a/secret.md", "memory/x.py", "memory/x.md:stream"):
                with self.subTest(path=path), self.assertRaises(BackendError):
                    memory.path(path)
            text, failed = memory.call("Write", {"file_path": "memory/water.md", "content": "Boil unsafe water."})
            self.assertFalse(failed, text)
            self.assertEqual(memory.call("Read", {"file_path": "memory/water.md"}), ("Boil unsafe water.", False))
            self.assertIn("Boil unsafe water.", memory.briefing())

    def test_oversized_memory_note_and_extra_write_keys_fail(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = MemoryFiles(folder)
            self.assertTrue(memory.call("Write", {"file_path": "CLAUDE.md", "content": "x" * 12001})[1])
            self.assertTrue(memory.call("Write", {"file_path": "CLAUDE.md", "content": "x", "append": True})[1])
            self.assertFalse((Path(folder) / "CLAUDE.md").exists())

    def test_stdin_protocol_ignores_noise_and_eof_is_not_an_interrupt(self):
        inbox = queue.Queue()
        stream = io.StringIO('noise\n{"type":"user","message":{"content":[{"type":"text","text":"danger"}]}}\n')
        read_messages(stream, inbox)
        self.assertTrue(has_queued_input(inbox))
        self.assertEqual(inbox.get(), "danger")
        self.assertFalse(has_queued_input(inbox))
        self.assertIsNone(inbox.get())


class WorkerProcessContract(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node is required for the inert fake CLI")
    def test_worker_process_accepts_runner_stdin_and_returns_tool_results(self):
        # The entire persistent worker runs with a fake Node CLI and fake MCP.
        # No real CLI binary, authentication, network or game bridge is used.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = root / "fake_mcp.py"
            server.write_text('from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("fake")\n'
                              '@mcp.tool()\ndef echo(value: str) -> str:\n    return "mock:" + value\n'
                              'mcp.run()\n', encoding="utf-8")
            cli = root / "fake_cli.js"
            cli.write_text('const fs = require("fs");\n'
                           'const messages = JSON.parse(fs.readFileSync(0, "utf8"));\n'
                           'const done = messages.some(m => m.role === "user" && m.content.includes("mock:offline"));\n'
                           'const text = JSON.stringify({summary:"offline", tool:done ? "" : "echo", arguments_json:done ? "{}" : JSON.stringify({value:"offline"})});\n'
                           'console.log(JSON.stringify({type:"item.completed",item:{type:"agent_message",text}}));\n'
                           'console.log(JSON.stringify({type:"turn.completed",usage:{input_tokens:10,output_tokens:2}}));\n', encoding="utf-8")
            config = root / "mcp.json"
            config.write_text(json.dumps({"mcpServers": {"dayz": {
                "command": sys.executable, "args": [str(server)]}}}), encoding="utf-8")
            persona = root / "persona.txt"
            persona.write_text("Offline fake survivor.", encoding="utf-8")
            env = {**os.environ, "ISU_CODEX_CLI": str(cli), "ISU_LLM_ALLOWED_TOOLS": "echo", "PYTHONIOENCODING": "utf-8"}
            command = [sys.executable, str(Path(__file__).with_name("provider_worker.py")),
                       "--model", "codex/default", "--mcp-config", str(config),
                       "--persona-file", str(persona), "--agent-home", folder]
            request = json.dumps({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "offline"}]}}) + "\n"
            result = subprocess.run(command, input=request, capture_output=True, text=True,
                                    encoding="utf-8", env=env, timeout=25,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(result.returncode, 0, result.stderr)
            events = [json.loads(line) for line in result.stdout.splitlines()]
            final = [e for e in events if e["type"] == "result"]
            self.assertEqual(len(final), 1)
            self.assertEqual(final[0]["subtype"], "success")
            self.assertEqual(final[0]["num_turns"], 2)
            replies = [e for e in events if e["type"] == "user"]
            self.assertEqual(replies[0]["message"]["content"][0]["content"], "mock:offline")


class FakePlanner:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, Completion) else Completion(answer, {"input_tokens": 20, "output_tokens": 4})


class FakeTools:
    catalog = [{"name": "observe", "inputSchema": {"type": "object"}},
               {"name": "eat", "inputSchema": {"type": "object"}}]

    def __init__(self):
        self.calls = []

    async def call(self, name, arguments):
        self.calls.append((name, arguments))
        return ("Inventory: Apple. Hungry." if name == "observe" else "Gegessen: Apple"), False


class WorkerContracts(unittest.IsolatedAsyncioTestCase):
    def worker(self, planner, **kwargs):
        tools, events = FakeTools(), []
        worker = DecisionWorker("moonshot/kimi-k3", planner, tools, "Survive.", emit_fn=events.append, **kwargs)
        return worker, tools, events

    async def test_observe_then_eat_uses_real_result_and_emits_runner_protocol(self):
        planner = FakePlanner(decision("observe"), decision("eat", classname="Apple"), decision())
        worker, tools, events = self.worker(planner)
        result = await worker.turn("I am hungry.")
        self.assertEqual(tools.calls, [("observe", {}), ("eat", {"classname": "Apple"})])
        self.assertIn("Inventory: Apple", json.dumps(planner.calls[1]))
        self.assertEqual(result["subtype"], "success")
        self.assertEqual(result["num_turns"], 3)
        self.assertEqual(result["usage"]["input_tokens"], 60)
        self.assertNotIn("total_cost_usd", result)
        self.assertFalse(result["cost_known"])
        uses = [block for e in events if e["type"] == "assistant" for block in e["message"]["content"] if block["type"] == "tool_use"]
        replies = [e["message"]["content"][0] for e in events if e["type"] == "user"]
        self.assertEqual(uses[0]["name"], "mcp__dayz__observe")
        self.assertEqual(uses[0]["id"], replies[0]["tool_use_id"])

    async def test_invalid_plan_never_reaches_game_and_cools_down(self):
        planner = FakePlanner(decision("Bash", command="bad"))
        worker, tools, events = self.worker(planner)
        result = await worker.turn("test")
        again = await worker.turn("test again")
        self.assertEqual(result["subtype"], "error_backend")
        self.assertTrue(again["is_error"])
        self.assertEqual(len(planner.calls), 1)
        self.assertEqual(tools.calls, [])

    async def test_pending_danger_stops_stale_plan_before_tool(self):
        worker, tools, events = self.worker(FakePlanner(decision("eat", classname="Apple")), has_pending=lambda: True)
        result = await worker.turn("old situation")
        self.assertEqual(tools.calls, [])
        self.assertEqual(result["subtype"], "success")
        self.assertIn("NOT executed", json.dumps(worker.turns))

    async def test_action_budget_caps_tool_loop(self):
        planner = FakePlanner(decision("observe"), decision("eat", classname="Apple"), decision())
        worker, tools, events = self.worker(planner, max_steps=2)
        result = await worker.turn("test")
        self.assertEqual(len(tools.calls), 2)
        self.assertEqual(len(planner.calls), 2)
        self.assertEqual(result["subtype"], "error_max_turns")

    async def test_repeated_identical_actions_are_stopped(self):
        # Festgefahren (dreimal observe ohne neues Ergebnis) endet den Zug
        # regulär mit Hinweis - kein Backend-Fehler, keine Provider-Pause.
        worker, tools, events = self.worker(FakePlanner(*[decision("observe")] * 4))
        result = await worker.turn("test")
        self.assertEqual(len(tools.calls), 2)
        self.assertEqual(result["subtype"], "success")
        self.assertFalse(result["is_error"])
        self.assertIn("Turn ended by the worker", json.dumps(worker.turns))
        self.assertTrue(any("[HINWEIS]" in json.dumps(e) for e in events))

    async def test_reasoning_is_preserved_for_api_but_not_shown_in_stream(self):
        first = Completion(decision("observe"), {}, {"role": "assistant", "content": decision("observe"), "reasoning_content": "internal-marker"})
        planner = FakePlanner(first, decision())
        worker, tools, events = self.worker(planner)
        await worker.turn("test")
        self.assertIn("internal-marker", json.dumps(planner.calls[1]))
        self.assertNotIn("internal-marker", json.dumps(events))

    async def test_history_evicts_complete_turns(self):
        worker, tools, events = self.worker(FakePlanner(decision()), context_chars=6000)
        worker.turns = [[{"role": "user", "content": "old-marker" + "x" * 5900},
                         {"role": "assistant", "content": "old reply", "reasoning_content": "old reasoning"}]]
        messages = worker.messages([{"role": "user", "content": "new" * 100}])
        self.assertNotIn("old-marker", json.dumps(messages))
        self.assertNotIn("old reasoning", json.dumps(messages))

    async def test_real_mcp_sdk_uses_only_configured_fake_dayz_server(self):
        # A fresh mock MCP subprocess exercises stdio/schema/result compatibility.
        # It contains no imports or commands from the actual DayZ bridge.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            server = root / "fake_server.py"
            server.write_text('from mcp.server.fastmcp import FastMCP\nmcp = FastMCP("fake")\n'
                              '@mcp.tool()\ndef echo(value: str) -> str:\n    return "mock:" + value\n'
                              'mcp.run()\n', encoding="utf-8")
            config = root / "mcp.json"
            config.write_text(json.dumps({"mcpServers": {
                "dayz": {"command": sys.executable, "args": [str(server)]},
                "ignored": {"command": "MUST_NOT_BE_STARTED"}}}), encoding="utf-8")
            with patch.dict(os.environ, {"ISU_LLM_ALLOWED_TOOLS": "echo"}):
                async with DayzTools(str(config), folder, timeout=15) as tools:
                    self.assertEqual([t["name"] for t in tools.catalog], ["echo"])
                    self.assertEqual(await tools.call("echo", {"value": "offline"}), ("mock:offline", False))
                    with self.assertRaises(BackendError):
                        await tools.call("Read", {"file_path": "CLAUDE.md"})


if __name__ == "__main__":
    unittest.main()
