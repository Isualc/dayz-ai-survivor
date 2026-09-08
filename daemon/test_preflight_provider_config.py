"""Offline checks for provider selection, preflight isolation and menu dispatch.

Run: python -m unittest discover -s daemon -p test_preflight_provider_config.py
No credentials, CLI sessions, cloud requests or game state are read or changed.
"""
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import arena_supervisor
import llm_backends
import preflight


class ProviderPreflightTests(unittest.TestCase):
    def setUp(self):
        self.environ = patch.dict(preflight.os.environ, {}, clear=True)
        self.environ.start()
        self.addCleanup(self.environ.stop)
        self.network = patch.object(preflight, "_http", side_effect=AssertionError("network used"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.cli_process = patch.object(preflight.subprocess, "run",
                                       side_effect=AssertionError("unmocked CLI process"))
        self.cli_process.start()
        self.addCleanup(self.cli_process.stop)

    def test_preflight_prefixes_cover_every_native_provider(self):
        self.assertEqual({prefix.removesuffix("/") for prefix in preflight.NATIVE_PREFIXES},
                         llm_backends.PROVIDERS)

    def test_direct_api_roster_does_not_require_claude_or_router(self):
        with patch.dict(preflight.os.environ, {"MISTRAL_API_KEY": "test-only",
                                               "MOONSHOT_API_KEY": "test-only"}), \
             patch.object(preflight, "check_cli", side_effect=AssertionError("Claude used")), \
             patch.object(preflight, "_port_open", side_effect=AssertionError("router used")):
            result = preflight.run_checks(["mistral/mistral-small-latest", "moonshot/kimi-k2.6"])
        self.assertEqual(preflight.summarize(result)[2], 0)
        self.assertFalse(any("test-only" in detail for _, _, detail in result))

    def test_missing_direct_api_keys_fail_before_start(self):
        result = preflight.run_checks(["mistral/mistral-small-latest", "moonshot/kimi-k3"])
        self.assertEqual(preflight.summarize(result)[2], 2)

    def test_native_cli_only_checks_its_own_installation(self):
        with patch.object(llm_backends, "cli_launch", return_value=["mock-cli.exe"]) as launch, \
             patch.object(preflight, "check_cli", side_effect=AssertionError("Claude used")):
            result = preflight.run_checks(["codex/default", "gemini-cli/default"])
        self.assertEqual(preflight.summarize(result)[2], 0)
        self.assertEqual([call.args[0] for call in launch.call_args_list], ["codex", "gemini-cli"])

    def test_missing_native_cli_fails_without_paid_fallback(self):
        with patch.object(llm_backends, "cli_launch", side_effect=llm_backends.BackendError("missing")):
            result = preflight.run_checks(["codex/default"])
        failures = [entry for entry in result if entry[1] == "fail"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "Codex-CLI")

    def test_antigravity_only_runs_local_version_without_keys_or_paid_fallback(self):
        command = [r"C:\Test Install\agy.exe"]
        with patch.dict(preflight.os.environ, {"ISU_PREFLIGHT_PING": "1",
                                               "ISU_PREFLIGHT_NETWORK": "1"}), \
             patch.object(llm_backends, "cli_launch", return_value=command) as launch, \
             patch.object(preflight.subprocess, "run",
                          return_value=subprocess.CompletedProcess(command, 0)) as run, \
             patch.object(preflight, "check_cli", side_effect=AssertionError("Claude used")), \
             patch.object(preflight, "_port_open", side_effect=AssertionError("router used")):
            result = preflight.run_checks(["Antigravity/claude-sonnet-4-6",
                                           "antigravity/default"])
        launch.assert_called_once_with("antigravity")
        run.assert_called_once_with(
            [*command, "--version"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if preflight.os.name == "nt" else 0,
        )
        self.assertEqual(preflight.summarize(result)[2], 0)
        native = [entry for entry in result if entry[0] == "Antigravity-CLI"]
        self.assertEqual(len(native), 1)
        self.assertEqual(native[0][1], "ok")
        self.assertIn("OAuth", native[0][2])
        self.assertIn("nicht geprueft", native[0][2])
        self.assertIn("kein API-Key", native[0][2])

    def test_missing_antigravity_path_points_to_its_own_override(self):
        with patch.object(llm_backends, "cli_launch",
                          side_effect=llm_backends.BackendError("private-path-token")), \
             patch.object(preflight, "check_cli", side_effect=AssertionError("Claude used")):
            result = preflight.run_checks(["antigravity/default"])
        failures = [entry for entry in result if entry[1] == "fail"]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0][0], "Antigravity-CLI")
        self.assertIn("ISU_ANTIGRAVITY_CLI", failures[0][2])
        self.assertNotIn("private-path-token", str(result))

    def test_empty_antigravity_launch_does_not_start_any_process(self):
        with patch.object(llm_backends, "cli_launch", return_value=[]):
            result = preflight.check_native_cli(["antigravity/default"], "antigravity")
        self.assertEqual(result[:2], ("Antigravity-CLI", "fail"))

    def test_antigravity_version_nonzero_fails_without_exposing_output(self):
        secret = "fixture-access-token-do-not-print"
        with patch.object(llm_backends, "cli_launch", return_value=["mock-agy.exe"]), \
             patch.object(preflight.subprocess, "run", return_value=
                          subprocess.CompletedProcess([], 9, stdout=secret, stderr=secret)) as run:
            result = preflight.check_native_cli(["antigravity/default"], "antigravity")
        run.assert_called_once()
        self.assertEqual(result[:2], ("Antigravity-CLI", "fail"))
        self.assertNotIn(secret, str(result))

    def test_antigravity_version_errors_are_bounded_and_do_not_expose_details(self):
        secret = "fixture-user-private-path"
        for error in (OSError(secret),
                      subprocess.TimeoutExpired([secret], 10, output=secret, stderr=secret)):
            with self.subTest(error=type(error).__name__), \
                 patch.object(llm_backends, "cli_launch", return_value=["mock-agy.exe"]), \
                 patch.object(preflight.subprocess, "run", side_effect=error) as run:
                result = preflight.check_native_cli(["antigravity/default"], "antigravity")
            run.assert_called_once()
            self.assertEqual(result[:2], ("Antigravity-CLI", "fail"))
            self.assertNotIn(secret, str(result))

    def test_legacy_claude_check_is_preserved_in_mixed_roster(self):
        with patch.object(preflight, "check_cli", return_value=("Claude-CLI", "fail", "missing")) as cli, \
             patch.object(llm_backends, "cli_launch", return_value=["mock-cli.exe"]):
            result = preflight.run_checks(["sonnet", "codex/default"])
        cli.assert_called_once()
        self.assertEqual(preflight.summarize(result)[2], 1)

    def test_present_voice_keys_still_do_not_contact_cloud_by_default(self):
        with patch.dict(preflight.os.environ, {"DISCORD_BOT_TOKEN": "test-only",
                                               "ELEVENLABS_API_KEY": "test-only"}):
            self.assertEqual(preflight.check_discord()[1], "ok")
            self.assertEqual(preflight.check_elevenlabs()[1], "ok")

    def test_retired_moonshot_is_rejected_but_custom_models_are_allowed(self):
        self.assertEqual(preflight.check_retired_moonshot(["moonshot/kimi-k2.5"])[1], "fail")
        self.assertIsNone(preflight.check_retired_moonshot(["moonshot/Custom-Future-Model"]))
        self.assertEqual(llm_backends.parse_backend("moonshot/Custom-Future-Model").model,
                         "Custom-Future-Model")


class MenuAndSupervisorTests(unittest.TestCase):
    def test_cost_label_distinguishes_unknown_native_billing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for agent, content in (("native", "unknown\n"), ("claude", "1.25\n"),
                                   ("invalid", "nan\n"), ("zero", "0.0\n")):
                (root / agent).mkdir()
                (root / agent / "round_cost.txt").write_text(content, encoding="utf-8")
            with patch.object(arena_supervisor, "agent_home_dir", side_effect=lambda aid: str(root / aid)):
                self.assertEqual(arena_supervisor.round_cost_label(["native"]), "USD unbekannt")
                self.assertEqual(arena_supervisor.round_cost_label(["native", "claude"]),
                                 "unbekannt + 1.25 USD bekannt")
                self.assertEqual(arena_supervisor.round_cost_label(["claude"]), "1.25 USD")
                self.assertEqual(arena_supervisor.round_cost_label(["zero"]), "0.00 USD")
                self.assertEqual(arena_supervisor.round_cost_summary(["native", "invalid", "missing"]),
                                 (0.0, 3))
                self.assertEqual(arena_supervisor.round_cost_total(["native", "claude"]), 1.25)

    def test_custom_native_id_is_preserved_in_arena_request(self):
        with patch.dict(arena_supervisor.os.environ, {}, clear=True):
            for provider in ("mistral", "antigravity"):
                with self.subTest(provider=provider):
                    selector = provider + "/Custom-Fable-Model"
                    cfg = arena_supervisor.parse_command(
                        "start|viktor:1:" + selector + ":jaeger:Viktor|hostile:2")
                    self.assertEqual(cfg["agents"]["viktor"]["model"], selector)
                    self.assertTrue(cfg["free"])

    def test_fable_51_keeps_explicit_model_and_api_route(self):
        with patch.dict(arena_supervisor.os.environ, {}, clear=True):
            for model in ("claude-fable-5-1", "api/claude-fable-5-1", "api/fable-5.1"):
                cfg = arena_supervisor.parse_command("start|viktor:1:" + model)
                self.assertEqual(cfg["agents"]["viktor"]["model"], model)
                self.assertIsNone(preflight.check_fable([model]))
            self.assertEqual(arena_supervisor._effective_model("viktor", "claude-fable-5"), "sonnet")

    def test_native_roster_never_starts_cloud_router_or_llama(self):
        cfg = {"agents": {str(i): {"enabled": True, "model": model} for i, model in enumerate(
            ["codex/default", "gemini-cli/default", "antigravity/default",
             "mistral/mistral-small-latest", "moonshot/kimi-k3"])}}
        with patch.object(arena_supervisor, "port_open", side_effect=AssertionError("port probed")), \
             patch.object(arena_supervisor, "spawn_backend", side_effect=AssertionError("backend started")):
            self.assertTrue(arena_supervisor.ensure_backends(cfg))

    def test_menu_model_and_label_arrays_stay_aligned_and_dispatchable(self):
        source = (Path(__file__).resolve().parents[1] /
                  "mod/IsuVoice/scripts/5_Mission/IsuVoice/IsuArenaMenu.c").read_text(encoding="utf-8")
        arrays = {name: re.findall(r'"([^"\\]*)"', values) for name, values in
                  re.findall(r'static ref TStringArray (s_\w+) = \{([^}]+)\};', source)}
        providers = arrays["s_Providers"]
        id_fn = re.search(r'static TStringArray ProviderModelIds\(int p\)\s*\{([^}]+)', source)[1]
        label_fn = re.search(r'static TStringArray ProviderModelLabels\(int p\)\s*\{([^}]+)', source)[1]
        ids = {0: "s_AnthropicModels", **{int(i): arr for i, arr in
               re.findall(r'if \(p == (\d+)\) return (s_\w+);', id_fn)}}
        labels = {0: "s_AnthropicLabels", **{int(i): arr for i, arr in
                  re.findall(r'if \(p == (\d+)\) return (s_\w+);', label_fn)}}
        self.assertEqual(set(ids), set(range(len(providers))))
        self.assertEqual(set(labels), set(ids))
        native_providers = set()
        for index in ids:
            self.assertGreater(len(arrays[ids[index]]), 0)
            self.assertEqual(len(arrays[ids[index]]), len(arrays[labels[index]]), providers[index])
            for selector in arrays[ids[index]]:
                if llm_backends.is_native_backend(selector):
                    native_providers.add(llm_backends.parse_backend(selector).provider)
        self.assertEqual(native_providers, llm_backends.PROVIDERS)


if __name__ == "__main__":
    unittest.main()
