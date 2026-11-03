"""Offline League watcher checks: fake games, cloud responses, and credentials."""

import asyncio
from contextlib import ExitStack, contextmanager, redirect_stdout
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from hubspaceng.errors import RequestError

from league_client import GameState
import league_watcher as watcher
from league_watcher import GameStateTracker, LeagueLighting, Scene, scene_updates
from light_transport import apply_updates
from test_light_transport import full_light


def fake_light(identity="light-1", name="Desk"):
    light = full_light()
    light._id = identity
    light.device_json["friendlyName"] = name
    return light


def requested_values(call):
    return {item["functionClass"]: item["value"]
            for item in call.kwargs["json"]["values"]}


STANDARD = {"color-mode": "white", "color-temperature": 2700,
            "brightness": 20, "power": "on"}
DEAD = {"color-mode": "color", "color-rgb": {"color-rgb": {"r": 255, "g": 0, "b": 0}},
        "brightness": 100, "power": "on"}


class TrackerTests(unittest.TestCase):
    def test_no_game_alive_death_respawn_and_game_exit(self):
        tracker = GameStateTracker(grace=3)
        cases = [
            (GameState.UNAVAILABLE, 0, Scene.STANDARD),
            (GameState.ALIVE, 1, Scene.STANDARD),
            (GameState.DEAD, 2, Scene.DEAD),
            (GameState.ALIVE, 3, Scene.STANDARD),
            (GameState.UNAVAILABLE, 4, Scene.STANDARD),
            (GameState.UNAVAILABLE, 7, Scene.STANDARD),
        ]
        for state, now, scene in cases:
            self.assertIs(tracker.observe(state, now), scene)

    def test_brief_outage_retains_red_then_restores_at_grace_boundary(self):
        tracker = GameStateTracker(grace=3)
        self.assertIs(tracker.observe(GameState.DEAD, 0), Scene.DEAD)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 1), Scene.DEAD)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 3.999), Scene.DEAD)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 4), Scene.STANDARD)

    def test_valid_observation_resets_missing_data_grace(self):
        tracker = GameStateTracker(grace=3)
        tracker.observe(GameState.DEAD, 0)
        tracker.observe(GameState.UNAVAILABLE, 1)
        tracker.observe(GameState.DEAD, 3)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 5), Scene.DEAD)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 7.999), Scene.DEAD)
        self.assertIs(tracker.observe(GameState.UNAVAILABLE, 8), Scene.STANDARD)


class LightingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = Mock(return_value=0.0)
        self.report = Mock()

    def lighting(self, lights):
        connection = SimpleNamespace(authenticate=AsyncMock(),
                                     load_lights=AsyncMock(return_value=lights))
        return LeagueLighting(connection, clock=self.clock, report=self.report)

    async def test_standard_is_one_exact_combined_white_2700k_20_percent_on_put(self):
        light = fake_light()
        with patch.object(watcher.settings, "STANDARD_BRIGHTNESS", 20), \
                patch.object(watcher.settings, "STANDARD_TEMPERATURE", 2700):
            await apply_updates(light, scene_updates(light, Scene.STANDARD))
        light.api.request.assert_awaited_once()
        request = light.api.request.await_args
        self.assertEqual(request.kwargs["method"], "PUT")
        self.assertEqual(requested_values(request), STANDARD)
        self.assertEqual(len(request.kwargs["json"]["values"]), 4)

    async def test_death_is_one_red_on_put_with_configurable_brightness(self):
        light = fake_light()
        with patch.object(watcher.settings, "DEAD_BRIGHTNESS", 42), \
                patch.object(watcher.settings, "DEAD_COLOR", "red"):
            await apply_updates(light, scene_updates(light, Scene.DEAD))
        light.api.request.assert_awaited_once()
        request = light.api.request.await_args
        self.assertEqual(request.kwargs["method"], "PUT")
        self.assertEqual(requested_values(request), {**DEAD, "brightness": 42})
        self.assertEqual(len(request.kwargs["json"]["values"]), 4)

    async def test_sequence_changes_lights_only_when_scene_changes(self):
        light = fake_light()
        lighting = self.lighting([light])
        tracker = GameStateTracker(grace=3)
        for state, now in (
            (GameState.UNAVAILABLE, 0), (GameState.ALIVE, 1),
            (GameState.DEAD, 2), (GameState.DEAD, 3),
            (GameState.ALIVE, 4), (GameState.UNAVAILABLE, 5),
            (GameState.UNAVAILABLE, 8),
        ):
            scene = tracker.observe(state, now)
            self.assertTrue(await lighting.sync(lambda: scene))
        self.assertEqual([requested_values(call) for call in light.api.request.await_args_list],
                         [STANDARD, DEAD, STANDARD])
        lighting.connection.authenticate.assert_awaited_once()
        lighting.connection.load_lights.assert_awaited_once()

    async def test_game_exit_while_dead_restores_only_after_grace(self):
        light = fake_light()
        lighting = self.lighting([light])
        tracker = GameStateTracker(grace=3)
        for state, now in ((GameState.DEAD, 0), (GameState.UNAVAILABLE, 1),
                           (GameState.UNAVAILABLE, 3.9), (GameState.UNAVAILABLE, 4)):
            scene = tracker.observe(state, now)
            await lighting.sync(lambda: scene)
        self.assertEqual([requested_values(call) for call in light.api.request.await_args_list],
                         [DEAD, STANDARD])

    async def test_failed_bulb_does_not_prevent_healthy_bulb_and_retries_are_bounded(self):
        failed, healthy = fake_light(), fake_light("light-2", "Bed")
        failed.api.request.side_effect = RequestError("private failure detail")
        lighting = self.lighting([failed, healthy])
        self.assertFalse(await lighting.sync(lambda: Scene.DEAD))
        self.assertEqual(requested_values(healthy.api.request.await_args), DEAD)
        self.assertNotIn("private failure detail", str(self.report.call_args_list))
        self.clock.return_value = watcher.settings.LIGHT_RETRY_INTERVAL - 0.01
        self.assertFalse(await lighting.sync(lambda: Scene.DEAD))
        failed.api.request.assert_awaited_once()
        healthy.api.request.assert_awaited_once()
        self.clock.return_value = watcher.settings.LIGHT_RETRY_INTERVAL
        self.assertFalse(await lighting.sync(lambda: Scene.DEAD))
        self.assertEqual(failed.api.request.await_count, 2)
        healthy.api.request.assert_awaited_once()

    async def test_new_scene_updates_healthy_bulb_while_failed_bulb_is_in_backoff(self):
        failed, healthy = fake_light(), fake_light("light-2", "Bed")
        failed.api.request.side_effect = RequestError("offline")
        lighting = self.lighting([failed, healthy])
        await lighting.sync(lambda: Scene.DEAD)
        self.assertFalse(await lighting.sync(lambda: Scene.STANDARD))
        failed.api.request.assert_awaited_once()
        self.assertEqual([requested_values(call) for call in healthy.api.request.await_args_list],
                         [DEAD, STANDARD])

    async def test_recovered_bulb_retries_current_scene_not_old_failed_scene(self):
        light = fake_light()
        echo_response = light.api.request.side_effect
        light.api.request.side_effect = RequestError("offline")
        lighting = self.lighting([light])
        await lighting.sync(lambda: Scene.DEAD)
        light.api.request.side_effect = echo_response
        self.clock.return_value = watcher.settings.LIGHT_RETRY_INTERVAL
        self.assertTrue(await lighting.sync(lambda: Scene.STANDARD))
        self.assertEqual(requested_values(light.api.request.await_args), STANDARD)

    async def test_connection_failure_uses_backoff_and_then_recovers(self):
        light = fake_light()
        lighting = self.lighting([light])
        lighting.connection.authenticate.side_effect = [RuntimeError("secret"), None]
        self.assertFalse(await lighting.sync(lambda: Scene.DEAD))
        self.assertFalse(await lighting.sync(lambda: Scene.STANDARD))
        lighting.connection.authenticate.assert_awaited_once()
        light.api.request.assert_not_awaited()
        self.assertNotIn("secret", str(self.report.call_args_list))
        self.clock.return_value = watcher.settings.LIGHT_RETRY_INTERVAL
        self.assertTrue(await lighting.sync(lambda: Scene.STANDARD))
        self.assertIsNone(lighting.last_error)
        self.assertEqual(requested_values(light.api.request.await_args), STANDARD)

    async def test_scene_change_during_put_does_not_queue_stale_scene_for_next_bulb(self):
        first, second = fake_light(), fake_light("light-2", "Bed")
        lighting = self.lighting([first, second])
        desired = Scene.DEAD
        started, release = asyncio.Event(), asyncio.Event()

        async def delayed_echo(**request):
            started.set()
            await release.wait()
            return None, request["json"]

        first.api.request.side_effect = delayed_echo
        pending = asyncio.create_task(lighting.sync(lambda: desired))
        try:
            await asyncio.wait_for(started.wait(), 1)
            desired = Scene.STANDARD
            release.set()
            self.assertFalse(await asyncio.wait_for(pending, 1))
        finally:
            release.set()
            if not pending.done():
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        self.assertEqual(requested_values(first.api.request.await_args), DEAD)
        self.assertEqual(requested_values(second.api.request.await_args), STANDARD)
        self.assertTrue(await lighting.sync(lambda: desired))
        self.assertEqual(requested_values(first.api.request.await_args), STANDARD)
        second.api.request.assert_awaited_once()

    async def test_failed_or_partial_red_invalidates_prior_standard_success(self):
        async def partial_response(**request):
            return None, {"metadeviceId": request["json"]["metadeviceId"],
                          "values": request["json"]["values"][:1]}

        for failure in (RequestError("offline"), partial_response):
            with self.subTest(failure=type(failure).__name__):
                self.clock.return_value = 0
                light = fake_light()
                echo_response = light.api.request.side_effect
                lighting = self.lighting([light])
                self.assertTrue(await lighting.sync(lambda: Scene.STANDARD))
                light.api.request.side_effect = failure
                self.assertFalse(await lighting.sync(lambda: Scene.DEAD))
                light.api.request.side_effect = echo_response
                self.clock.return_value = watcher.settings.LIGHT_RETRY_INTERVAL
                self.assertTrue(await lighting.sync(lambda: Scene.STANDARD))
                self.assertEqual([requested_values(call) for call in light.api.request.await_args_list],
                                 [STANDARD, DEAD, STANDARD])

    async def test_force_restore_ignores_failed_bulb_backoff(self):
        light = fake_light()
        echo_response = light.api.request.side_effect
        lighting = self.lighting([light])
        light.api.request.side_effect = RequestError("offline")
        await lighting.sync(lambda: Scene.DEAD)
        light.api.request.side_effect = echo_response
        self.assertTrue(await lighting.sync(lambda: Scene.STANDARD, force=True))
        self.assertEqual(light.api.request.await_count, 2)
        self.assertEqual(requested_values(light.api.request.await_args), STANDARD)


