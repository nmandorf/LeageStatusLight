"""Color controls. Edit COLOR_PRESETS below to add your own named colors.

Each RGB tuple contains red, green, and blue values from 0 to 255.
White temperature is measured in kelvin: lower values look warmer.
"""

import argparse
import re

from hubspaceng.models.functions.category import CategoryFunction
from hubspaceng.models.functions.color import ColorFunction, ColorValue
from hubspaceng.models.functions.range import RangeFunction

from errors import SetupError


# ---- Edit your favorite colors here -----------------------------------------
COLOR_PRESETS = {
    "red": (255, 0, 0),
    "green": (0, 255, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "orange": (255, 100, 0),
    "purple": (160, 0, 255),
    "pink": (255, 80, 160),
    "cyan": (0, 255, 255),
    "white": (255, 255, 255),
}
# ---------------------------------------------------------------------------


def parse_color(value):
    """Accept a preset name, #RRGGBB, or R,G,B for use in the command line."""
    value = str(value).strip()
    named = COLOR_PRESETS.get(value.casefold())
    if named is not None:
        rgb = named
    elif re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        rgb = tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
    elif re.fullmatch(r"\d+\s*,\s*\d+\s*,\s*\d+", value):
        rgb = tuple(int(component.strip()) for component in value.split(","))
    else:
        raise argparse.ArgumentTypeError(
            "Use a color name (" + ", ".join(COLOR_PRESETS) + "), #RRGGBB, or R,G,B."
        )
    if not _valid_rgb(rgb):
        raise argparse.ArgumentTypeError("Each red, green, and blue value must be a whole number from 0 to 255.")
    return tuple(rgb)


def validate_color(light, rgb):
    """Check the RGB value and this light's controls without changing it."""
    if not _valid_rgb(rgb):
        raise SetupError("Each red, green, and blue value must be a whole number from 0 to 255.")
    if _color_function(light) is None:
        raise SetupError(f"{_name(light)} does not expose an RGB color control.")
    _mode_setting(light, "color")


async def set_color(light, rgb):
    """Switch to color mode and send an RGB color; brightness stays separate."""
    validate_color(light, rgb)
    await _set_mode(light, "color")
    await _color_function(light).set_state(ColorValue(*rgb))


def validate_temperature(light, value):
    """Check a supported white temperature without changing this light."""
    _temperature_setting(light, value)
    _mode_setting(light, "white")


async def set_temperature(light, value):
    """Switch to white mode and set a supported temperature in kelvin."""
    validate_temperature(light, value)
    control, setting = _temperature_setting(light, value)
    await _set_mode(light, "white")
    await control.set_state(setting)


def describe_color_support(light):
    """Describe the device's own color and temperature controls for listings."""
    descriptions = []
    if _color_function(light) is not None:
        descriptions.append("RGB color")
    temperature = _temperature_function(light)
    if isinstance(temperature, CategoryFunction):
        descriptions.append("white: " + ", ".join(temperature.values))
    elif isinstance(temperature, RangeFunction):
        descriptions.append(
            f"white: {temperature.min_value}–{temperature.max_value} K "
            f"(step {temperature.step})"
        )
    return " | ".join(descriptions) or "no color controls"


# Device compatibility helpers. hubspace-ng may select a subtype that leaves
# other supported functions hidden, so use actual device definitions as needed.
def _name(light):
    return light.name or light.id


def _valid_rgb(rgb):
    return (
        isinstance(rgb, (tuple, list))
        and len(rgb) == 3
        and all(type(component) is int and 0 <= component <= 255 for component in rgb)
    )


def _function(light, attribute, function_class, data_type, implementation):
    existing = getattr(light, attribute, None)
    if isinstance(existing, implementation):
        return existing
    lookup = getattr(light, "filter_function_def", None)
    if not callable(lookup):
        return None
    definition = lookup(function_class, data_type)
    if isinstance(definition, ValueError):
        raise SetupError(f"{_name(light)} has multiple {function_class} controls; select one in the code before using it.")
    if not isinstance(definition, dict):
        return None
    return implementation(function_class, light, definition)


def _color_function(light):
    return _function(light, "color", "color-rgb", "object", ColorFunction)


def _temperature_function(light):
    category = _function(light, "color_temp", "color-temperature", "category", CategoryFunction)
    if category is not None:
        return category
    return _function(light, "color_temp", "color-temperature", "numeric", RangeFunction)


def _mode_setting(light, requested):
    control = _function(light, "color_mode", "color-mode", "category", CategoryFunction)
    if control is None:
        return None, None
    for option in control.values:
        if option.casefold() == requested:
            return control, option
    raise SetupError(f"{_name(light)} does not advertise a {requested} color mode.")


async def _set_mode(light, requested):
    control, setting = _mode_setting(light, requested)
    if control is not None:
        await control.set_state(setting)


def _kelvin(value):
    text = str(value).strip()
    if re.fullmatch(r"\d+\s*[kK]?", text):
        return int(text.rstrip("kK").strip())
    return None


def _temperature_setting(light, value):
    control = _temperature_function(light)
    if control is None:
        raise SetupError(f"{_name(light)} does not expose a white temperature control.")
    if isinstance(control, CategoryFunction):
        wanted = str(value).strip().casefold()
        for option in control.values:
            if option.casefold() == wanted:
                return control, option
        kelvin = _kelvin(value)
        if kelvin is not None:
            for option in control.values:
                if _kelvin(option) == kelvin:
                    return control, option
        raise SetupError(f"{_name(light)} accepts these white temperatures: {', '.join(control.values)}.")
    kelvin = _kelvin(value)
    minimum, maximum, step = control.min_value, control.max_value, control.step
    if kelvin is None or not minimum <= kelvin <= maximum:
        raise SetupError(f"{_name(light)} accepts white temperatures from {minimum} to {maximum} kelvin.")
    if step and (kelvin - minimum) % step:
        raise SetupError(f"{_name(light)} accepts white temperatures in steps of {step} kelvin from {minimum}.")
    return control, kelvin
