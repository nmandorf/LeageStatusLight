"""Local process lifecycle for the League light watcher; no cloud imports here."""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


PROJECT_DIR = Path(__file__).resolve().parent
START_TIMEOUT = 2.0
STOP_TIMEOUT = 1.0
POLL_INTERVAL = 0.05
PUBLIC_FIELDS = frozenset({
    "state", "game_state", "theme", "dry_run", "last_error", "light_status",
    "standard_restored",
})


class AlreadyRunningError(RuntimeError):
    """Another watcher owns the project lock."""


def _paths(project_dir=None):
    project = Path(project_dir if project_dir is not None else PROJECT_DIR).resolve()
    cache = project / ".cache" / "league"
    return project, cache, cache / "watcher.lock", cache / "status.json", cache / "stop.request", cache / "watcher.log"


def _timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _try_lock(handle):
    """Acquire a nonblocking OS lock; the persistent file is never unlinked."""
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            return False
        raise
    return True


def _unlock(handle):
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _private_file(path, flags):
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "ab" if flags & os.O_APPEND else "r+b") as handle:
        yield handle


def _write_json(path, data):
    descriptor, temporary = tempfile.mkstemp(prefix="status-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class WatcherRuntime:
    """Hold the one-watcher lock and publish safe status during a watcher run.

    Enter this context before authentication or any light effects. Callers must
    pass only public state and sanitized error types, never tokens, URLs,
    credentials, raw API responses, or exception messages to ``update_status``.
    The watcher owns restoring standard lights before leaving this context.
    """

    def __init__(self, project_dir=None):
        (self.project_dir, self.cache_dir, self.lock_path, self.status_path,
         self.stop_path, self.log_path) = _paths(project_dir)
        self._handle = None
        self._status = {}

    def __enter__(self):
        if self._handle is not None:
            raise RuntimeError("WatcherRuntime is already entered")
        self.cache_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        handle = os.fdopen(descriptor, "r+b")
        try:
            if not _try_lock(handle):
                raise AlreadyRunningError("The League watcher is already running.")
            self._handle = handle
            # Only the winning watcher may remove a request from an old run.
            self.stop_path.unlink(missing_ok=True)
            self._status = {"state": "starting", "game_state": "unknown", "theme": None,
                            "dry_run": False, "last_error": None, "light_status": {}}
            self.update_status()
        except BaseException:
            self._handle = None
            handle.close()  # Closing also releases a successfully acquired lock.
            raise
        return self

    def should_stop(self):
        return self.stop_path.exists()

    def update_status(self, **fields):
        if self._handle is None:
            raise RuntimeError("WatcherRuntime must be entered before updating status")
        if fields.keys() - PUBLIC_FIELDS:
            raise ValueError("Only public watcher status fields may be saved")
        self._status.update(fields)
        self._status.update(pid=os.getpid(), updated_at=_timestamp())
        _write_json(self.status_path, self._status)

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            if self._handle is not None:
                fields = {"state": "stopped"}
                if exc_type is not None:
                    fields["last_error"] = exc_type.__name__
                self.update_status(**fields)
        finally:
            if self._handle is not None:
                try:
                    _unlock(self._handle)
                finally:
                    self._handle.close()
                    self._handle = None


def service_status(project_dir=None):
    """Return saved public state plus liveness established by the OS lock."""
    _, _, lock_path, status_path, _, log_path = _paths(project_dir)
    try:
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (FileNotFoundError, ValueError, UnicodeError):
        data = {}
    data = {key: value for key, value in data.items() if key in PUBLIC_FIELDS or key in {"pid", "updated_at"}}
    running = False
    try:
        with lock_path.open("r+b") as handle:
            running = not _try_lock(handle)
            if not running:
                _unlock(handle)
    except FileNotFoundError:
        pass
    return {**data, "running": running, "log_path": str(log_path)}


def _show_status(project_dir=None):
    status = service_status(project_dir)
    if status["running"]:
        print(f"League watcher: running (PID {status.get('pid', 'starting')}).")
    else:
        print("League watcher: stopped.")
    for field, label in (("game_state", "Game"), ("theme", "Theme"), ("dry_run", "Dry run"),
                         ("last_error", "Last error"), ("updated_at", "Updated")):
        if status.get(field) is not None:
            print(f"{label}: {status[field]}")
    light_status = status.get("light_status", {})
    if isinstance(light_status, dict):
        for name, value in light_status.items():
            print(f"Light {name}: {value}")
    print(f"Log: {status['log_path']}")
    return 0


def _start(args, project_dir=None):
    project, cache, _, _, _, log_path = _paths(project_dir)
    if service_status(project)["running"]:
        print("League watcher is already running.")
        return 0
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    command = [sys.executable, str(project / "league_watcher.py")]
    command.extend(flag for flag, enabled in (("--dry-run", args.dry_run), ("--once", args.once)) if enabled)
    options = {"cwd": str(project), "stdin": subprocess.DEVNULL,
               "stderr": subprocess.STDOUT, "close_fds": True}
    if os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        options["start_new_session"] = True
    with _private_file(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND) as logfile:
        process = subprocess.Popen(command, stdout=logfile, **options)
    deadline = time.monotonic() + START_TIMEOUT
    while True:
        return_code = process.poll()
        status = service_status(project)
        if return_code is not None:
            if status["running"]:
                print("League watcher is already running.")
                return 0
            if return_code == 0 and args.once:
                print(f"League watcher completed one poll. Log: {log_path}")
                return 0
            print(f"League watcher exited before startup completed (exit {return_code}). Log: {log_path}", file=sys.stderr)
            return 1
        if status["running"] and status.get("pid") == process.pid:
            print(f"League watcher started (PID {process.pid}). Log: {log_path}")
            return 0
        if time.monotonic() >= deadline:
            if status["running"]:
                print(f"League watcher is running. Log: {log_path}")
            else:
                print(f"League watcher launched; startup is still pending. Check 'light league status'. Log: {log_path}")
            return 0
        time.sleep(POLL_INTERVAL)


def _stop(project_dir=None):
    project, _, _, _, stop_path, _ = _paths(project_dir)
    if not service_status(project)["running"]:
        print("League watcher is already stopped.")
        return 0
    with _private_file(stop_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND):
        pass
    deadline = time.monotonic() + STOP_TIMEOUT
    while service_status(project)["running"]:
        if time.monotonic() >= deadline:
            print("Stop requested; the watcher will restore standard lights and exit.")
            return 0
        time.sleep(POLL_INTERVAL)
    print("League watcher stopped.")
    return 0


def main(argv=None, *, project_dir=None):
    parser = argparse.ArgumentParser(prog="light league", description="Run the League light watcher locally.")
    parser.add_argument("action", nargs="?", choices=("start", "stop", "status", "run"), default="status")
    parser.add_argument("--dry-run", action="store_true", help="Observe game state without Hubspace credentials or light writes")
    parser.add_argument("--once", action="store_true", help="Poll and apply the current state once, then exit")
    args = parser.parse_args(argv)
    if args.action in ("stop", "status") and (args.dry_run or args.once):
        parser.error("--dry-run and --once apply to start or run only")
    if args.action == "run":
        # Help/status/stop/start must not import cloud dependencies or authenticate.
        import league_watcher
        flags = [flag for flag, enabled in (("--dry-run", args.dry_run), ("--once", args.once)) if enabled]
        return league_watcher.main(flags)
    try:
        if args.action == "start":
            return _start(args, project_dir)
        if args.action == "stop":
            return _stop(project_dir)
        return _show_status(project_dir)
    except OSError as error:
        print(f"Could not {args.action} League watcher ({type(error).__name__}). Check access to the project .cache/league directory.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
