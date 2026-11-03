"""Setup checks without real credentials, prompts, or cloud access."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import configure_windows


class WindowsSetupTests(unittest.TestCase):
    def test_email_only_is_written_and_hidden_login_is_reused(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(configure_windows.controller, "ROOT", Path(folder)), \
                patch("builtins.input", return_value="player@example.invalid"), \
                patch.object(configure_windows.controller, "main", return_value=0) as login, \
                patch.dict(configure_windows.os.environ, {"HUBSPACE_USERNAME": "old@example.invalid"}), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure_windows.main(), 0)
            self.assertEqual(json.loads((Path(folder) / "config.json").read_text()),
                             {"username": "player@example.invalid"})
            self.assertEqual(configure_windows.os.environ["HUBSPACE_USERNAME"], "player@example.invalid")
            login.assert_called_once_with(["login"])

    def test_invalid_email_does_not_write_or_authenticate(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(configure_windows.controller, "ROOT", Path(folder)), \
                patch("builtins.input", return_value="not an email"), \
                patch.object(configure_windows.controller, "main") as login, \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(configure_windows.main(), 1)
            self.assertFalse((Path(folder) / "config.json").exists())
            login.assert_not_called()

    def test_login_failure_is_returned_to_installer(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(configure_windows.controller, "ROOT", Path(folder)), \
                patch("builtins.input", return_value="player@example.invalid"), \
                patch.object(configure_windows.controller, "main", return_value=1), \
                patch.dict(configure_windows.os.environ, {}, clear=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(configure_windows.main(), 1)


if __name__ == "__main__":
    unittest.main()
