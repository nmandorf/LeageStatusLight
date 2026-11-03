"""Turn Hubspace lights red on your League death; restore warm light on respawn."""

import argparse
import asyncio
from dataclasses import dataclass
from enum import Enum
import os
import signal
import sys
from time import monotonic
from types import SimpleNamespace

from aiohttp import ClientSession, ClientTimeout

# Importing main also disables upstream logging that can contain credentials.
import main as controller
from errors import SetupError
from hubspace_connection import HubspaceConnection
from league_client import GameState, LiveGameClient
import league_settings as settings
from light_color import parse_color
from light_transport import apply_updates, build_updates
from session_cache import SessionCache


class Scene(str, Enum):
    STANDARD = "standard"
    DEAD = "dead"


class GameStateTracker:
    """Ignore a brief API outage; use normal lighting once a game disappears."""

    def __init__(self, grace=settings.DISCONNECT_GRACE):
        self.grace = grace
        self.scene = Scene.STANDARD
        self.missing_since = None

    def observe(self, state, now):
        if state in (GameState.ALIVE, GameState.DEAD):
            self.missing_since = None
            self.scene = Scene.DEAD if state == GameState.DEAD else Scene.STANDARD
        else:
            if self.missing_since is None:
                self.missing_since = now
            if now - self.missing_since >= self.grace:
                self.scene = Scene.STANDARD
        return self.scene


def scene_updates(light, scene):
    """Prepare one combined mode/color-or-temperature/brightness/power request."""
    if scene == Scene.DEAD:
        return build_updates(SimpleNamespace(
            command="set", value=settings.DEAD_BRIGHTNESS,
            color=parse_color(settings.DEAD_COLOR),
        ), light)
    temperature = build_updates(SimpleNamespace(
        command="temperature", value=str(settings.STANDARD_TEMPERATURE),
    ), light)
    strength = build_updates(SimpleNamespace(
        command="set", value=settings.STANDARD_BRIGHTNESS, color=None,
    ), light)
    return temperature + strength


class LeagueLighting:
    """Reuse one cloud session and recover each failed light independently."""

    def __init__(self, connection, clock=monotonic, report=print):
        self.connection = connection
        self.clock = clock
        self.report = report
        self.lights = None
        self.next_connect = 0.0
        self.applied = {}
        self.attempted = {}
        self.next_attempt = {}
        self.light_status = {}
        self.last_error = None
        self._reported = {}

    def _note(self, name, message):
        if self._reported.get(name) != message:
            self.report(f"{name}: {message}", flush=True)
            self._reported[name] = message

    async def _connect(self):
        if self.lights is not None:
            return True
        if self.clock() < self.next_connect:
            return False
        try:
            await asyncio.wait_for(self.connection.authenticate(), timeout=30)
            lights = await asyncio.wait_for(self.connection.load_lights(), timeout=30)
            if not lights:
                raise SetupError("No supported lights found")
            self.lights = lights
            self.last_error = None
            return True
        except Exception as error:
            self.last_error = f"Hubspace connection failed ({type(error).__name__}); will retry."
            self._note("Hubspace", self.last_error)
            self.next_connect = self.clock() + settings.LIGHT_RETRY_INTERVAL
            return False

    async def sync(self, desired, stopping=None, force=False):
        """Read desired() before each bulb so an old scene is never queued."""
        if not await self._connect():
            return False
        # Give responsive lights priority over bulbs that recently failed.
        for light in sorted(self.lights, key=lambda item: item.id in self.next_attempt):
            if stopping is not None and stopping():
                return False
            scene = desired()
            key = light.id
            name = light.name or key
            if not force and self.applied.get(key) == scene:
                continue
            if not force and self.clock() < self.next_attempt.get(key, 0):
                continue
            self.attempted[key] = scene
            # An interrupted/failed request may still have changed this light.
            # Invalidate the old success so a respawn always restores standard.
            self.applied.pop(key, None)
            try:
                updates = scene_updates(light, scene)
                await asyncio.wait_for(apply_updates(light, updates), timeout=settings.CLOUD_REQUEST_TIMEOUT)
            except Exception as error:
                message = ("Lighting settings unsupported; check league_settings.py."
                           if isinstance(error, SetupError) else
                           f"{type(error).__name__}; check this bulb's Wi-Fi. Retrying.")
                self.light_status[name] = message
                self.next_attempt[key] = self.clock() + settings.LIGHT_RETRY_INTERVAL
                self._note(name, message)
                continue
            self.applied[key] = scene
            self.next_attempt.pop(key, None)
            message = (f"red at {settings.DEAD_BRIGHTNESS}% sent to Hubspace"
                       if scene == Scene.DEAD else
                       f"{settings.STANDARD_BRIGHTNESS}% at {settings.STANDARD_TEMPERATURE} K sent to Hubspace")
            self.light_status[name] = message
            self._note(name, message)
        return all(self.applied.get(light.id) == desired() for light in self.lights)


