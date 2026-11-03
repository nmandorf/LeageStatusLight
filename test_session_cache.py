"""Offline cache lifecycle checks; every Keychain operation is replaced."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from session_cache import DEVICE_TTL_SECONDS, SessionCache, TOKEN_SERVICE


def utc_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def device_records():
    return [{"account_id": "test-account", "device": {
        "id": "test-light", "friendlyName": "Desk", "description": {
            "device": {"deviceClass": "light"},
            "functions": [{"id": "power", "functionClass": "power", "type": "category",
                           "values": [{"name": "on", "deviceValues": [{"value": "on"}]}]}],
        },
    }}]


class SessionCacheTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.cache = SessionCache("offline@example.invalid", self.root / "cache")
        self.storage = {}

        def get_password(service, username):
            return self.storage.get((service, username))

        def set_password(service, username, value):
            self.storage[service, username] = value

        def delete_password(service, username):
            self.storage.pop((service, username), None)

        for name, replacement in (("get_password", get_password), ("set_password", set_password),
                                  ("delete_password", delete_password)):
            mock = self.enterContext(patch("session_cache.keyring." + name, side_effect=replacement))
            setattr(self, name, mock)

    def token(self):
        return "Bearer offline-test-token", utc_now() + timedelta(minutes=4), datetime.now()

    def test_token_round_trip_preserves_naive_datetimes_and_stays_out_of_files(self):
        token = self.token()
        self.cache.save_token(token)
        self.assertEqual(self.cache.load_token(), token)
        self.set_password.assert_called_once()
        self.assertEqual(self.set_password.call_args.args[:2], (TOKEN_SERVICE, self.cache.username))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_tokens_are_isolated_by_username_and_clear_does_not_delete_password(self):
        token = self.token()
        self.cache.save_token(token)
        self.storage["hubspace-lights", self.cache.username] = "offline-password"
        other = SessionCache("other@example.invalid", self.cache.directory)
        self.assertIsNone(other.load_token())
        self.cache.clear_token()
        self.assertIsNone(self.cache.load_token())
        self.assertEqual(self.storage["hubspace-lights", self.cache.username], "offline-password")
        self.delete_password.assert_called_once_with(TOKEN_SERVICE, self.cache.username)

    def test_expired_near_expiry_aware_and_invalid_bearer_tokens_are_not_saved(self):
        good = self.token()
        invalid = [None, (), (None, *good[1:]), ("Bearer ", *good[1:]),
                   ("Bearer token with spaces", *good[1:]), ("Basic token", *good[1:]),
                   (good[0], utc_now() - timedelta(seconds=1), good[2]),
                   (good[0], utc_now() + timedelta(seconds=29), good[2]),
                   (good[0], datetime.now(timezone.utc) + timedelta(minutes=4), good[2]),
                   (good[0], good[1], "invalid")]
        for token in invalid:
            with self.subTest(token=token):
                self.cache.save_token(token)
        self.set_password.assert_not_called()

    def test_malformed_wrong_user_and_expired_keychain_data_are_cache_misses(self):
        self.cache.save_token(self.token())
        valid = json.loads(self.storage[TOKEN_SERVICE, self.cache.username])
        wrong_user = {**valid, "username": "other@example.invalid"}
        wrong_schema = {**valid, "version": 999}
        expired = {**valid, "expires_at": (utc_now() - timedelta(seconds=1)).isoformat()}
        malformed_date = {**valid, "created_at": "invalid"}
        for value in (None, "", "{broken", "null", "[]", "{}", json.dumps(wrong_user),
                      json.dumps(wrong_schema), json.dumps(expired), json.dumps(malformed_date)):
            with self.subTest(value=value):
                self.storage[TOKEN_SERVICE, self.cache.username] = value
                self.assertIsNone(self.cache.load_token())

    def test_keychain_backend_failures_are_optional_and_silent(self):
        self.get_password.side_effect = RuntimeError("offline Keychain failure")
        self.set_password.side_effect = RuntimeError("offline Keychain failure")
        self.delete_password.side_effect = RuntimeError("offline Keychain failure")
        self.assertIsNone(self.cache.load_token())
        self.cache.save_token(self.token())
        self.cache.clear_token()

    def test_device_round_trip_is_private_and_atomic(self):
        records = device_records()
        self.cache.save_devices(records)
        self.assertEqual(self.cache.load_devices(), records)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.cache.directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(self.cache.device_path.stat().st_mode), 0o600)
        self.assertEqual(list(self.cache.directory.iterdir()), [self.cache.device_path])
        self.get_password.assert_not_called()
        self.set_password.assert_not_called()

    def test_device_cache_writes_and_replaces_without_os_fchmod(self):
        # Replace only session_cache's os binding, leaving the process-wide os
        # module and tempfile implementation intact. Windows lacks fchmod.
        os_without_fchmod = SimpleNamespace(fdopen=os.fdopen, replace=os.replace)
        original = device_records()
        updated = deepcopy(original)
        updated[0]["device"]["friendlyName"] = "Updated desk"
        with patch("session_cache.os", os_without_fchmod):
            self.cache.save_devices(original)
            self.assertEqual(self.cache.load_devices(), original)
            self.cache.save_devices(updated)
            self.assertEqual(self.cache.load_devices(), updated)
        self.assertEqual(list(self.cache.directory.iterdir()), [self.cache.device_path])
        self.get_password.assert_not_called()
        self.set_password.assert_not_called()

    def test_device_cache_ttl_future_date_and_other_username(self):
        with patch("session_cache.time.time", return_value=1000):
            self.cache.save_devices(device_records())
        for now, valid in ((999, False), (1000, True), (1299.9, True),
                           (1000 + DEVICE_TTL_SECONDS, False)):
            with self.subTest(now=now), patch("session_cache.time.time", return_value=now):
                self.assertEqual(self.cache.load_devices() is not None, valid)
        other = SessionCache("other@example.invalid", self.cache.directory)
        with patch("session_cache.time.time", return_value=1000):
            self.assertIsNone(other.load_devices())

    def test_missing_and_malformed_device_caches_are_misses(self):
        self.assertIsNone(self.cache.load_devices())
        self.cache.save_devices(device_records())
        valid = json.loads(self.cache.device_path.read_text())
        for payload in ("not json", "null", "[]", "{}", json.dumps({**valid, "version": 99}),
                        json.dumps({**valid, "saved_at": float("nan")}),
                        json.dumps({**valid, "saved_at": True}),
                        json.dumps({**valid, "devices": [None]})):
            with self.subTest(payload=payload):
                self.cache.device_path.write_text(payload)
                self.assertIsNone(self.cache.load_devices())

    def test_missing_or_invalid_identity_and_capabilities_are_rejected(self):
        valid = device_records()
        bad_account = deepcopy(valid)
        bad_account[0]["account_id"] = ""
        missing_id = deepcopy(valid)
        del missing_id[0]["device"]["id"]
        missing_description = deepcopy(valid)
        del missing_description[0]["device"]["description"]
        missing_functions = deepcopy(valid)
        del missing_functions[0]["device"]["description"]["functions"]
        broken_function = deepcopy(valid)
        broken_function[0]["device"]["description"]["functions"] = [None]
        missing_function_id = deepcopy(valid)
        del missing_function_id[0]["device"]["description"]["functions"][0]["id"]
        invalid_name = deepcopy(valid)
        invalid_name[0]["device"]["friendlyName"] = []
        for records in (None, {}, [None], bad_account, missing_id, missing_description,
                        missing_functions, broken_function, missing_function_id, invalid_name,
                        valid + valid):
            with self.subTest(records=records):
                self.cache.save_devices(records)
                self.assertFalse(self.cache.device_path.exists())

    def test_empty_discovery_and_unnamed_lights_are_valid(self):
        self.cache.save_devices([])
        self.assertEqual(self.cache.load_devices(), [])
        unnamed = device_records()
        del unnamed[0]["device"]["friendlyName"]
        self.cache.save_devices(unnamed)
        self.assertEqual(self.cache.load_devices(), unnamed)

    def test_failed_atomic_replacement_preserves_previous_cache_and_cleans_temporary_file(self):
        original = device_records()
        self.cache.save_devices(original)
        changed = deepcopy(original)
        changed[0]["device"]["friendlyName"] = "New name"
        with patch("session_cache.os.replace", side_effect=PermissionError("offline failure")):
            self.cache.save_devices(changed)
        self.assertEqual(self.cache.load_devices(), original)
        self.assertEqual(list(self.cache.directory.iterdir()), [self.cache.device_path])

    def test_unwritable_cache_is_ignored(self):
        with patch("session_cache.Path.mkdir", side_effect=PermissionError("offline failure")):
            self.cache.save_devices(device_records())
        self.assertIsNone(self.cache.load_devices())

    def test_symlink_directory_is_ignored(self):
        target = self.root / "target"
        target.mkdir()
        try:
            self.cache.directory.symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            if os.name == "nt" and (
                isinstance(error, (PermissionError, NotImplementedError))
                or getattr(error, "winerror", None) == 1314
            ):
                self.skipTest("Windows symlinks require Developer Mode or the create-symlink privilege")
            raise
        self.cache.save_devices(device_records())
        self.assertEqual(list(target.iterdir()), [])
        self.assertIsNone(self.cache.load_devices())


if __name__ == "__main__":
    unittest.main()