class FakeSession:
    def __init__(self):
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


@contextmanager
def fake_runtime(lights, game_state=GameState.DEAD):
    """Prevent network, Keychain, cache, signal, and runtime filesystem effects."""
    runtime = SimpleNamespace(should_stop=Mock(return_value=False), update_status=Mock())
    connection = SimpleNamespace(authenticate=AsyncMock(),
                                 load_lights=AsyncMock(return_value=lights))
    sessions = [FakeSession(), FakeSession()]
    game = SimpleNamespace(sample=AsyncMock(return_value=game_state))
    loop = asyncio.get_running_loop()
    with ExitStack() as stack:
        session_factory = stack.enter_context(patch.object(watcher, "ClientSession", side_effect=sessions))
        game_factory = stack.enter_context(patch.object(watcher, "LiveGameClient", return_value=game))
        connection_factory = stack.enter_context(patch.object(watcher, "HubspaceConnection", return_value=connection))
        cache = stack.enter_context(patch.object(watcher, "SessionCache"))
        username = stack.enter_context(patch.object(watcher.controller, "username", return_value="offline@example.invalid"))
        keychain = stack.enter_context(patch.object(watcher.controller.keyring, "get_password", return_value="offline-password"))
        stack.enter_context(patch.dict(watcher.os.environ, {"HUBSPACE_PASSWORD": ""}))
        add_signal = stack.enter_context(patch.object(loop, "add_signal_handler"))
        remove_signal = stack.enter_context(patch.object(loop, "remove_signal_handler"))
        stack.enter_context(patch.object(watcher.settings, "POLL_INTERVAL", 0.001))
        stack.enter_context(redirect_stdout(io.StringIO()))
        yield SimpleNamespace(runtime=runtime, connection=connection, game=game,
                              sessions=sessions, session_factory=session_factory,
                              game_factory=game_factory, connection_factory=connection_factory,
                              cache=cache, username=username, keychain=keychain,
                              add_signal=add_signal, remove_signal=remove_signal)


class WatcherLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_run_once_never_uses_credentials_cache_or_hubspace(self):
        with fake_runtime([]) as fake:
            self.assertEqual(await watcher.run_watcher(fake.runtime, dry_run=True, once=True), 0)
            fake.game.sample.assert_awaited_once()
            fake.username.assert_not_called()
            fake.keychain.assert_not_called()
            fake.cache.assert_not_called()
            fake.connection_factory.assert_not_called()
            fake.connection.authenticate.assert_not_awaited()
            for call in fake.session_factory.call_args_list:
                self.assertIs(call.kwargs["trust_env"], False)
            fake.game_factory.assert_called_once_with(fake.sessions[1])
            self.assertTrue(all(session.closed for session in fake.sessions))
            self.assertEqual(fake.remove_signal.call_count, fake.add_signal.call_count)

    async def test_once_applies_current_scene_and_exits_without_restoring(self):
        light = fake_light()
        with fake_runtime([light]) as fake:
            self.assertEqual(await watcher.run_watcher(fake.runtime, once=True), 0)
        light.api.request.assert_awaited_once()
        self.assertEqual(requested_values(light.api.request.await_args), DEAD)

    async def test_normal_stop_restores_standard_before_session_closes(self):
        light = fake_light()
        with fake_runtime([light]) as fake:
            red_sent = asyncio.Event()
            fake.runtime.should_stop.side_effect = red_sent.is_set

            async def echo(**request):
                self.assertFalse(fake.sessions[0].closed)
                if request["json"]["values"][0]["value"] == "color":
                    red_sent.set()
                return None, request["json"]

            light.api.request.side_effect = echo
            self.assertEqual(await asyncio.wait_for(watcher.run_watcher(fake.runtime), 1), 0)
            self.assertTrue(any(call.kwargs.get("standard_restored") is True
                                for call in fake.runtime.update_status.call_args_list))
            self.assertTrue(all(session.closed for session in fake.sessions))
        self.assertEqual([requested_values(call) for call in light.api.request.await_args_list],
                         [DEAD, STANDARD])

    async def test_cancel_during_red_put_restores_before_closing_session(self):
        light = fake_light()
        with fake_runtime([light]) as fake:
            red_started, red_cancelled = asyncio.Event(), asyncio.Event()
            # This is the platform behavior for the Windows Ctrl+C fallback.
            fake.add_signal.side_effect = NotImplementedError

            async def interrupted_red(**request):
                self.assertFalse(fake.sessions[0].closed)
                if request["json"]["values"][0]["value"] == "color":
                    red_started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        red_cancelled.set()
                return None, request["json"]

            light.api.request.side_effect = interrupted_red
            pending = asyncio.create_task(watcher.run_watcher(fake.runtime))
            try:
                await asyncio.wait_for(red_started.wait(), 1)
                pending.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(pending, 1)
            finally:
                if not pending.done():
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)
            self.assertTrue(red_cancelled.is_set())
            self.assertTrue(all(session.closed for session in fake.sessions))
            self.assertTrue(any(call.kwargs.get("standard_restored") is True
                                for call in fake.runtime.update_status.call_args_list))
            fake.remove_signal.assert_not_called()
        self.assertEqual([requested_values(call) for call in light.api.request.await_args_list],
                         [DEAD, STANDARD])

    async def test_failed_shutdown_restore_reports_failure_and_nonzero_exit(self):
        light = fake_light()
        with fake_runtime([light]) as fake:
            red_sent = asyncio.Event()
            fake.runtime.should_stop.side_effect = red_sent.is_set

            async def failed_restore(**request):
                if request["json"]["values"][0]["value"] == "color":
                    red_sent.set()
                    return None, request["json"]
                raise RequestError("offline restore failure")

            light.api.request.side_effect = failed_restore
            self.assertEqual(await asyncio.wait_for(watcher.run_watcher(fake.runtime), 1), 1)
            self.assertTrue(any(call.kwargs.get("standard_restored") is False
                                for call in fake.runtime.update_status.call_args_list))


if __name__ == "__main__":
    unittest.main()
