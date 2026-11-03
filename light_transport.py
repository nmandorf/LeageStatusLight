"""Send a light's settings together in one Hubspace cloud request.

Keep brightness rules in light_strength.py and color rules in light_color.py.
This module handles only preparing those settings and sending their payload.
"""

from hubspaceng.const import METADATA_API_CALLING_HOST, METADATA_API_HOST, USER_AGENT
from hubspaceng.errors import RequestError
from hubspaceng.models.functions.color import ColorValue
from hubspaceng.util import get_utc_time

from errors import SetupError
import light_color
import light_strength


def build_updates(args, light):
    """Validate an action and return (control, value) pairs without network I/O.

    Build updates for every target before applying any of them, so an invalid
    setting on the second light cannot cause a partial group change.
    """
    updates = []
    if args.command in ("on", "off"):
        light_strength.validate_power(light)
        updates.append((light.power, args.command))
    elif args.command == "brightness":
        light_strength.validate_brightness(light, args.value)
        updates.append((light.brightness, args.value))
    elif args.command == "color":
        updates.extend(_color_updates(light, args.value))
    elif args.command == "temperature":
        light_color.validate_temperature(light, args.value)
        control, value = light_color._mode_setting(light, "white")
        if control is not None:
            updates.append((control, value))
        updates.append(light_color._temperature_setting(light, args.value))
    elif args.command == "set":
        light_strength.validate_power(light)
        light_strength.validate_brightness(light, args.value)
        if args.color is not None:
            updates.extend(_color_updates(light, args.color))
        updates.extend(((light.brightness, args.value), (light.power, "on")))
    else:
        raise SetupError("This command does not change light settings.")

    # Exercise library validation and serialization during preparation too.
    # apply_updates repeats this check for callers supplying their own pairs.
    _serialize_updates(light, updates)
    return updates


def _color_updates(light, rgb):
    light_color.validate_color(light, rgb)
    updates = []
    control, value = light_color._mode_setting(light, "color")
    if control is not None:
        updates.append((control, value))
    updates.append((light_color._color_function(light), ColorValue(*rgb)))
    return updates


def _serialize_updates(light, updates):
    """Prepare values and prevent accidentally combining different devices."""
    if not updates:
        raise SetupError("No light settings were supplied.")
    settings = []
    seen = set()
    for control, value in updates:
        key = (control.func_class, control.func_instance)
        if control.device is not light or key in seen:
            raise SetupError("Each light control must appear once and belong to the selected light.")
        try:
            if not control.validate_state(value):
                raise ValueError("Invalid control value")
            serialized = control.get_serializable_state(value)
        except (TypeError, ValueError, ZeroDivisionError):
            raise SetupError(f"Invalid setting for {light.name or light.id}: {control.title}.") from None
        setting = {"functionClass": control.func_class, "value": serialized}
        if control.func_instance is not None:
            setting["functionInstance"] = control.func_instance
        settings.append(setting)
        seen.add(key)
    return settings


async def apply_updates(light, updates):
    """Send one PUT and require a valid confirmation for every requested value.

    The library's request method retains its authentication and request lock.
    There is no additional retry: an uncertain result must not replay a write.
    A cloud confirmation is not a guarantee of the bulb's physical state.
    """
    settings = _serialize_updates(light, updates)
    timestamp = get_utc_time()
    for setting in settings:
        setting["lastUpdateTime"] = timestamp
    _, response = await light.api.request(
        method="PUT",
        returns="json",
        url=f"https://{METADATA_API_HOST}/v1/accounts/{light.account.id}/metadevices/{light.id}/state",
        headers={
            "user-agent": USER_AGENT,
            "host": METADATA_API_CALLING_HOST,
            "accept-encoding": "gzip",
            "content-type": "application/json; charset=utf-8",
        },
        json={"metadeviceId": str(light.id), "values": settings},
    )

    failure = f"Hubspace did not confirm all requested settings for {light.name or light.id}."
    if not isinstance(response, dict) or not isinstance(response.get("values"), list):
        raise RequestError(failure)
    if response.get("metadeviceId", str(light.id)) != str(light.id):
        raise RequestError(failure)

    confirmed = []
    for (control, _), requested in zip(updates, settings):
        matches = [
            item for item in response["values"]
            if isinstance(item, dict)
            and item.get("functionClass") == control.func_class
            and item.get("functionInstance") == control.func_instance
        ]
        if len(matches) != 1 or "value" not in matches[0]:
            raise RequestError(failure)
        try:
            value = control.parse_state(matches[0]["value"])
            if not control.validate_state(value):
                raise ValueError("Invalid confirmed value")
            if control.get_serializable_state(value) != requested["value"]:
                raise ValueError("The cloud returned a different setting")
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            raise RequestError(failure) from None
        confirmed.append((control, value))

    # Update local values only after the entire response has been checked.
    for control, value in confirmed:
        control._value = value
