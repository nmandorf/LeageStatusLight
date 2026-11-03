"""Offline checks of combined settings, using real Hubspace function models."""

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from hubspaceng.const import METADATA_API_CALLING_HOST, METADATA_API_HOST, USER_AGENT
from hubspaceng.errors import RequestError
from hubspaceng.models.devices.lights.base import BaseLightDevice
from hubspaceng.models.devices.lights.tunable import TunableLightDevice
from hubspaceng.models.functions.color import ColorValue

from errors import SetupError
from light_transport import apply_updates, build_updates
from test_light_color import category, light_with, rgb_definition, temperature_definition


def brightness_definition(minimum=0, maximum=100, step=1):
    return {
        "id": "brightness", "functionClass": "brightness", "type": "numeric",
        "values": [{"range": {"min": minimum, "max": maximum, "step": step}}],
    }


def full_light():
    return light_with([
        category("power", "on", "off"), brightness_definition(),
        category("color-mode", "color", "white"), rgb_definition(), temperature_definition(),
    ])


def action(command, value=None, color=None):
    return SimpleNamespace(command=command, value=value, color=color)


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, light, args):
        updates = build_updates(args, light)
        light.api.request.assert_not_awaited()
        with patch("light_transport.get_utc_time", return_value=1234):
            await apply_updates(light, updates)
        return updates

    async def test_combined_command_sends_one_exact_request_with_all_settings(self):
        light = full_light()
        updates = await self.apply(light, action("set", 100, (255, 0, 0)))
        light.api.request.assert_awaited_once_with(
            method="PUT", returns="json",
            url=f"https://{METADATA_API_HOST}/v1/accounts/offline-account/metadevices/offline-light/state",
            headers={"user-agent": USER_AGENT, "host": METADATA_API_CALLING_HOST,
                     "accept-encoding": "gzip", "content-type": "application/json; charset=utf-8"},
            json={"metadeviceId": "offline-light", "values": [
                {"functionClass": "color-mode", "value": "color", "lastUpdateTime": 1234},
                {"functionClass": "color-rgb", "functionInstance": "light", "lastUpdateTime": 1234,
                 "value": {"color-rgb": {"r": 255, "g": 0, "b": 0}}},
                {"functionClass": "brightness", "value": 100, "lastUpdateTime": 1234},
                {"functionClass": "power", "value": "on", "lastUpdateTime": 1234},
            ]},
        )
        self.assertEqual(light.brightness.get_state(), 100)
        self.assertEqual(light.power.get_state(), "on")
        self.assertEqual(updates[1][0].get_state(), ColorValue(255, 0, 0))

    async def test_independent_actions_change_only_requested_settings(self):
        cases = [
            (action("on"), [("power", "on")]),
            (action("off"), [("power", "off")]),
            (action("brightness", 50), [("brightness", 50)]),
            (action("set", 50), [("brightness", 50), ("power", "on")]),
            (action("color", (1, 2, 3)), [("color-mode", "color"),
                                        ("color-rgb", {"color-rgb": {"r": 1, "g": 2, "b": 3}})]),
            (action("temperature", "2700K"), [("color-mode", "white"), ("color-temperature", 2700)]),
        ]
        for args, expected in cases:
            with self.subTest(command=args.command):
                light = full_light()
                await self.apply(light, args)
                light.api.request.assert_awaited_once()
                values = light.api.request.await_args.kwargs["json"]["values"]
                self.assertEqual([(item["functionClass"], item["value"]) for item in values], expected)

    async def test_category_temperature_and_hidden_color_use_device_definitions(self):
        light = light_with([
            category("color-mode", "Color", "White"), rgb_definition(),
            category("color-temperature", "2700K", "4000K"),
        ], TunableLightDevice)
        await self.apply(light, action("temperature", "2700"))
        values = light.api.request.await_args.kwargs["json"]["values"]
        self.assertEqual([item["value"] for item in values], ["White", "2700K"])
        light.api.request.reset_mock()
        await self.apply(light, action("color", (255, 0, 0)))
        self.assertEqual(light.api.request.await_args.kwargs["json"]["values"][0]["value"], "Color")

    async def test_missing_mode_is_optional(self):
        light = light_with([rgb_definition()])
        await self.apply(light, action("color", (1, 2, 3)))
        values = light.api.request.await_args.kwargs["json"]["values"]
        self.assertEqual(len(values), 1)
        self.assertEqual(values[0]["functionClass"], "color-rgb")

    async def test_invalid_or_unsupported_settings_fail_without_writes(self):
        cases = [
            (full_light(), action("brightness", 101)),
            (full_light(), action("set", 100, (256, 0, 0))),
            (full_light(), action("color", (True, 0, 0))),
            (full_light(), action("temperature", "2250")),
            (full_light(), action("list")),
            (light_with([], BaseLightDevice), action("on")),
            (light_with([], BaseLightDevice), action("brightness", 50)),
            (light_with([], BaseLightDevice), action("color", (1, 2, 3))),
            (light_with([], BaseLightDevice), action("temperature", "2700")),
            (light_with([category("color-mode", "white"), rgb_definition()]), action("color", (1, 2, 3))),
            (light_with([category("power", "disabled")], BaseLightDevice), action("on")),
            (light_with([brightness_definition(10, 90, 10)]), action("brightness", 42)),
        ]
        for light, args in cases:
            with self.subTest(command=args.command, value=args.value):
                with self.assertRaises(SetupError):
                    build_updates(args, light)
                light.api.request.assert_not_awaited()

    async def test_network_failure_is_not_retried_or_recorded_as_success(self):
        light = full_light()
        updates = build_updates(action("set", 100, (255, 0, 0)), light)
        light.api.request.side_effect = RequestError("Offline simulated failure")
        with self.assertRaises(RequestError):
            await apply_updates(light, updates)
        light.api.request.assert_awaited_once()
        self.assertTrue(all(control.get_state() is None for control, _ in updates))

    async def test_incomplete_invalid_or_mismatched_response_never_claims_success(self):
        def remove_value(response):
            response["values"].pop()

        def wrong_value(response):
            response["values"][-1]["value"] = "off"

        def wrong_instance(response):
            response["values"][1]["functionInstance"] = "other"

        def invalid_rgb(response):
            response["values"][1]["value"] = {"color-rgb": {"r": 999, "g": 0, "b": 0}}

        def duplicate_value(response):
            response["values"].append(deepcopy(response["values"][0]))

        def wrong_device(response):
            response["metadeviceId"] = "different-light"

        for mutate in (remove_value, wrong_value, wrong_instance, invalid_rgb, duplicate_value, wrong_device):
            with self.subTest(response=mutate.__name__):
                light = full_light()
                updates = build_updates(action("set", 100, (255, 0, 0)), light)

                async def reply(**request):
                    response = deepcopy(request["json"])
                    mutate(response)
                    return None, response

                light.api.request.side_effect = reply
                with self.assertRaises(RequestError):
                    await apply_updates(light, updates)
                light.api.request.assert_awaited_once()
                self.assertTrue(all(control.get_state() is None for control, _ in updates))

        for response in (None, {}, {"values": None}, {"values": []}, {"values": [None]}):
            with self.subTest(response=response):
                light = full_light()
                light.api.request.side_effect = None
                light.api.request.return_value = None, response
                with self.assertRaises(RequestError):
                    await apply_updates(light, build_updates(action("on"), light))

    async def test_empty_duplicate_and_cross_device_updates_fail_before_request(self):
        light, other = full_light(), full_light()
        for updates in ([], [(light.power, "on"), (light.power, "off")], [(other.power, "on")]):
            with self.subTest(updates=updates):
                with self.assertRaises(SetupError):
                    await apply_updates(light, updates)
                light.api.request.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
