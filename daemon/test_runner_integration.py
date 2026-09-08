"""Offline regressions for native dispatch, billing, Fable 5.1 and observation."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import bridge
import run_agent


class RunnerIntegrationTests(unittest.TestCase):
    def test_native_spawn_never_uses_claude(self):
        with tempfile.TemporaryDirectory() as folder:
            persona = Path(folder) / "persona.md"
            persona.write_text("Survivor {NAME}", encoding="utf-8")
            with patch.object(run_agent, "PERSONA_FILE", str(persona)), \
                 patch.object(run_agent, "AGENT_HOME", folder), \
                 patch.object(run_agent, "spawn_provider") as native, \
                 patch.object(run_agent.subprocess, "Popen") as claude:
                for model in ("codex/default", "gemini-cli/default", "mistral/mistral-small-latest", "moonshot/kimi-k2.6"):
                    result = run_agent.spawn_claude("mcp.json", model, turn_limit=4)
                    self.assertIs(result, native.return_value)
                    self.assertEqual(native.call_args.args[1], model)
                    self.assertIn("Survivor", native.call_args.args[2])
                    self.assertEqual(native.call_args.kwargs["turn_limit"], 4)
                claude.assert_not_called()

    def test_fable_51_uses_adaptive_thinking_even_if_ui_off(self):
        with tempfile.TemporaryDirectory() as folder:
            persona = Path(folder) / "persona.md"
            persona.write_text("Survivor", encoding="utf-8")
            (Path(folder) / "journal").mkdir()
            with patch.object(run_agent, "PERSONA_FILE", str(persona)), \
                 patch.object(run_agent, "AGENT_HOME", folder), \
                 patch.object(run_agent, "AGENT_THINKING", 0), \
                 patch.dict(os.environ, {"MAX_THINKING_TOKENS": "0"}, clear=True), \
                 patch.object(run_agent.subprocess, "Popen") as popen:
                run_agent.spawn_claude("mcp.json", "api/fable-5.1")
                args, kwargs = popen.call_args
                self.assertIn("claude-fable-5-1", args[0])
                self.assertEqual(args[0][args[0].index("--effort") + 1], "low")
                self.assertNotIn("MAX_THINKING_TOKENS", kwargs["env"])
                kwargs["stderr"].close()

    def test_aliases_and_unknown_provider(self):
        self.assertEqual(run_agent.resolve_backend("fable-5.1")[0], "claude-fable-5-1")
        self.assertEqual(run_agent.resolve_backend("api/fable-5-1")[0], "claude-fable-5-1")
        self.assertEqual(run_agent.resolve_backend("codex/gpt-6-astra")[2], "native/codex")
        with self.assertRaises(ValueError):
            run_agent.resolve_backend("typo/some-model")

    def test_missing_api_key_fails_before_touching_game(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "Überlebender"
            home.mkdir()
            stop_flag = home / "stop.flag"
            stop_flag.write_text("keep", encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(run_agent.sys, "argv", ["run_agent.py", "--home", str(home),
                                                       "--model", "mistral/mistral-small-latest"]), \
                 patch.object(run_agent.sys, "stderr", io.StringIO()), \
                 patch.object(run_agent, "Bridge") as game, \
                 patch.object(run_agent, "spawn_provider") as native, \
                 patch.object(run_agent.subprocess, "Popen") as process:
                for _ in range(2):
                    with self.assertRaises(SystemExit) as error:
                        run_agent.main()
                    self.assertEqual(error.exception.code, 2)
                game.assert_not_called()
                native.assert_not_called()
                process.assert_not_called()
            entries = (home / "journal/startup_errors.log").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(entries), 2, "Each failed start must append its own line")
            for entry in entries:
                self.assertRegex(entry, r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}')
                self.assertIn('model="mistral/mistral-small-latest"', entry)
                self.assertIn("MISTRAL_API_KEY is missing", entry)
            self.assertEqual(stop_flag.read_text(encoding="utf-8"), "keep")

    def test_invalid_cli_override_logs_in_effective_default_home(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder) / "agent_homes/birgit"
            nonexistent = str(Path(folder) / "private-launch-path" / "codex.exe")
            with patch.dict(os.environ, {"ISU_CODEX_CLI": nonexistent}, clear=True), \
                 patch.object(run_agent.sys, "argv", ["run_agent.py", "--npc-id", "birgit",
                                                       "--model", "codex/gpt-6-astra"]), \
                 patch.object(run_agent.sys, "stderr", io.StringIO()), \
                 patch.object(run_agent, "agent_home_dir", return_value=str(home)) as resolve_home, \
                 patch.object(run_agent, "Bridge") as game, \
                 patch.object(run_agent.subprocess, "Popen") as process:
                with self.assertRaises(SystemExit) as error:
                    run_agent.main()
                self.assertEqual(error.exception.code, 2)
                resolve_home.assert_called_once_with("birgit")
                game.assert_not_called()
                process.assert_not_called()
            entry = (home / "journal/startup_errors.log").read_text(encoding="utf-8")
            self.assertEqual(len(entry.splitlines()), 1)
            self.assertIn('model="codex/gpt-6-astra"', entry)
            self.assertIn("configured codex CLI executable does not exist", entry)
            self.assertNotIn("private-launch-path", entry)

    def test_startup_log_failure_preserves_original_validation_error(self):
        with tempfile.TemporaryDirectory() as folder:
            # A file where the journal directory should be forces an OSError.
            journal = Path(folder) / "journal"
            journal.write_text("keep", encoding="utf-8")
            stderr = io.StringIO()
            with patch.dict(os.environ, {}, clear=True), \
                 patch.object(run_agent.sys, "argv", ["run_agent.py", "--home", folder,
                                                       "--model", "mistral/mistral-small-latest"]), \
                 patch.object(run_agent.sys, "stderr", stderr), \
                 patch.object(run_agent, "Bridge") as game, \
                 patch.object(run_agent.subprocess, "Popen") as process:
                with self.assertRaises(SystemExit) as error:
                    run_agent.main()
                self.assertEqual(error.exception.code, 2)
                self.assertIn("MISTRAL_API_KEY is missing", stderr.getvalue())
                game.assert_not_called()
                process.assert_not_called()
            self.assertEqual(journal.read_text(encoding="utf-8"), "keep")

    def test_unknown_cost_is_not_reported_as_free(self):
        process = Mock(stdout=io.StringIO(json.dumps({
            "type": "result", "duration_ms": 1500, "num_turns": 2,
            "subtype": "success",
        }) + "\n"))
        journal = Mock()
        reader = run_agent.BrainReader(process, journal, run_agent.TokenTracker())
        reader.run()
        message = journal.log.call_args_list[0].args[0]
        self.assertIn("Kosten nicht gemeldet", message)
        self.assertNotIn("0.0000 USD", message)


class ObservationTests(unittest.TestCase):
    def state(self):
        return {"npc": {"spawned": True, "alive": True, "health": 100},
                "inventory": [{"classname": f"Item{i}", "quantity": 1, "kind": "other"}
                              for i in range(21)],
                "nearby": [{"classname": "GardenPlot", "kind": "garden", "harvestable": True}]}

    def test_full_observation_really_shows_all_inventory(self):
        state = self.state()
        full, _ = bridge.format_observation(state, compact=False)
        compact, _ = bridge.format_observation(state, compact=True)
        self.assertIn("Item20", full)
        self.assertNotIn("Item20", compact)
        self.assertIn("ERNTEREIF", full)

    def test_food_change_invalidates_delta_and_is_visible(self):
        state = self.state()
        food = {"classname": "WolfSteakMeat", "quantity": 100, "kind": "food", "food_stage": 1}
        state["inventory"] = [food]
        old = bridge.inventory_signature(state)
        food["food_stage"] = 2
        self.assertNotEqual(old, bridge.inventory_signature(state))
        text, _ = bridge.format_observation(state)
        self.assertIn("gebraten", text)
        food.update(kind="drink", liquid_safe=False, frozen=True)
        text, _ = bridge.format_observation(state)
        self.assertIn("NICHT SICHER TRINKBAR", text)
        self.assertIn("GEFROREN", text)


if __name__ == "__main__":
    unittest.main()