@dataclass
class WatchState:
    scene: Scene = Scene.STANDARD
    game: GameState = GameState.UNAVAILABLE


async def _pause(stop, seconds):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def run_watcher(runtime, *, dry_run=False, once=False):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            pass

    state = WatchState()
    tracker = GameStateTracker()
    lighting = None
    worker = None
    try:
        async with ClientSession(timeout=ClientTimeout(total=30), trust_env=False) as cloud_session, \
                ClientSession(timeout=ClientTimeout(total=2), trust_env=False) as game_session:
            game = LiveGameClient(game_session)
            if not dry_run:
                email = controller.username()
                password = os.environ.get("HUBSPACE_PASSWORD") or controller.keyring.get_password(
                    controller.KEYCHAIN_SERVICE, email)
                if not password:
                    raise SetupError("No saved Hubspace password. Run light login first.")
                connection = HubspaceConnection(email, password, cloud_session,
                                               SessionCache(email, controller.ROOT / ".cache"))
                lighting = LeagueLighting(connection)

            def publish():
                runtime.update_status(
                    state="running", game_state=state.game.value, theme=state.scene.value,
                    dry_run=dry_run,
                    light_status=dict(lighting.light_status) if lighting else {},
                    last_error=lighting.last_error if lighting else None,
                )

            async def control_lights():
                while not stop.is_set():
                    await lighting.sync(lambda: state.scene, stopping=stop.is_set)
                    publish()
                    await _pause(stop, settings.POLL_INTERVAL)

            print(f"League lights {'preview' if dry_run else 'watcher'} running. "
                  "Stop with light league stop or Ctrl+C.", flush=True)
            print(f"Alive / outside a game: {settings.STANDARD_BRIGHTNESS}% at "
                  f"{settings.STANDARD_TEMPERATURE} K. Dead: {settings.DEAD_COLOR} at "
                  f"{settings.DEAD_BRIGHTNESS}%.", flush=True)

            restored = True
            try:
                # Sample before the first command so starting while already
                # dead does not briefly apply the normal scene first.
                last_game = None
                while not stop.is_set() and not runtime.should_stop():
                    state.game = await game.sample()
                    state.scene = tracker.observe(state.game, monotonic())
                    if state.game != last_game:
                        label = {
                            GameState.ALIVE: "You are alive; normal lighting.",
                            GameState.DEAD: "You are dead; red lighting.",
                            GameState.UNAVAILABLE: "Waiting for League's live game data on this computer.",
                        }[state.game]
                        print(label, flush=True)
                        last_game = state.game
                    publish()
                    if once:
                        if lighting:
                            success = await lighting.sync(lambda: state.scene)
                            publish()
                            return 0 if success else 1
                        return 0
                    if lighting and worker is None:
                        worker = asyncio.create_task(control_lights())
                    if worker is not None and worker.done():
                        await worker  # Surface an unexpected worker error safely.
                    await _pause(stop, settings.POLL_INTERVAL)
            finally:
                # Run cleanup before closing the cloud session, including on
                # cancellation (the Ctrl+C fallback on Windows).
                stop.set()
                if worker is not None:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)
                    worker = None
                if not once and lighting and lighting.lights is not None:
                    print("Restoring normal lighting before stopping.", flush=True)
                    restored = await lighting.sync(lambda: Scene.STANDARD, force=True)
                    runtime.update_status(standard_restored=restored,
                                          theme=Scene.STANDARD.value,
                                          light_status=dict(lighting.light_status))
            return 0 if restored else 1
    finally:
        stop.set()
        if worker is not None:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)
        for sig in installed_signals:
            loop.remove_signal_handler(sig)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Read League state without accessing Hubspace or changing lights")
    parser.add_argument("--once", action="store_true", help="Check once, apply the current scene, then exit")
    args = parser.parse_args(argv)
    from league_service import AlreadyRunningError, WatcherRuntime
    try:
        with WatcherRuntime() as runtime:
            return asyncio.run(run_watcher(runtime, dry_run=args.dry_run, once=args.once))
    except AlreadyRunningError:
        print("League lights is already running. Use light league status or light league stop.", file=sys.stderr)
    except SetupError as error:
        print(f"Error: {error}", file=sys.stderr)
    except KeyboardInterrupt:
        print("League lights stopped.")
    except Exception as error:
        print(f"League lights failed ({type(error).__name__}). Check light league status.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
