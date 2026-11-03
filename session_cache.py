"""Optional startup caches: tokens stay in Keychain; device definitions stay local.

Cached devices describe controls, not their current state. Any cache failure is a
miss so an unavailable Keychain/cache directory never prevents a normal sign-in.
"""

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import tempfile
import time

import keyring


TOKEN_SERVICE = "hubspace-lights-session"
SCHEMA_VERSION = 1
DEVICE_TTL_SECONDS = 5 * 60
TOKEN_MARGIN_SECONDS = 30


def _valid_token(token):
    if not isinstance(token, (tuple, list)) or len(token) != 3:
        return False
    bearer, expires, created = token
    if not isinstance(bearer, str) or not bearer.startswith("Bearer "):
        return False
    value = bearer[len("Bearer "):]
    if not value or any(character.isspace() for character in value):
        return False
    if not all(isinstance(value, datetime) and value.tzinfo is None
               for value in (expires, created)):
        return False
    # This is the upstream library's UTC refresh deadline, not the JWT's expiry.
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    return expires > utc_now + timedelta(seconds=TOKEN_MARGIN_SECONDS)


def _valid_devices(records):
    if not isinstance(records, list):
        return False
    identities = set()
    for record in records:
        if not isinstance(record, dict):
            return False
        account_id, device = record.get("account_id"), record.get("device")
        if not isinstance(account_id, str) or not account_id.strip():
            return False
        if not isinstance(device, dict):
            return False
        device_id = device.get("id")
        if not isinstance(device_id, str) or not device_id.strip():
            return False
        identity = account_id, device_id
        if identity in identities:
            return False
        identities.add(identity)
        # Unnamed devices are supported by ID, just as they are in fresh lists.
        if device.get("friendlyName") is not None and not isinstance(device["friendlyName"], str):
            return False
        description = device.get("description")
        if not isinstance(description, dict):
            return False
        functions = description.get("functions")
        if not isinstance(functions, list):
            return False
        for function in functions:
            if not isinstance(function, dict):
                return False
            if not all(isinstance(function.get(field), str) and function[field]
                       for field in ("id", "functionClass", "type")):
                return False
    return True


class SessionCache:
    """Cache a single username's token and a short-lived list of light definitions."""

    def __init__(self, username: str, directory: Path):
        self.username = username
        self.directory = Path(directory)
        self.device_path = self.directory / "devices.json"

    def load_token(self):
        """Return the original naive datetime tuple, or miss without exposing it."""
        try:
            raw = keyring.get_password(TOKEN_SERVICE, self.username)
            if not raw:
                return None
            payload = json.loads(raw)
            if (not isinstance(payload, dict)
                    or type(payload.get("version")) is not int
                    or payload["version"] != SCHEMA_VERSION
                    or payload.get("username") != self.username):
                return None
            token = (
                payload["token"],
                datetime.fromisoformat(payload["expires_at"]),
                datetime.fromisoformat(payload["created_at"]),
            )
            return token if _valid_token(token) else None
        except Exception:
            return None

    def save_token(self, token):
        """Store only a usable token in a separate Keychain service."""
        try:
            if not _valid_token(token):
                return
            payload = {
                "version": SCHEMA_VERSION,
                "username": self.username,
                "token": token[0],
                "expires_at": token[1].isoformat(),
                "created_at": token[2].isoformat(),
            }
            keyring.set_password(TOKEN_SERVICE, self.username, json.dumps(payload))
        except Exception:
            pass

    def clear_token(self):
        """Forget the cached session, leaving the separately saved password alone."""
        try:
            keyring.delete_password(TOKEN_SERVICE, self.username)
        except Exception:
            pass

    def load_devices(self):
        """Load definitions only when their schema, username, and age match."""
        try:
            if self.directory.is_symlink() or self.device_path.is_symlink():
                return None
            payload = json.loads(self.device_path.read_text(encoding="utf-8"))
            if (not isinstance(payload, dict)
                    or type(payload.get("version")) is not int
                    or payload["version"] != SCHEMA_VERSION
                    or payload.get("username") != self.username):
                return None
            saved_at = payload.get("saved_at")
            if (type(saved_at) not in (int, float) or not math.isfinite(saved_at)
                    or not 0 <= time.time() - saved_at < DEVICE_TTL_SECONDS):
                return None
            devices = payload.get("devices")
            return devices if _valid_devices(devices) else None
        except Exception:
            return None

    def save_devices(self, devices):
        """Atomically replace a private metadata file; no tokens are written here."""
        temporary = None
        try:
            if not _valid_devices(devices) or self.directory.is_symlink():
                return
            payload = json.dumps({
                "version": SCHEMA_VERSION,
                "username": self.username,
                "saved_at": time.time(),
                "devices": devices,
            }, allow_nan=False)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.directory.chmod(0o700)
            descriptor, temporary = tempfile.mkstemp(prefix=".devices-", suffix=".json", dir=self.directory)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                if hasattr(os, "fchmod"):
                    os.fchmod(output.fileno(), 0o600)
                output.write(payload)
            os.replace(temporary, self.device_path)
        except Exception:
            pass
        finally:
            if temporary is not None:
                try:
                    Path(temporary).unlink(missing_ok=True)
                except Exception:
                    pass
