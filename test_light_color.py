"""Offline color checks using real library functions and a fake Hubspace API."""

import argparse
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from hubspaceng.errors import RequestError
from hubspaceng.models.devices.lights.base import BaseLightDevice
from hubspaceng.models.devices.lights.rgb import RGBLightDevice
from hubspaceng.models.devices.lights.tunable import TunableLightDevice

from errors import SetupError
from light_color import (
    describe_color_support,
    parse_color,
    set_color,
    set_temperature,
    validate_color,
    validate_temperature,
)


def category(function_class, *options):
    return {
        "id": function_class,
        "functionClass": function_class,
        "type": "category",
        "values": [{"name": option, "deviceValues": [{"value": option}]} for option in options],
    }


def rgb_definition():
    return {"id": "rgb", "functionClass": "color-rgb", "type": "object", "functionInstance": "light"}


def temperature_definition():
    return {
        "id": "kelvin",
        "functionClass": "color-temperature",
        "type": "numeric",
        "values": [{"name": "color-temperature", "range": {"min": 2200, "max": 6500, "step": 100}}],
    }


def light_with(functions, device_type=RGBLightDevice):
    async def echo_request(**request):
        # Simulate a state API reply without making a network request.
        return None, request["json"]

    api = SimpleNamespace(request=AsyncMock(side_effect=echo_request))
    account = SimpleNamespace(id="offline-account", api=api)
    light = device_type(
        {"id": "offline-light", "friendlyName": "Test light", "description": {"functions": functions}},
        account,
        datetime.now(timezone.utc),
    )
    return light


