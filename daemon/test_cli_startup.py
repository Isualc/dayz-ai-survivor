"""Offline CLI startup regression tests; no login, subprocess or model calls.

Run: python -m unittest discover -s daemon -p test_cli_startup.py -v
"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import llm_backends


class CodexAppDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="isu-cli-startup-test-")
        self.addCleanup(self.folder.cleanup)
        self.local_app_data = Path(self.folder.name)
        # Keep filesystem operations native while exercising the Windows-only
        # branch, including when these tests run on a Linux CI worker.
        self.path_class = type(self.local_app_data)
        self.app_bin = self.local_app_data / "OpenAI" / "Codex" / "bin"
        self.env = {"PATH": "", "LOCALAPPDATA": str(self.local_app_data)}

    def executable(self, relative: str, *, mtime: float = 1000) -> Path:
        path = self.app_bin / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Offline fixture, never execute.")
        os.utime(path, (mtime, mtime))
        return path

    def launch(self, provider="codex", *, env=None, which_result=None, platform="nt"):
        with patch("llm_backends.os.name", platform), \
                patch("llm_backends.Path", self.path_class), \
                patch("llm_backends.shutil.which", return_value=which_result), \
                patch("llm_backends.subprocess.run", side_effect=AssertionError("No subprocess expected")):
            return llm_backends.cli_launch(provider, self.env if env is None else env)

    def test_direct_app_executable_is_found_without_path(self):
        executable = self.executable("codex.exe")
        self.assertEqual(self.launch(), [str(executable)])

    def test_versioned_app_executable_is_found_without_path(self):
        executable = self.executable("2026.901.3/codex.exe")
        self.assertEqual(self.launch(), [str(executable)])

    def test_newest_versioned_mtime_wins_over_directory_name(self):
        self.executable("9999-old-version/codex.exe", mtime=2000)
        newest = self.executable("0001-new-version/codex.exe", mtime=3000)
        self.assertEqual(self.launch(), [str(newest)])

    def test_stable_direct_install_takes_precedence_over_versioned_install(self):
        self.executable("versioned/codex.exe", mtime=2000)
        direct = self.executable("codex.exe", mtime=1000)
        self.assertEqual(self.launch(), [str(direct)])

    def test_invalid_explicit_override_does_not_fall_back_to_app(self):
        self.executable("codex.exe")
        env = dict(self.env, ISU_CODEX_CLI=str(self.local_app_data / "missing.exe"))
        with patch("llm_backends._codex_app_executable",
                   side_effect=AssertionError("Explicit override must not fall back")):
            with self.assertRaisesRegex(llm_backends.BackendError, "configured.*does not exist"):
                self.launch(env=env)

    def test_existing_path_executable_takes_precedence_over_app(self):
        self.executable("codex.exe", mtime=2000)
        path_executable = self.local_app_data / "user cli" / "codex.exe"
        path_executable.parent.mkdir()
        path_executable.write_bytes(b"Offline PATH fixture.")
        with patch("llm_backends._codex_app_executable",
                   side_effect=AssertionError("PATH must take precedence")):
            self.assertEqual(self.launch(which_result=str(path_executable)), [str(path_executable)])

    def test_no_app_or_path_executable_reports_missing_cli(self):
        with self.assertRaisesRegex(llm_backends.BackendError, "CLI not found"):
            self.launch()

    def test_directory_named_codex_exe_is_not_an_executable(self):
        (self.app_bin / "codex.exe").mkdir(parents=True)
        with self.assertRaisesRegex(llm_backends.BackendError, "CLI not found"):
            self.launch()

    def test_explicit_environment_does_not_read_host_localappdata(self):
        self.executable("codex.exe")
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.local_app_data)}):
            with self.assertRaisesRegex(llm_backends.BackendError, "CLI not found"):
                self.launch(env={"PATH": ""})

    def test_app_fallback_is_windows_only(self):
        self.executable("codex.exe")
        with self.assertRaisesRegex(llm_backends.BackendError, "CLI not found"):
            self.launch(platform="posix")

    def test_codex_app_is_never_used_as_gemini(self):
        self.executable("codex.exe")
        with self.assertRaisesRegex(llm_backends.BackendError, "gemini CLI not found"):
            self.launch(provider="gemini-cli")


class CliFailureDiagnosticsTests(unittest.TestCase):
    # These are deliberately fake fixtures. No real credentials are read.
    PRIVATE_FIXTURE = "access_token=fake-test-token account=fixture@example.invalid"

    def test_unsupported_client_is_classified_without_echoing_credentials(self):
        stdout = '{"error":{"code":"unsupported_client","details":"' + self.PRIVATE_FIXTURE + '"}}'
        detail = llm_backends.cli_failure_detail("gemini-cli", stdout, self.PRIVATE_FIXTURE)
        self.assertEqual(detail.split(":", 1)[0], "UNSUPPORTED_CLIENT")
        self.assertNotIn("fake-test-token", detail)
        self.assertNotIn("fixture@example.invalid", detail)
        self.assertEqual(detail, llm_backends.cli_failure_detail("gemini-cli", "unsupported_client", ""))

    def test_unsupported_client_diagnosis_is_specific_to_gemini(self):
        detail = llm_backends.cli_failure_detail("codex", "unsupported_client", "")
        self.assertEqual(detail.split(":", 1)[0], "CLI_REQUEST_FAILED")

    def test_policy_error_is_classified_without_echoing_private_details(self):
        detail = llm_backends.cli_failure_detail(
            "gemini-cli", "", "Policy file error: invalid policy rule; " + self.PRIVATE_FIXTURE)
        self.assertEqual(detail.split(":", 1)[0], "CLI_POLICY_INVALID")
        self.assertEqual(detail, llm_backends.cli_failure_detail("gemini-cli", "policy file error", ""))
        self.assertNotIn("fake-test-token", detail)

    def test_generic_failure_reports_only_fixed_diagnosis(self):
        detail = llm_backends.cli_failure_detail("codex", self.PRIVATE_FIXTURE, "Unrecognized private diagnostic")
        self.assertEqual(detail, "CLI_REQUEST_FAILED: check CLI login, version and account limits.")

    def test_nonzero_exit_propagates_classification_once_without_api_fallback(self):
        failures = (
            ("codex", "not logged in", "CLI_LOGIN_REQUIRED"),
            ("gemini-cli", "unsupported_client", "UNSUPPORTED_CLIENT"),
        )
        for provider, marker, code in failures:
            with self.subTest(provider=provider):
                result = subprocess.CompletedProcess(
                    args=["fixture-cli"], returncode=17,
                    stdout=marker + " " + self.PRIVATE_FIXTURE, stderr=self.PRIVATE_FIXTURE)
                with patch("llm_backends.cli_launch", return_value=["fixture-cli"]), \
                        patch("llm_backends.subprocess.run", return_value=result) as run, \
                        patch("llm_backends.ApiPlanner", side_effect=AssertionError("No API fallback")) as api, \
                        patch("llm_backends.urllib.request.build_opener",
                              side_effect=AssertionError("No network fallback")) as network:
                    planner = llm_backends.CliPlanner(llm_backends.parse_backend(provider + "/default"), env={"PATH": ""})
                    with self.assertRaises(llm_backends.BackendError) as caught:
                        planner.complete([{"role": "user", "content": "Private fixture prompt, do not log."}])
                run.assert_called_once()
                api.assert_not_called()
                network.assert_not_called()
                self.assertIn("exited 17: " + code + ":", str(caught.exception))
                self.assertIn("No API fallback", str(caught.exception))
                self.assertNotIn("fake-test-token", str(caught.exception))
                self.assertNotIn("fixture@example.invalid", str(caught.exception))
                self.assertNotIn("Private fixture prompt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
