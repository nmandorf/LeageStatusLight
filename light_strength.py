"""Brightness and on/off controls. Edit light strength behavior in this file.

The CLI uses these validators before sending combined updates. The async
setters are available for custom Python scripts. No login code belongs here.
"""

import argparse

from errors import SetupError


def parse_brightness(value: str) -> int:
    """Accept a whole-number brightness percentage from the command line."""
    try:
        number = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("Brightness must be a whole number from 0 to 100.") from error
    if not 0 <= number <= 100:
        raise argparse.ArgumentTypeError("Brightness must be between 0 and 100.")
    return number


def validate_power(light) -> None:
    """Check power support without changing the light."""
    if light.power is None:
        raise SetupError(f"{light.name or light.id} does not expose a power control.")


async def set_power(light, enabled: bool) -> None:
    """Turn one light on or off, leaving its brightness and color alone."""
    validate_power(light)
    if not isinstance(enabled, bool):
        raise SetupError("Power must be True (on) or False (off).")
    await (light.turn_on() if enabled else light.turn_off())


def validate_brightness(light, value: int) -> None:
    """Check the percentage and this model's range before sending a command."""
    if type(value) is not int or not 0 <= value <= 100:
        raise SetupError("Brightness must be a whole number from 0 to 100.")
    name = light.name or light.id
    capability = light.brightness
    if capability is None:
        raise SetupError(f"{name} does not expose a brightness control.")
    minimum, maximum = capability.min_value, capability.max_value
    if not minimum <= value <= maximum:
        raise SetupError(f"{name} accepts brightness from {minimum} to {maximum}.")
    step = capability.step
    if step and (value - minimum) % step:
        raise SetupError(f"{name} accepts brightness in steps of {step} from {minimum}.")


async def set_brightness(light, value: int) -> None:
    """Set one light's brightness percentage, preserving its color and power."""
    validate_brightness(light, value)
    await light.set_brightness(value)