class ColorParsingTests(unittest.TestCase):
    def test_names_hex_and_rgb(self):
        examples = {
            "blue": (0, 0, 255),
            " RED ": (255, 0, 0),
            "#01aBff": (1, 171, 255),
            "255, 100, 0": (255, 100, 0),
            "0,0,0": (0, 0, 0),
        }
        for text, expected in examples.items():
            with self.subTest(text=text):
                self.assertEqual(parse_color(text), expected)

    def test_invalid_colors_are_argument_errors(self):
        for value in ("", "not-a-color", "#FFF", "#GG0000", "256,0,0", "-1,0,0", "1.5,0,0", "1,2", "1,2,3,4"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                parse_color(value)


class ColorRequestTests(unittest.IsolatedAsyncioTestCase):
    def assert_request(self, request, function_class, value, instance=None):
        self.assertEqual(request.kwargs["method"], "PUT")
        self.assertTrue(request.kwargs["url"].endswith("/accounts/offline-account/metadevices/offline-light/state"))
        setting = {"functionClass": function_class, "lastUpdateTime": 1234, "value": value}
        if instance is not None:
            setting["functionInstance"] = instance
        self.assertEqual(request.kwargs["json"], {"metadeviceId": "offline-light", "values": [setting]})

    async def test_rgb_switches_mode_then_sends_exact_rgb_payload(self):
        light = light_with([category("color-mode", "color", "white", "sequence"), rgb_definition()])
        with patch("hubspaceng.models.functions.base.get_utc_time", return_value=1234):
            await set_color(light, (12, 34, 56))
        requests = light.api.request.await_args_list
        self.assertEqual(len(requests), 2)
        self.assert_request(requests[0], "color-mode", "color")
        self.assert_request(requests[1], "color-rgb", {"color-rgb": {"r": 12, "g": 34, "b": 56}}, "light")

    async def test_numeric_temperature_hidden_by_rgb_subtype_switches_to_white(self):
        light = light_with([category("color-mode", "color", "white"), rgb_definition(), temperature_definition()])
        self.assertFalse(hasattr(light, "color_temp"))
        with patch("hubspaceng.models.functions.base.get_utc_time", return_value=1234):
            await set_temperature(light, "2700K")
        requests = light.api.request.await_args_list
        self.assertEqual(len(requests), 2)
        self.assert_request(requests[0], "color-mode", "white")
        self.assert_request(requests[1], "color-temperature", 2700)
        self.assertIn("white: 2200–6500 K (step 100)", describe_color_support(light))

    async def test_rgb_and_mode_hidden_by_tunable_subtype_are_available(self):
        light = light_with(
            [category("color-mode", "color", "white"), rgb_definition(), category("color-temperature", "2700K", "4000K")],
            TunableLightDevice,
        )
        self.assertFalse(hasattr(light, "color"))
        with patch("hubspaceng.models.functions.base.get_utc_time", return_value=1234):
            await set_color(light, (255, 0, 0))
        self.assertEqual(light.api.request.await_count, 2)
        self.assert_request(light.api.request.await_args_list[0], "color-mode", "color")
        self.assert_request(light.api.request.await_args_list[1], "color-rgb", {"color-rgb": {"r": 255, "g": 0, "b": 0}}, "light")

    async def test_category_temperature_uses_advertised_spelling(self):
        for requested in ("2700", "2700k", "2700 K"):
            with self.subTest(requested=requested):
                light = light_with([category("color-temperature", "2700K", "4000K")], TunableLightDevice)
                with patch("hubspaceng.models.functions.base.get_utc_time", return_value=1234):
                    await set_temperature(light, requested)
                light.api.request.assert_awaited_once()
                self.assert_request(light.api.request.await_args, "color-temperature", "2700K")

    async def test_rgb_only_light_does_not_need_a_mode_control(self):
        light = light_with([rgb_definition()])
        await set_color(light, (0, 0, 255))
        light.api.request.assert_awaited_once()

    async def test_invalid_rgb_values_are_rejected_before_mode_changes(self):
        for rgb in ((256, 0, 0), (-1, 0, 0), (1.5, 0, 0), (True, 0, 0), (1, 2), "red"):
            with self.subTest(rgb=rgb):
                light = light_with([category("color-mode", "color", "white"), rgb_definition()])
                with self.assertRaises(SetupError):
                    await set_color(light, rgb)
                light.api.request.assert_not_awaited()

    async def test_invalid_temperature_is_rejected_before_mode_changes(self):
        for value in ("2100", "6600", "2250", "warm", "2700.0", "-2700"):
            with self.subTest(value=value):
                light = light_with([category("color-mode", "color", "white"), temperature_definition()])
                with self.assertRaises(SetupError):
                    await set_temperature(light, value)
                light.api.request.assert_not_awaited()

    async def test_missing_or_unknown_modes_are_checked_before_writes(self):
        for setter, requested, definition, wrong_mode in (
            (set_color, (255, 0, 0), rgb_definition(), "white"),
            (set_temperature, "2700", temperature_definition(), "color"),
        ):
            with self.subTest(setter=setter.__name__):
                light = light_with([category("color-mode", wrong_mode, "sequence"), definition])
                with self.assertRaises(SetupError):
                    await setter(light, requested)
                light.api.request.assert_not_awaited()

    async def test_unsupported_controls_report_clear_errors_without_writes(self):
        light = light_with([], BaseLightDevice)
        with self.assertRaisesRegex(SetupError, "RGB color"):
            validate_color(light, (255, 0, 0))
        with self.assertRaisesRegex(SetupError, "white temperature"):
            validate_temperature(light, "2700")
        self.assertEqual(describe_color_support(light), "no color controls")
        light.api.request.assert_not_awaited()

    async def test_unsupported_category_temperature_is_rejected(self):
        light = light_with([category("color-temperature", "2700K", "4000K")], TunableLightDevice)
        with self.assertRaisesRegex(SetupError, "2700K, 4000K"):
            await set_temperature(light, "3000")
        light.api.request.assert_not_awaited()

    async def test_mode_failure_stops_before_color_request(self):
        light = light_with([category("color-mode", "color", "white"), rgb_definition()])
        light.api.request.side_effect = ConnectionError("Offline test failure")
        with self.assertRaises(RequestError):
            await set_color(light, (255, 0, 0))
        light.api.request.assert_awaited_once()
        self.assertEqual(light.api.request.await_args.kwargs["json"]["values"][0]["functionClass"], "color-mode")


if __name__ == "__main__":
    unittest.main()
