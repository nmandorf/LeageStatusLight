"""Offline checks for the short `light` command and combined light updates."""

import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

import light_cli
import main
from test_main import assert_no_light_writes, fake_cloud, fake_light, remove_control, sent_values


class ShortcutTests(unittest.TestCase):
    def test_league_commands_forward_without_reading_hubspace_credentials(self):
        import league_service
        for action in ("start", "stop", "status", "run"):
            with self.subTest(action=action), \
                    patch.object(league_service, "main", return_value=0) as service, \
                    patch.object(main, "main") as application, \
                    patch.object(main.keyring, "get_password") as keychain:
                self.assertEqual(light_cli.main(["league", action]), 0)
                service.assert_called_once_with([action])
                application.assert_not_called()
                keychain.assert_not_called()

    def test_short_commands_forward_to_existing_application(self):
        cases = (
            ([], ["list"]),
            (["list"], ["list"]),
            (["colors"], ["colors"]),
            (["login"], ["login"]),
            (["on"], ["on", "--all"]),
            (["off"], ["off", "--all"]),
            (["0"], ["off", "--all"]),
            (["50"], ["set", "--all", "50"]),
            (["100", "red"], ["set", "--all", "100", "red"]),
            (["50", "#FF6600"], ["set", "--all", "50", "#FF6600"]),
            (["on", "--light", "Left bed"], ["on", "Left bed"]),
            (["0", "--light", "Left bed"], ["off", "Left bed"]),
            (["50", "red", "--light", "Left bed"], ["set", "--light", "Left bed", "50", "red"]),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(main, "main", return_value=0) as application:
                self.assertEqual(light_cli.main(argv), 0)
                application.assert_called_once_with(expected)

    def test_application_failure_is_returned(self):
        with patch.object(main, "main", return_value=1):
            self.assertEqual(light_cli.main(["100", "red"]), 1)

    def test_invalid_shortcut_is_rejected_before_application_or_authentication(self):
        invalid = (["-1"], ["101"], ["1.5"], ["bright"], ["0", "red"])
        for argv in invalid:
            with self.subTest(argv=argv), patch.object(main, "main") as application, \
                    patch.object(main, "username") as username, \
                    patch.object(main.keyring, "get_password") as keychain, \
                    patch.object(main, "HubspaceConnection") as login, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    light_cli.main(argv)
                self.assertEqual(result.exception.code, 2)
                application.assert_not_called()
                username.assert_not_called()
                keychain.assert_not_called()
                login.assert_not_called()

    def test_help_does_not_access_application_keychain_or_cloud(self):
        with patch.object(main, "main") as application, \
                patch.object(main, "username") as username, \
                patch.object(main.keyring, "get_password") as keychain, \
                patch.object(main, "HubspaceConnection") as login, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as result:
                light_cli.main(["--help"])
        self.assertEqual(result.exception.code, 0)
        self.assertIn("light", output.getvalue().lower())
        application.assert_not_called()
        username.assert_not_called()
        keychain.assert_not_called()
        login.assert_not_called()


class CombinedActionTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_fake_cloud(self, argv, lights):
        with fake_cloud(lights) as (connection, _):
            await main.run(main.arguments(argv), "offline-test@example.invalid", "test-only")
            main.keyring.set_password.assert_not_called()
        connection.authenticate.assert_awaited_once_with(force=False)
        connection.load_lights.assert_awaited_once_with(fresh_state=False)

    async def test_combined_color_brightness_and_power_use_one_authentication_and_one_put_per_light(self):
        lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
        events = []
        original_build = main.light_transport.build_updates

        def build(args, light):
            events.append(("prepare", light.id))
            return original_build(args, light)

        for light in lights:
            async def write(*, json, light=light, **kwargs):
                events.append(("write", light.id))
                return None, json
            light.api.request.side_effect = write

        with patch.object(main.light_transport, "build_updates", side_effect=build):
            await self.run_with_fake_cloud(["set", "--all", "100", "red"], lights)
        self.assertEqual(events, [("prepare", "first"), ("prepare", "second"),
                                  ("write", "first"), ("write", "second")])
        for light in lights:
            light.api.request.assert_awaited_once()
            self.assertEqual(sent_values(light), {
                "color-mode": "color", "color-rgb": {"color-rgb": {"r": 255, "g": 0, "b": 0}},
                "brightness": 100, "power": "on",
            })

    async def test_brightness_without_color_turns_on_only_selected_light(self):
        selected, other = fake_light("first", "Left bed"), fake_light("second", "Right bed")
        with patch.object(main.light_color, "validate_color") as validate_color:
            await self.run_with_fake_cloud(["set", "--light", "Left bed", "50"], [selected, other])
        selected.api.request.assert_awaited_once()
        self.assertEqual(sent_values(selected), {"brightness": 50, "power": "on"})
        validate_color.assert_not_called()
        assert_no_light_writes(self, [other])

    async def test_incompatible_second_light_prevents_every_write(self):
        for unsupported in ("power", "brightness", "color"):
            with self.subTest(unsupported=unsupported):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                remove_control(lights[1], unsupported, "color-rgb" if unsupported == "color" else unsupported)
                with self.assertRaises(main.SetupError):
                    await self.run_with_fake_cloud(["set", "--all", "100", "red"], lights)
                assert_no_light_writes(self, lights)


if __name__ == "__main__":
    unittest.main()
