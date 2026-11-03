"""Offline lifecycle tests; never launch the real watcher or access Hubspace."""

import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

import league_service as service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.cache = self.project / ".cache" / "league"

    def invoke(self, argv, **kwargs):
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            result = service.main(argv, project_dir=self.project, **kwargs)
        return result, output.getvalue(), errors.getvalue()

    def test_status_defaults_to_stopped_without_creating_runtime_files(self):
        result, output, errors = self.invoke([])
        self.assertEqual(result, 0)
        self.assertIn("stopped", output)
        self.assertEqual(errors, "")
        self.assertFalse(self.cache.exists())

    def test_runtime_publishes_status_and_releases_lock(self):
        with service.WatcherRuntime(self.project) as runtime:
            runtime.update_status(game_state="InProgress", theme="alive", dry_run=True,
                                  light_status={"Left bed": "applied"}, standard_restored=False)
            status = service.service_status(self.project)
            self.assertTrue(status["running"])
            self.assertEqual(status["pid"], os.getpid())
            self.assertIn("updated_at", status)
            self.assertEqual(status["game_state"], "InProgress")
            self.assertEqual(status["light_status"], {"Left bed": "applied"})
        status = service.service_status(self.project)
        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "stopped")
        self.assertEqual(status["theme"], "alive")
        self.assertTrue(status["dry_run"])
        self.assertTrue((self.cache / "watcher.lock").exists())

    def test_one_runtime_wins_and_loser_cannot_clear_stop_request(self):
        with service.WatcherRuntime(self.project) as first:
            first.stop_path.touch()
            with self.assertRaises(service.AlreadyRunningError):
                with service.WatcherRuntime(self.project):
                    self.fail("A second watcher reached its effect loop")
            self.assertTrue(first.should_stop())
            self.assertTrue(service.service_status(self.project)["running"])
        with service.WatcherRuntime(self.project) as replacement:
            self.assertFalse(replacement.should_stop())

    def test_exception_releases_lock_and_saves_type_without_message(self):
        with self.assertRaisesRegex(ValueError, "sensitive"):
            with service.WatcherRuntime(self.project):
                raise ValueError("sensitive-token")
        status = service.service_status(self.project)
        self.assertFalse(status["running"])
        self.assertEqual(status["last_error"], "ValueError")
        self.assertNotIn("sensitive", (self.cache / "status.json").read_text())

    def test_lock_releases_even_if_final_status_write_fails(self):
        runtime = service.WatcherRuntime(self.project)
        runtime.__enter__()
        self.addCleanup(runtime.__exit__, None, None, None)
        with patch.object(service, "_write_json", side_effect=OSError()), self.assertRaises(OSError):
            runtime.__exit__(None, None, None)
        self.assertFalse(service.service_status(self.project)["running"])
        with service.WatcherRuntime(self.project):
            pass

    def test_unknown_fields_are_not_written(self):
        with service.WatcherRuntime(self.project) as runtime:
            with self.assertRaises(ValueError):
                runtime.update_status(access_token="secret-value")
            self.assertNotIn("secret-value", runtime.status_path.read_text())

    def test_update_before_enter_is_rejected(self):
        with self.assertRaises(RuntimeError):
            service.WatcherRuntime(self.project).update_status(theme="standard")

    def test_stale_pid_is_not_treated_as_running(self):
        with service.WatcherRuntime(self.project):
            pass
        path = self.cache / "status.json"
        path.write_text(json.dumps({"pid": os.getpid(), "state": "running", "access_token": "secret-value"}))
        status = service.service_status(self.project)
        self.assertFalse(status["running"])
        self.assertNotIn("access_token", status)
        result, output, _ = self.invoke(["status"])
        self.assertEqual(result, 0)
        self.assertIn("stopped", output)
        self.assertNotIn("secret-value", output)

    def test_corrupt_status_does_not_hide_a_held_lock(self):
        with service.WatcherRuntime(self.project) as runtime:
            runtime.status_path.write_text("{incomplete")
            self.assertTrue(service.service_status(self.project)["running"])

    def test_status_shows_safe_game_and_light_details(self):
        with service.WatcherRuntime(self.project) as runtime:
            runtime.update_status(game_state="InProgress", theme="dead", dry_run=True,
                                  last_error="TimeoutError", light_status={"Left bed": "unavailable"})
            result, output, _ = self.invoke(["status"])
        self.assertEqual(result, 0)
        for expected in ("running", "InProgress", "dead", "Dry run: True", "TimeoutError", "Left bed: unavailable"):
            self.assertIn(expected, output)

    def test_start_uses_current_interpreter_project_cwd_log_and_detachment(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        snapshots = [{"running": False}, {"running": True, "pid": process.pid}]
        with patch.object(service.subprocess, "Popen", return_value=process) as popen, \
                patch.object(service, "service_status", side_effect=snapshots):
            result, output, errors = self.invoke(["start", "--dry-run", "--once"])
        self.assertEqual(result, 0)
        self.assertEqual(errors, "")
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, str(self.project / "league_watcher.py"), "--dry-run", "--once"])
        self.assertEqual(kwargs["cwd"], str(self.project))
        self.assertEqual(kwargs["stdin"], service.subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], service.subprocess.STDOUT)
        self.assertTrue(kwargs["close_fds"])
        self.assertTrue(kwargs["stdout"].closed)
        if os.name == "nt":
            self.assertTrue(kwargs["creationflags"] & service.subprocess.DETACHED_PROCESS)
        else:
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual((self.cache / "watcher.log").stat().st_mode & 0o777, 0o600)
        self.assertIn("12345", output)
        self.assertTrue((self.cache / "watcher.log").exists())

    def test_start_does_not_launch_when_lock_is_held(self):
        with service.WatcherRuntime(self.project), patch.object(service.subprocess, "Popen") as popen:
            result, output, _ = self.invoke(["start"])
        self.assertEqual(result, 0)
        self.assertIn("already running", output)
        popen.assert_not_called()

    def test_start_reports_early_failure_without_printing_log_contents(self):
        self.cache.mkdir(parents=True)
        (self.cache / "watcher.log").write_text("sensitive-token")
        process = Mock(pid=12345)
        process.poll.return_value = 1
        with patch.object(service.subprocess, "Popen", return_value=process):
            result, output, errors = self.invoke(["start"])
        self.assertEqual(result, 1)
        self.assertIn("exit 1", errors)
        self.assertIn("watcher.log", errors)
        self.assertNotIn("sensitive-token", output + errors)

    def test_start_handles_competing_watcher_winning_lock(self):
        process = Mock(pid=12345)
        process.poll.return_value = 1
        with patch.object(service.subprocess, "Popen", return_value=process), \
                patch.object(service, "service_status", side_effect=[{"running": False}, {"running": True, "pid": 54321}]):
            result, output, errors = self.invoke(["start"])
        self.assertEqual(result, 0)
        self.assertIn("already running", output)
        self.assertEqual(errors, "")

    def test_start_once_reports_success_when_child_already_finished(self):
        process = Mock(pid=12345)
        process.poll.return_value = 0
        with patch.object(service.subprocess, "Popen", return_value=process):
            result, output, _ = self.invoke(["start", "--dry-run", "--once"])
        self.assertEqual(result, 0)
        self.assertIn("completed one poll", output)

    def test_start_wait_is_bounded(self):
        process = Mock(pid=12345)
        process.poll.return_value = None
        with patch.object(service.subprocess, "Popen", return_value=process), \
                patch.object(service.time, "monotonic", side_effect=[0, 3]), \
                patch.object(service.time, "sleep") as sleep:
            result, output, _ = self.invoke(["start"])
        self.assertEqual(result, 0)
        self.assertIn("startup is still pending", output)
        sleep.assert_not_called()

    def test_stop_only_writes_request_and_waits_bounded(self):
        with service.WatcherRuntime(self.project) as runtime, \
                patch.object(service.time, "monotonic", side_effect=[0, 2]), \
                patch.object(service.os, "kill", create=True) as kill, \
                patch.object(service.time, "sleep") as sleep:
            result, output, _ = self.invoke(["stop"])
            self.assertTrue(runtime.should_stop())
            self.assertTrue(service.service_status(self.project)["running"])
        self.assertEqual(result, 0)
        self.assertIn("Stop requested", output)
        self.assertIn("restore standard lights", output)
        kill.assert_not_called()
        sleep.assert_not_called()

    def test_stop_observes_watcher_exit(self):
        self.cache.mkdir(parents=True)
        with patch.object(service, "service_status", side_effect=[{"running": True}, {"running": False}]):
            result, output, _ = self.invoke(["stop"])
        self.assertEqual(result, 0)
        self.assertIn("watcher stopped", output)

    def test_stopping_stale_pid_does_not_create_stop_request(self):
        with service.WatcherRuntime(self.project):
            pass
        result, output, _ = self.invoke(["stop"])
        self.assertEqual(result, 0)
        self.assertIn("already stopped", output)
        self.assertFalse((self.cache / "stop.request").exists())

    def test_run_forwards_flags_to_lazily_imported_watcher(self):
        watcher = types.SimpleNamespace(main=Mock(return_value=7))
        with patch.dict(sys.modules, {"league_watcher": watcher}):
            result, _, _ = self.invoke(["run", "--dry-run", "--once"])
        self.assertEqual(result, 7)
        watcher.main.assert_called_once_with(["--dry-run", "--once"])

    def test_help_and_status_do_not_import_watcher(self):
        with patch.dict(sys.modules, {"league_watcher": None}):
            with self.assertRaises(SystemExit) as outcome:
                self.invoke(["--help"])
            self.assertEqual(outcome.exception.code, 0)
            self.assertEqual(self.invoke(["status"])[0], 0)

    def test_status_rejects_run_flags(self):
        for argv in (["status", "--dry-run"], ["stop", "--once"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit) as outcome:
                self.invoke(argv)
            self.assertEqual(outcome.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
