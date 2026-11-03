"""Offline integration checks; no Keychain, filesystem cache, or network I/O."""

import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import main
from test_light_transport import full_light

ACTION_VALUES = {
    "on": ([], None), "off": ([], None), "brightness": (["50"], 50),
    "color": (["255,0,128"], (255, 0, 128)), "temperature": (["2700"], "2700"),
}


def fake_light(device_id="light-1", name="Desk", minimum=1, maximum=100, step=1):
    light = full_light()
    light._id = device_id
    light.device_json["friendlyName"] = name
    light.brightness.min_value, light.brightness.max_value, light.brightness.step = minimum, maximum, step
    light.power._value, light.brightness._value = "on", 50
    return light


def assert_no_light_writes(test, lights):
    for light in lights:
        light.api.request.assert_not_awaited()


def remove_control(light, attribute, function_class):
    setattr(light, attribute, None)
    light.device_json["description"]["functions"] = [
        item for item in light.device_json["description"]["functions"]
        if item["functionClass"] != function_class
    ]


def sent_values(light):
    return {item["functionClass"]: item["value"]
            for item in light.api.request.await_args.kwargs["json"]["values"]}


@contextlib.contextmanager
def fake_cloud(lights, *, cached=False, refreshed_lights=None):
    connection = SimpleNamespace(authenticate=AsyncMock(), load_lights=AsyncMock(return_value=lights),
                                 used_cached_devices=cached)
    if refreshed_lights is not None:
        connection.load_lights.side_effect = [lights, refreshed_lights]
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(main, "ClientSession", return_value=AsyncMock()))
        stack.enter_context(patch.object(main, "SessionCache"))
        stack.enter_context(patch.object(main, "HubspaceConnection", return_value=connection))
        stack.enter_context(patch.object(main.keyring, "get_password"))
        stack.enter_context(patch.object(main.keyring, "set_password"))
        stack.enter_context(patch.dict(main.os.environ, {"HUBSPACE_PASSWORD": ""}))
        output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        yield connection, output


class CommandTests(unittest.TestCase):
    def test_batch_and_single_light_commands_parse(self):
        for command, (value, expected) in ACTION_VALUES.items():
            with self.subTest(command=command, target="all"):
                args = main.arguments([command, "--all", *value])
                self.assertEqual(args.command, command)
                self.assertTrue(args.all_lights)
                self.assertIsNone(args.light)
                if value:
                    self.assertEqual(args.value, expected)
            with self.subTest(command=command, target="single"):
                args = main.arguments([command, "Left bed", *value])
                self.assertEqual(args.command, command)
                self.assertFalse(args.all_lights)
                self.assertEqual(args.light, "Left bed")
                if value:
                    self.assertEqual(args.value, expected)

    def test_commands_require_exactly_one_target_mode(self):
        for command, (value, _) in ACTION_VALUES.items():
            invalid_commands = (
                [command, *value],
                [command, "--all", "Left bed", *value],
                [command, "Left bed", "--all", *value],
            )
            for argv in invalid_commands:
                with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as result:
                        main.arguments(argv)
                    self.assertEqual(result.exception.code, 2)

    def test_duplicate_names_require_id(self):
        first = fake_light("first", "Desk")
        second = fake_light("second", "DESK")
        with self.assertRaises(main.SetupError):
            main.select_light([first, second], "desk")
        self.assertIs(main.select_light([first, second], "second"), second)
        assert_no_light_writes(self, [first, second])

    def test_invalid_brightness_is_rejected_before_credentials_or_login(self):
        for value in ("-1", "101", "1.5", "bright"):
            with self.subTest(value=value), patch.object(main, "username") as username, \
                    patch.object(main, "HubspaceConnection") as login, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    main.main(["brightness", "Desk", value])
                self.assertEqual(result.exception.code, 2)
                username.assert_not_called()
                login.assert_not_called()

    def test_invalid_color_is_rejected_before_credentials_or_login(self):
        for value in ("256,0,0", "-1,0,0", "1,2", "1.5,0,0", "#gg1122", "unknown-color"):
            with self.subTest(value=value), patch.object(main, "username") as username, \
                    patch.object(main.keyring, "get_password") as saved_password, \
                    patch.object(main, "HubspaceConnection") as login, \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    main.main(["color", "Desk", value])
                self.assertEqual(result.exception.code, 2)
                username.assert_not_called()
                saved_password.assert_not_called()
                login.assert_not_called()

    def test_colors_command_works_without_credentials_keychain_or_cloud(self):
        with patch.object(main, "username") as username, \
                patch.object(main.keyring, "get_password") as read_password, \
                patch.object(main.keyring, "set_password") as save_password, \
                patch.object(main, "ClientSession") as session, \
                patch.object(main, "HubspaceConnection") as login, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main.main(["colors"]), 0)
        self.assertIn("red", output.getvalue().lower())
        username.assert_not_called()
        read_password.assert_not_called()
        save_password.assert_not_called()
        session.assert_not_called()
        login.assert_not_called()


class CloudBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def run_with_fake_cloud(self, args, lights):
        with fake_cloud(lights) as (connection, output):
            await main.run(args, "offline-test@example.invalid", "test-only")
            main.keyring.set_password.assert_not_called()
        connection.authenticate.assert_awaited_once_with(force=False)
        connection.load_lights.assert_awaited_once_with(fresh_state=args.command in (None, "list", "login"))
        return output.getvalue()

    async def test_default_lists_lights_without_writing(self):
        lights = [fake_light()]
        output = await self.run_with_fake_cloud(main.arguments([]), lights)
        self.assertIn("Found 1 light(s)", output)
        self.assertIn("Desk | on | brightness: 50", output)
        assert_no_light_writes(self, lights)

    async def test_ambiguous_target_never_changes_either_light(self):
        lights = [fake_light("first", "Desk"), fake_light("second", "Desk")]
        with self.assertRaises(main.SetupError):
            await self.run_with_fake_cloud(main.arguments(["off", "Desk"]), lights)
        assert_no_light_writes(self, lights)

    async def test_device_brightness_constraints_are_checked_before_write(self):
        for value in (0, 42):
            with self.subTest(value=value):
                light = fake_light(minimum=10, maximum=90, step=10)
                with self.assertRaises(main.SetupError):
                    await self.run_with_fake_cloud(main.arguments(["brightness", "Desk", str(value)]), [light])
                assert_no_light_writes(self, [light])

    async def test_batch_changes_each_light_once(self):
        for command in ("on", "off", "brightness"):
            with self.subTest(command=command):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                value = ["50"] if command == "brightness" else []
                await self.run_with_fake_cloud(main.arguments([command, "--all", *value]), lights)
                for light in lights:
                    light.api.request.assert_awaited_once()
                    expected = {"brightness": 50} if command == "brightness" else {"power": command}
                    self.assertEqual(sent_values(light), expected)

    async def test_single_target_leaves_other_light_untouched(self):
        for command in ("on", "off", "brightness"):
            with self.subTest(command=command):
                selected, other = fake_light("first", "Left bed"), fake_light("second", "Right bed")
                value = ["50"] if command == "brightness" else []
                await self.run_with_fake_cloud(main.arguments([command, "Left bed", *value]), [selected, other])
                selected.api.request.assert_awaited_once()
                expected = {"brightness": 50} if command == "brightness" else {"power": command}
                self.assertEqual(sent_values(selected), expected)
                assert_no_light_writes(self, [other])

    async def test_batch_validates_all_power_controls_before_any_write(self):
        for command in ("on", "off"):
            with self.subTest(command=command):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                lights[1].power = None
                with self.assertRaises(main.SetupError):
                    await self.run_with_fake_cloud(main.arguments([command, "--all"]), lights)
                assert_no_light_writes(self, lights)

    async def test_batch_validates_all_brightness_controls_before_any_write(self):
        capabilities = (None, SimpleNamespace(min_value=60, max_value=100, step=1),
                        SimpleNamespace(min_value=1, max_value=40, step=1),
                        SimpleNamespace(min_value=10, max_value=90, step=30))
        for capability in capabilities:
            with self.subTest(capability=capability):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                lights[1].brightness = capability
                with self.assertRaises(main.SetupError):
                    await self.run_with_fake_cloud(main.arguments(["brightness", "--all", "50"]), lights)
                assert_no_light_writes(self, lights)

    async def test_batch_with_no_lights_reports_an_error(self):
        for command, (value, _) in ACTION_VALUES.items():
            with self.subTest(command=command), self.assertRaises(main.SetupError):
                await self.run_with_fake_cloud(main.arguments([command, "--all", *value]), [])

    async def test_batch_commands_prepare_every_target_before_first_write(self):
        for command, (value, _) in ACTION_VALUES.items():
            with self.subTest(command=command):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                events = []
                prepare = main.light_transport.build_updates

                def build_updates(args, light):
                    events.append(("prepare", light.id))
                    return prepare(args, light)

                for light in lights:
                    async def write(*, json, light=light, **kwargs):
                        events.append(("write", light.id))
                        return None, json
                    light.api.request.side_effect = write

                with patch.object(main.light_transport, "build_updates", side_effect=build_updates):
                    await self.run_with_fake_cloud(main.arguments([command, "--all", *value]), lights)
                self.assertEqual(events, [("prepare", "first"), ("prepare", "second"),
                                          ("write", "first"), ("write", "second")])

    async def test_batch_unsupported_color_control_prevents_all_writes(self):
        for command in ("color", "temperature"):
            value, _ = ACTION_VALUES[command]
            with self.subTest(command=command):
                lights = [fake_light("first", "Left bed"), fake_light("second", "Right bed")]
                remove_control(lights[1], "color" if command == "color" else "color_temp",
                               "color-rgb" if command == "color" else "color-temperature")
                with self.assertRaises(main.SetupError):
                    await self.run_with_fake_cloud(main.arguments([command, "--all", *value]), lights)
                assert_no_light_writes(self, lights)

    async def test_single_color_target_leaves_other_light_untouched(self):
        for command in ("color", "temperature"):
            value, _ = ACTION_VALUES[command]
            with self.subTest(command=command):
                selected, other = fake_light("first", "Left bed"), fake_light("second", "Right bed")
                await self.run_with_fake_cloud(main.arguments([command, "Left bed", *value]), [selected, other])
                selected.api.request.assert_awaited_once()
                expected = ({"color-mode": "color", "color-rgb": {"color-rgb": {"r": 255, "g": 0, "b": 128}}}
                            if command == "color" else {"color-mode": "white", "color-temperature": 2700})
                self.assertEqual(sent_values(selected), expected)
                assert_no_light_writes(self, [other])

    async def test_list_requests_fresh_state_but_actions_use_cached_definitions(self):
        for argv, expected in (([], True), (["list"], True), (["on", "--all"], False)):
            with self.subTest(argv=argv), fake_cloud([fake_light()]) as (connection, _):
                await main.run(main.arguments(argv), "offline-test@example.invalid", "test-only")
                connection.load_lights.assert_awaited_once_with(fresh_state=expected)

    async def test_login_forces_authentication_and_fresh_state(self):
        light = fake_light()
        with fake_cloud([light]) as (connection, _):
            await main.run(main.arguments(["login"]), "offline-test@example.invalid", "test-only")
            main.keyring.set_password.assert_called_once_with(main.KEYCHAIN_SERVICE, "offline-test@example.invalid", "test-only")
        connection.authenticate.assert_awaited_once_with(force=True)
        connection.load_lights.assert_awaited_once_with(fresh_state=True)
        assert_no_light_writes(self, [light])

    async def test_password_override_forces_authentication(self):
        with fake_cloud([fake_light()]) as (connection, _):
            with patch.dict(main.os.environ, {"HUBSPACE_PASSWORD": "offline-override"}):
                await main.run(main.arguments(["list"]), "offline-test@example.invalid", "offline-override")
        connection.authenticate.assert_awaited_once_with(force=True)

    async def test_missing_cached_name_refreshes_once_and_uses_fresh_light(self):
        old, renamed = fake_light(name="Old name"), fake_light(name="New name")
        with fake_cloud([old], cached=True, refreshed_lights=[renamed]) as (connection, _):
            await main.run(main.arguments(["off", "New name"]), "offline-test@example.invalid", "test-only")
        self.assertEqual(connection.load_lights.await_count, 2)
        connection.load_lights.assert_any_await(fresh_state=False)
        connection.load_lights.assert_any_await(refresh=True)
        self.assertEqual(sent_values(renamed), {"power": "off"})
        assert_no_light_writes(self, [old])

    async def test_missing_name_after_refresh_or_fresh_discovery_never_writes(self):
        for cached in (False, True):
            with self.subTest(cached=cached):
                light = fake_light()
                with fake_cloud([light], cached=cached, refreshed_lights=[light]) as (connection, _):
                    with self.assertRaises(main.SetupError):
                        await main.run(main.arguments(["off", "Missing"]), "offline-test@example.invalid", "test-only")
                self.assertEqual(connection.load_lights.await_count, 2 if cached else 1)
                assert_no_light_writes(self, [light])


if __name__ == "__main__":
    unittest.main()
